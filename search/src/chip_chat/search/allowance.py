"""The 1,000 semantic requests a month, counted — because past them there is no bill.

The search service is on the **Free** tier (``search.tf``, and
``docs/service-inventory.md`` for how it got there), and on Free the semantic
ranker is not merely cheap. It is capped:

    semanticSearch = "free"    1,000 semantic requests per calendar month
    semanticSearch = "standard"  $1.00 per 1,000 — and rejected on a Free SKU,
                                 in as many words: *"Semantic Search Standard
                                 Tier is not supported on Free SKU."*

So the thousand-and-first semantic query of a month does not cost a dollar. It
**fails**. That single fact is why :mod:`chip_chat.search.retrieve` has a
hybrid-without-reranking path at all, and why RFC-001 §10's *"AI Search
unavailable → the knowledge lane declines"* must not be reached by way of a
spent allowance: an exhausted reranker is not an outage, and a lane that
declined because a counter rolled over would be declining for a reason the
visitor cannot see and nobody can fix until the first of the month.

**Counted when issued, not when answered.** A semantic request that fails may
well not be charged against the allowance; counting it anyway errs on the side
the ceiling is on. Being one request pessimistic against a limit of 1,000 is not
a failure mode. Being one request optimistic is.

**The count outlives the process, if it is asked to.** The obvious way to spend
a month's allowance without noticing is an evaluation sweep — issue #50 runs the
retriever over a golden set, and a hundred queries is a tenth of the month in
one command. :class:`InMemoryAllowanceStore` cannot see that happen twice;
:class:`FileAllowanceStore` can, and it is what the eval harness should be given.
Issue #10 asks for exactly this counter.
"""

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

__all__ = [
    "FREE_TIER_SEMANTIC_REQUESTS",
    "AllowanceReport",
    "AllowanceStore",
    "Clock",
    "FileAllowanceStore",
    "InMemoryAllowanceStore",
    "SemanticAllowance",
    "SystemClock",
    "month_of",
]

FREE_TIER_SEMANTIC_REQUESTS: Final = 1000
"""Semantic requests a Free-tier service may serve in a calendar month.

Not a price and not a soft limit. Verified as the Free plan's allowance on
2026-08-26 (bead ``cc-okc``), alongside the finding that ``standard`` — the plan
that converts the ceiling into $1.00 per 1,000 — is refused outright on a Free
SKU. Moving the service to Basic is what makes this number go away, and it costs
$73.73 a month.
"""


class Clock(Protocol):
    """Wall-clock time, injected so a month boundary can be tested."""

    def now(self) -> datetime:
        """Return the current time, timezone-aware and in UTC."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        """Return :func:`datetime.datetime.now` in UTC."""
        return datetime.now(UTC)


def month_of(moment: datetime) -> str:
    """Return the calendar month ``moment`` falls in, as ``2026-08``.

    UTC, and the assumption is worth naming: Azure's allowance resets on *a*
    calendar month, and this counter resets on the UTC one. The two can differ
    for a few hours at a month boundary, in which case this counter is the
    stricter of the pair for a service whose subscription sits east of UTC and
    the looser for one west of it. At a thousand requests a month, a few hours
    of disagreement is not the risk; forgetting to reset at all would be.

    Args:
        moment: Any instant.

    Returns:
        ``YYYY-MM``.
    """
    return moment.astimezone(UTC).strftime("%Y-%m")


class AllowanceStore(Protocol):
    """Where the running count lives between reads."""

    def read(self) -> tuple[str, int]:
        """Return the month the count belongs to and the count."""
        ...

    def write(self, month: str, spent: int) -> None:
        """Record ``spent`` requests against ``month``."""
        ...


class InMemoryAllowanceStore:
    """A count that lives as long as the process. The default.

    Right for the deployed app, which is one long-lived container: the retriever
    is built once and the allowance is built with it. Wrong for anything that
    runs the retriever in short-lived processes, which is what
    :class:`FileAllowanceStore` is for.
    """

    __slots__ = ("_month", "_spent")

    def __init__(self, month: str = "", spent: int = 0) -> None:
        """Initialise the store.

        Args:
            month: The month the initial count belongs to.
            spent: Requests already spent in it.
        """
        self._month = month
        self._spent = spent

    def read(self) -> tuple[str, int]:
        return self._month, self._spent

    def write(self, month: str, spent: int) -> None:
        self._month, self._spent = month, spent


class FileAllowanceStore:
    """A count in a small JSON file, so it survives the process that made it.

    Written whole and moved into place with :func:`os.replace`, so a reader
    never sees half a number. Two processes racing can still lose one write and
    undercount by one — which is a rounding error against a ceiling of 1,000,
    and a very different thing from the failure this exists to prevent, which is
    an eval sweep spending the month's allowance twice because nothing was
    keeping score between runs.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        """Initialise the store.

        Args:
            path: The file to keep the count in. It and its parent directory
                are created on first write.
        """
        self._path = path

    def read(self) -> tuple[str, int]:
        try:
            payload: Mapping[str, Any] = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            # A missing or unreadable file is a count of zero rather than an
            # error: a counter that could refuse to start would be a way for a
            # bad path to take the knowledge lane down, which is precisely the
            # blast radius RFC-001 section 10 bounds.
            return "", 0
        return str(payload.get("month", "")), int(payload.get("spent", 0))

    def write(self, month: str, spent: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"month": month, "spent": spent}, indent=2), "utf-8"
        )
        os.replace(temporary, self._path)


