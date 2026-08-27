"""The other three writes' confirmation records, for actions that have no draft.

Issue #62 built :mod:`chip_chat.api.drafts` because ``place_order`` needs a
priced card before it needs a confirmation flag. The remaining three write
actions need the flag and nothing else: cancelling an order, redeeming a reward
and editing preferences do not have to be composed against the catalogue, they
have to be *shown to somebody who then said yes*. This module is that half on
its own.

RFC-001 section 06 is one sentence long and applies to all four: every write
tool takes an identifier for something the visitor has already been shown, and
the ops API rejects anything not marked confirmed by a request carrying the
visitor's session. A draft satisfies that sentence for orders. A
:class:`Confirmation` satisfies it for the rest, and the two are deliberately
the same shape where it matters -- minted by the app, confirmed only by a
request, scoped to one visitor, and expiring.

**What is on the record is what gets written.** :attr:`Confirmation.payload` is
what the card said, frozen at the moment it was rendered, and the ops API sends
*that* to the stored procedure rather than whatever arrived with the call. A
model that renders a card offering to redeem one reward and then calls the write
with another is not a case this module has to detect: the second reward was
never on a card, so there is no confirmation for it.

**A preference edit is identified by its own content.** ``cancel_order`` and
``redeem_points`` name a row that already exists, so the reference id is the
``order_id`` or the ``reward_id``. ``update_preferences`` names nothing --
docs/action-surface.md section 7.4 makes it a partial object over three fields --
so its reference is :func:`preferences_reference`, a digest of exactly what was
shown. Change one character of the proposed preferences after the visitor
confirmed and the digest no longer matches a confirmation, which is the same
refusal by a different route.

**In memory, and therefore per replica.** The same honest limitation
:class:`~chip_chat.api.drafts.DraftStore` and
:class:`~chip_chat.api.ledger.BudgetLedger` carry, for the same reason: a
forgotten confirmation is a refusal, never a write nobody agreed to.
"""

import hashlib
import json
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from chip_chat.api.clock import Clock, SystemClock
from chip_chat.otel import OpsAction

__all__ = [
    "DEFAULT_CONFIRMATION_TTL_SECONDS",
    "Confirmation",
    "ConfirmationCode",
    "ConfirmationLedger",
    "ConfirmationRejectedError",
    "preferences_reference",
]

DEFAULT_CONFIRMATION_TTL_SECONDS: Final = 900.0
"""How long a card stays confirmable, in seconds. Fifteen minutes.

**INVENTED**, and the same number :data:`chip_chat.api.drafts` uses for the same
reason -- docs/action-surface.md section 10 records the draft TTL as invention,
and a second, different ceiling for the other three actions would be a second
invented number nobody could argue with as one.
"""

_MAX_CONFIRMATIONS: Final = 4_096
"""Open confirmations held before the oldest are evicted.

A ceiling rather than unbounded growth, because this dictionary lives for the
lifetime of the process and a visitor who never presses Confirm leaves a record
behind. Eviction costs a re-proposal; no ceiling costs the process.
"""

_ACTIONS_WITHOUT_A_DRAFT: Final = (
    OpsAction.CANCEL_ORDER,
    OpsAction.REDEEM_POINTS,
    OpsAction.UPDATE_PREFERENCES,
)
"""The three this ledger serves. ``place_order`` confirms against a draft.

Written down so that :meth:`ConfirmationLedger.offer` can refuse
``place_order`` outright. A confirmation record for an order would be a second
place an order could be confirmed, and the whole point of #62 is that there is
one.
"""


class ConfirmationCode(StrEnum):
    """Why a write was refused before it reached a procedure.

    Deliberately parallel to the three draft codes of
    :class:`chip_chat.api.drafts.RejectionCode`, and deliberately *not* the same
    strings: a trace that says ``CONFIRMATION_NOT_CONFIRMED`` tells an operator
    which of the two records was missing without a second lookup.
    """

    NOT_FOUND = "CONFIRMATION_NOT_FOUND"
    """No such card for this visitor -- including one that is somebody else's.

    The same answer for a reference that never existed and for one belonging to
    another session. "Somebody else has this" is a fact a stranger is not owed;
    docs/action-surface.md section 7, first bullet.
    """

    NOT_CONFIRMED = "CONFIRMATION_NOT_CONFIRMED"
    """The card was rendered and the visitor never pressed Confirm.

    **The launch gate.** An agent that decides to skip the confirmation step
    produces this and an eval failure, not a write.
    """

    EXPIRED = "CONFIRMATION_EXPIRED"
    """The card aged out. A confirmation left open in a tab is not consent kept."""


class ConfirmationRejectedError(Exception):
    """A refusal naming the rule it broke.

    Attributes:
        code: Which of the three.
        message: What happened, in a sentence, for a log rather than a visitor.
    """

    __slots__ = ("code", "message")

    def __init__(self, code: ConfirmationCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_result(self) -> Mapping[str, str]:
        """Render the refusal as the tool result the model is handed back."""
        return {"error": self.code.value, "detail": self.message}


@dataclass(frozen=True, slots=True)
class Confirmation:
    """One card the visitor was shown, and whether they said yes to it.

    Frozen, and :meth:`ConfirmationLedger.confirm` replaces the record rather
    than mutating it -- so there is no attribute anywhere in the process that a
    stray assignment could set to ``True``.

    Attributes:
        confirmation_id: The opaque id this record is known by, and the
            idempotency key the ops API spends on the write it authorises.
        demo_id: The visitor it was minted for, resolved from the session by the
            app. Never supplied by a client and never by a tool argument.
        action: Which write it authorises. One record authorises one action.
        reference_id: What the action names -- an ``order_id``, a ``reward_id``,
            or :func:`preferences_reference` of the proposed preferences.
        payload: What the card said, frozen. This is what the ops API sends to
            the stored procedure.
        created_at: When it was minted.
        expires_at: When it stops being confirmable, in wall-clock time, for the
            card. The check itself reads a monotonic clock -- see
            :attr:`expires_after`.
        expires_after: The monotonic instant the TTL ends at. Monotonic because
            a system clock that steps backwards must not resurrect dead consent.
        confirmed: Whether the visitor pressed Confirm. Set only by
            :meth:`ConfirmationLedger.confirm`.
    """

    confirmation_id: str
    demo_id: str
    action: OpsAction
    reference_id: str
    payload: Mapping[str, Any]
    created_at: datetime
    expires_at: datetime
    expires_after: float
    confirmed: bool = False

    def as_card(self) -> Mapping[str, Any]:
        """The confirmation card, which is what the widget renders."""
        return {
            "confirmation_id": self.confirmation_id,
            "action": self.action.value,
            "reference_id": self.reference_id,
            "details": dict(self.payload),
            "requires_confirmation": True,
            "confirmed": self.confirmed,
            "expires_at": self.expires_at.isoformat(),
        }


class ConfirmationLedger:
    """Mints confirmation records, and holds the flag the ops API reads.

    Thread-safe, for the same reason :class:`~chip_chat.api.drafts.DraftStore`
    is: a Container App serves concurrent requests, and a dictionary mutated
    from two of them is the kind of bug that appears once, in front of somebody.

    One instance per process holds every visitor's records, keyed by
    confirmation id and scoped by ``demo_id`` on the way in and out. A
    per-visitor ledger would put the scoping in the caller, which is exactly
    where it must not be.
    """

    __slots__ = (
        "_by_reference",
        "_clock",
        "_lock",
        "_max_confirmations",
        "_records",
        "_ttl_seconds",
    )

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = DEFAULT_CONFIRMATION_TTL_SECONDS,
        max_confirmations: int = _MAX_CONFIRMATIONS,
    ) -> None:
        """Assemble the ledger.

        Args:
            clock: Source of time. Defaults to the system clock.
            ttl_seconds: How long a record stays confirmable.
            max_confirmations: Open records held before the oldest are evicted.
        """
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ttl_seconds = ttl_seconds
        self._max_confirmations = max_confirmations
        self._lock = threading.Lock()
        self._records: dict[str, Confirmation] = {}
        self._by_reference: dict[tuple[str, OpsAction, str], str] = {}

    def offer(
        self,
        demo_id: str,
        action: OpsAction,
        reference_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Confirmation:
        """Mint an unconfirmed record for a card about to be rendered.

        Writes nothing and authorises nothing: an offer is a question. What it
        does do is fix, at the moment the card is composed, everything the write
        will later be held to.

        A second offer for the same ``(action, reference_id)`` retires the
        first, so a visitor looking at one card for one subject has exactly one
        record behind it. Two live records for the same subject would be two
        answers to the same question, one of which nobody is looking at.

        Args:
            demo_id: The visitor the card belongs to, resolved from the session
                by the app.
            action: Which write this card offers.
            reference_id: What the action names.
            payload: What the card says. Frozen on the way in, and sent to the
                stored procedure verbatim when the write happens.

        Returns:
            The unconfirmed record.

        Raises:
            ValueError: If ``demo_id`` or ``reference_id`` is empty, or
                ``action`` is ``place_order`` -- which confirms against a draft
                and must not have a second route. All three are wiring bugs
                rather than anything a visitor did.
        """
        if not demo_id:
            raise ValueError("a confirmation has to belong to a visitor")
        if not reference_id:
            raise ValueError("a confirmation has to name what it is confirming")
        if action not in _ACTIONS_WITHOUT_A_DRAFT:
            raise ValueError(
                f"{action.value} confirms against a draft "
                "(chip_chat.api.drafts), not against this ledger"
            )

        now = self._clock.now()
        record = Confirmation(
            confirmation_id=f"cfm-{secrets.token_urlsafe(9)}",
            demo_id=demo_id,
            action=action,
            reference_id=reference_id,
            payload=_frozen_mapping(payload or {}),
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
            expires_after=self._clock.monotonic() + self._ttl_seconds,
        )
        key = (demo_id, action, reference_id)
        with self._lock:
            self._sweep_locked()
            superseded = self._by_reference.get(key)
            if superseded is not None:
                self._records.pop(superseded, None)
            self._records[record.confirmation_id] = record
            self._by_reference[key] = record.confirmation_id
        return record

    def confirm(self, demo_id: str, confirmation_id: str) -> Confirmation:
        """Mark a record confirmed on behalf of ``demo_id``.

        **The launch gate.** Called by the request handler when a request
        carrying the visitor's session cookie says the Confirm button was
        pressed, and by nothing else. There is deliberately no tool that reaches
        it and no argument to it the model can supply.

        Args:
            demo_id: The visitor the confirming request resolved to.
            confirmation_id: The card they were shown.

        Returns:
            The confirmed record.

        Raises:
            ConfirmationRejectedError: ``CONFIRMATION_NOT_FOUND`` if there is no
                such record for this visitor -- including the case where it
                belongs to somebody else -- or ``CONFIRMATION_EXPIRED``.
        """
        with self._lock:
            record = self._live_locked(demo_id, confirmation_id)
            confirmed = replace(record, confirmed=True)
            self._records[confirmation_id] = confirmed
            return confirmed

    def claim(self, demo_id: str, action: OpsAction, reference_id: str) -> Confirmation:
        """Return a confirmed record for writing, and retire it.

        What the ops API calls before it writes anything, and the counterpart of
        :meth:`chip_chat.api.drafts.DraftStore.claim`. The record is removed as
        it is handed over, so one confirmation becomes at most one write.

        Args:
            demo_id: The visitor the writing request resolved to.
            action: Which write is being attempted.
            reference_id: What that write names.

        Returns:
            The confirmed, unexpired record.

        Raises:
            ConfirmationRejectedError: ``CONFIRMATION_NOT_FOUND``,
                ``CONFIRMATION_EXPIRED``, or ``CONFIRMATION_NOT_CONFIRMED``. The
                last is the launch gate, and it is a refusal rather than a
                warning.
        """
        with self._lock:
            confirmation_id = self._by_reference.get((demo_id, action, reference_id))
            if confirmation_id is None:
                raise ConfirmationRejectedError(
                    ConfirmationCode.NOT_FOUND,
                    f"no {action.value} card for {reference_id!r} belongs to this "
                    "visitor",
                )
            record = self._live_locked(demo_id, confirmation_id)
            if not record.confirmed:
                raise ConfirmationRejectedError(
                    ConfirmationCode.NOT_CONFIRMED,
                    f"the {action.value} card for {reference_id!r} has not been "
                    "confirmed by the visitor",
                )
            self._forget_locked(record)
            return record

    def get(self, demo_id: str, confirmation_id: str) -> Confirmation | None:
        """Return a live record belonging to ``demo_id``, or ``None``.

        The non-raising read, for a caller rendering a card rather than
        enforcing a rule.
        """
        with self._lock:
            try:
                return self._live_locked(demo_id, confirmation_id)
            except ConfirmationRejectedError:
                return None

    def discard(self, demo_id: str, confirmation_id: str) -> bool:
        """Drop a record. Returns whether there was one of ``demo_id``'s to drop."""
        with self._lock:
            record = self._records.get(confirmation_id)
            if record is None or record.demo_id != demo_id:
                return False
            self._forget_locked(record)
            return True

    def __len__(self) -> int:
        """How many records are held, expired ones included until they are swept."""
        with self._lock:
            return len(self._records)

    # --- internals --------------------------------------------------------

    def _live_locked(self, demo_id: str, confirmation_id: str) -> Confirmation:
        """Return an unexpired record of ``demo_id``'s. Caller holds the lock."""
        record = self._records.get(confirmation_id)
        if record is None or record.demo_id != demo_id:
            raise ConfirmationRejectedError(
                ConfirmationCode.NOT_FOUND,
                f"confirmation {confirmation_id!r} does not belong to this visitor",
            )
        if self._clock.monotonic() >= record.expires_after:
            self._forget_locked(record)
            raise ConfirmationRejectedError(
                ConfirmationCode.EXPIRED,
                f"confirmation {confirmation_id!r} expired at "
                f"{record.expires_at.isoformat()}",
            )
        return record

    def _forget_locked(self, record: Confirmation) -> None:
        """Remove a record and its reference index entry. Caller holds the lock."""
        self._records.pop(record.confirmation_id, None)
        key = (record.demo_id, record.action, record.reference_id)
        if self._by_reference.get(key) == record.confirmation_id:
            del self._by_reference[key]

    def _sweep_locked(self) -> None:
        """Drop expired records, then the oldest, until there is room for one more."""
        now = self._clock.monotonic()
        for record in [r for r in self._records.values() if now >= r.expires_after]:
            self._forget_locked(record)
        while len(self._records) >= self._max_confirmations:
            oldest = min(self._records.values(), key=lambda r: r.created_at)
            self._forget_locked(oldest)


def preferences_reference(prefs: Mapping[str, Any]) -> str:
    """Return the reference id for a proposed preference edit.

    ``update_preferences`` names no row -- docs/action-surface.md section 7.4
    makes it a partial object over three editable fields -- so what the visitor
    is shown *is* its own identifier. The digest is over a canonical rendering,
    so key order and whitespace do not change it and a value does.

    An explicit null is meaningful here (section 7.4: absent leaves a field
    alone, null clears it), so nulls are kept rather than dropped.

    Args:
        prefs: The preferences as they will be shown on the card.

    Returns:
        ``prefs-`` followed by the first sixteen hex characters of the SHA-256
        of the canonical JSON. Sixteen because this identifies one card inside
        one visitor's session rather than a document in a store.
    """
    canonical = json.dumps(prefs, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"prefs-{digest[:16]}"


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only deep copy, so a card cannot be edited after it is shown."""
    return MappingProxyType({key: _frozen(item) for key, item in value.items()})


def _frozen(value: Any) -> Any:
    """Freeze one JSON-shaped value: mappings become proxies, sequences tuples."""
    if isinstance(value, Mapping):
        return _frozen_mapping(value)
    if isinstance(value, str | bytes):
        return value
    if isinstance(value, Sequence):
        return tuple(_frozen(item) for item in value)
    return value