@dataclass(frozen=True, slots=True)
class AllowanceReport:
    """What the counter knows, for a span, a CLI and issue #10's harness.

    Attributes:
        month: The calendar month these numbers belong to.
        spent: Semantic requests issued in it.
        limit: The ceiling.
        exhausted: Whether reranking is off for the rest of the month.
        reason: Why, when the service said so before the counter did.
    """

    month: str
    spent: int
    limit: int
    exhausted: bool
    reason: str | None = None

    @property
    def remaining(self) -> int:
        """Requests left in the month, never below zero."""
        return max(0, self.limit - self.spent)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready form."""
        return {
            "month": self.month,
            "spent": self.spent,
            "limit": self.limit,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
            "reason": self.reason,
        }


class SemanticAllowance:
    """The month's semantic requests, counted and capped.

    Thread-safe, for the same reason ``chip_chat.api.ledger`` is: the check and
    the increment have to be one step, or twenty concurrent turns a few requests
    below the ceiling all read a number under it and all proceed.
    """

    __slots__ = ("_clock", "_limit", "_lock", "_reason", "_store")

    def __init__(
        self,
        *,
        limit: int = FREE_TIER_SEMANTIC_REQUESTS,
        store: AllowanceStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Initialise the allowance.

        Args:
            limit: The ceiling. Lower it to hold something back — an eval sweep
                given ``limit=200`` cannot spend the demo's month.
            store: Where the count lives. In memory by default.
            clock: Wall-clock time, for the month boundary.
        """
        self._limit = max(0, limit)
        self._store = InMemoryAllowanceStore() if store is None else store
        self._clock = SystemClock() if clock is None else clock
        self._lock = threading.Lock()
        self._reason: str | None = None

    def _current(self) -> tuple[str, int]:
        """Return this month and what has been spent in it, rolling over."""
        month = month_of(self._clock.now())
        recorded, spent = self._store.read()
        if recorded != month:
            return month, 0
        return month, spent

    def spend(self, requests: int = 1) -> bool:
        """Claim ``requests`` semantic requests, if the month has them.

        Args:
            requests: How many to claim.

        Returns:
            ``True`` if they were claimed and the caller may ask for reranking;
            ``False`` if the allowance is spent, in which case nothing was
            counted and the caller degrades to hybrid without reranking.
        """
        with self._lock:
            month, spent = self._current()
            if spent + requests > self._limit:
                self._store.write(month, min(spent, self._limit))
                return False
            self._store.write(month, spent + requests)
            return True

    def exhaust(self, reason: str) -> None:
        """Record that the service itself refused, and stop asking this month.

        The service is the authority and the counter is an estimate of it. When
        the two disagree — a semantic request refused while the count says there
        is room, because the allowance was partly spent by something outside
        this process — the service wins and the rest of the month is hybrid
        without reranking.

        Args:
            reason: What the service said, kept for the span and the CLI.
        """
        with self._lock:
            month, _ = self._current()
            self._store.write(month, self._limit)
            self._reason = reason

    def report(self) -> AllowanceReport:
        """Return what the counter knows, without claiming anything."""
        with self._lock:
            month, spent = self._current()
            return AllowanceReport(
                month=month,
                spent=spent,
                limit=self._limit,
                exhausted=spent >= self._limit,
                reason=self._reason,
            )
