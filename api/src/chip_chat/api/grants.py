"""The confirmation grant: what the ops API verifies when it cannot look anything up.

RFC-001 §06 gives the write path one rule and ``docs/ops-api.md`` states it in
the imperative: *the ops API rejects any draft that has not been marked confirmed
by a request carrying the visitor's session.* Until this module existed, the ops
API kept that rule by **looking the record up** -- ``DraftStore.claim`` and
``ConfirmationLedger.claim``, both reading a flag out of a dictionary in the same
process.

That worked, and it worked only while there was one process. The deployed system
has two: the chat app mints and confirms drafts in a Container App, and the ops
API is an Azure Function with the write role. A draft minted in the first is
invisible to the second, so wiring the two together naively would have made every
``place_order`` return ``DRAFT_NOT_FOUND`` -- the gate refusing not because
nobody confirmed but because nobody could see that they had.

**A grant is the record, signed.** When a request carrying the visitor's session
confirms a card, and only then, the app can claim that record and sign what it
claimed. The signature covers the action, the visitor, the reference the visitor
was shown, the arguments the procedure will be called with, a single-use id, and
an expiry. The ops API then does exactly what it did before -- refuse anything it
cannot claim -- with *verify* in the place of *look up*.

Three properties survive the move, and they are the three the gate is made of.

**What is written is still what was confirmed.** :attr:`Grant.arguments` is the
procedure's own argument list after the retry key, built in the app from the
claimed record and then signed. The ops API passes it to
:func:`chip_chat.api.ops._arguments` unread. There is no field anywhere on the
wire through which a model could alter an order between the card the visitor read
and the row that gets written, because the one field that decides the row is
inside the signature.

**A grant is bound to one visitor.** :attr:`Grant.demo_id` is signed, and
:func:`verify` refuses a grant whose visitor is not the one the request's
``x-cilantro-session`` header resolved to. A stolen grant presented on somebody
else's session is refused before a database session is acquired, which is the
same answer ``another-session`` already got out of the lookup.

**A grant is spent once.** :attr:`Grant.grant_id` is the *retry key*, and every
procedure spends its retry key inside its own transaction with a ``MERGE`` (see
``sql/12_procedures.sql``). So a replayed grant does not write twice; it replays
the first attempt's stored receipt. That is not a new mechanism invented for
this file -- it is the mechanism ``docs/ops-api.md`` already describes for a
connection that dies after the procedure committed, doing a second job it was
already shaped for. The alternative, a spent-nonce table shared between the two
processes, would have re-introduced exactly the shared state this design exists
to avoid, one layer down.

**What the signing key is, and why it is not the ops key itself.** Both tiers
already hold one shared secret: ``CHIP_CHAT_OPS_KEY``, the value the chat app
presents on ``x-cilantro-ops-key`` and the ops API compares with
``hmac.compare_digest``. Adding a second secret to Key Vault would be a second
thing to mint, reference, rotate and get wrong, and ``docs/ops-api.md`` already
records what an unresolved Key Vault reference costs. So the signing key is
*derived* from the ops key rather than stored beside it --
:func:`signing_key`, one HMAC under a fixed label -- which costs nothing, adds
no credential, and buys one real thing: the ops key travels on every request as a
bearer header, and the key that signs grants never leaves either process. A
proxy log, a mirrored header or a misconfigured trace that captured the ops key
would give an attacker the ability to *call* the ops API, which is bad; it would
not give them the ability to *mint a confirmation*, which is worse.

**The wire format is deliberately not a JWT.** It is
``base64url(payload) . base64url(HMAC-SHA256(key, base64url(payload)))``, with no
header segment, because a header segment is a field naming the algorithm and a
field naming the algorithm is the ``alg: none`` foothold. There is one algorithm
here, it is not negotiable, and :data:`VERSION` inside the signed payload is how
a second one would ever be introduced -- signed, rather than announced by the
token itself.

**What this does not defend against.** An attacker who has fully compromised the
*app process* holds the signing key and can mint any grant. That is not a
regression and it is not a hole this module could close: the same attacker
already holds the ops key and can call the write path directly. What the
boundary still buys, and the reason the write role stays where it is, is that
such an attacker is confined to what the four stored procedures allow --
catalogue-validated lines, a published reward at its published cost, the
visitor's own orders -- rather than holding arbitrary SQL against
``CHIP_CHAT.ACCOUNTS``. ``docs/decisions/confirmation-grants.md`` is the full
argument, attacker by attacker.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from chip_chat.otel import OpsAction

__all__ = [
    "DEFAULT_GRANT_TTL_SECONDS",
    "GRANT_HEADER",
    "OPS_KEY_VARIABLE",
    "VERSION",
    "Grant",
    "GrantCode",
    "GrantRejectedError",
    "GrantSigner",
    "mint",
    "signing_key",
    "verify",
]

VERSION: Final = 1
"""The payload version, inside the signature.

Bumping it is how a format change happens. It is signed rather than negotiated
because a token that told the verifier how to read it would be a token that
could tell the verifier to read it leniently.
"""

GRANT_HEADER: Final = "x-cilantro-confirmation"
"""The header a grant travels in, beside ``x-cilantro-session``.

A header rather than a body field, and for the same reason the visitor is one:
the body of a write is the reference the *model* named, and a confirmation that
travelled in the same object as a model-named value would be one field away from
looking like something a model could name. Nothing model-reachable composes a
header on this request; ``chip_chat.api.opsclient`` does, server-to-server.
"""

OPS_KEY_VARIABLE: Final = "CHIP_CHAT_OPS_KEY"
"""Where the shared secret is read from on both tiers.

The same name the Functions app already reads (``api/functions/function_app.py``)
and the name the Container App now carries too, so that one Key Vault secret
configures both ends of one relationship.
"""

DEFAULT_GRANT_TTL_SECONDS: Final = 120.0
"""How long a grant is good for, in seconds.

Two minutes, and deliberately *much* shorter than the fifteen-minute life of the
card it authorises. The two intervals measure different things and conflating
them was tempting and wrong. A card's TTL is how long a visitor has to make up
their mind, and fifteen minutes of that is generous and correct. A grant's TTL
is how long the *already-claimed* record stays writable while an HTTP request
crosses two Azure services, and that is a matter of seconds; anything longer is
a window in which a captured grant is still worth something. Two minutes leaves
room for a warehouse resume, a retry and a clock a little out of step, and
nothing else.
"""

_LABEL: Final = b"chip-chat/confirmation-grant/v1"
"""The derivation label. See :func:`signing_key`.

Fixed, versioned, and never derived from anything a caller supplies: a label
that varied with input would be a key that varied with input, and the point of
the derivation is that both tiers reach the same key from the same secret
without either of them transmitting it.
"""

_SEPARATOR: Final = "."
"""What divides the payload from its signature. One byte, and not in the
alphabet either half is encoded in, so splitting is unambiguous."""


class GrantCode(StrEnum):
    """Why a grant did not authorise the write it arrived with.

    Two codes and not one, because the two mean different things to an operator
    and to :attr:`~chip_chat.otel.attributes.ConfirmationState`. The same split
    ``chip_chat.api.ops`` already makes between the four rejections that are
    launch-gate violations and the two expiries that are not: a grant that does
    not verify is somebody presenting a confirmation nobody gave, and a grant
    that has merely aged out is a request that took too long.
    """

    INVALID = "CONFIRMATION_GRANT_INVALID"
    """The grant is missing, malformed, unsigned, signed with the wrong key, or
    bound to a different visitor, action or reference than the call it arrived
    on. **The launch gate**, and the state an eval counts."""

    EXPIRED = "CONFIRMATION_GRANT_EXPIRED"
    """The grant verified and its window has closed. Consent that was plausibly
    given and is no longer in force -- not an accusation."""


class GrantRejectedError(Exception):
    """A grant that does not authorise this write, naming which rule it broke.

    Attributes:
        code: Which of the two.
        message: What happened, in a sentence, for a log rather than a visitor.
    """

    __slots__ = ("code", "message")

    def __init__(self, code: GrantCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_result(self) -> Mapping[str, str]:
        """Render the refusal the way the other two record types render theirs."""
        return {"error": self.code.value, "detail": self.message}


@dataclass(frozen=True, slots=True)
class Grant:
    """One claimed, confirmed record, in the form a second process can check.

    Frozen, because a grant that could be edited after it was verified would be
    a grant whose signature covered something other than what got written.

    Attributes:
        action: Which write this authorises. One grant authorises one action;
            a grant for ``redeem_points`` presented to ``cancel_order`` does not
            verify, so the four routes cannot be crossed.
        demo_id: The visitor the app resolved from the session cookie when it
            claimed the record. Checked against the bound visitor by
            :func:`verify` -- this is the field that makes a captured grant
            worthless to anybody else.
        reference_id: What the visitor was shown: a draft id, an order id, a
            reward id, or :func:`~chip_chat.api.confirmations.preferences_reference`
            of the preferences on the card.
        arguments: The stored procedure's positional arguments **after** the
            retry key, exactly as the claim built them. This is the payload the
            gate exists to protect: it is assembled in the app from the claimed
            record, signed here, and handed to the procedure unread, so nothing
            that arrives with the call can reach a procedure argument.
        grant_id: The single-use id, which is also the retry key the procedure
            spends. See the module docstring on why replay needs no shared
            state.
        expires_at: Epoch seconds after which this grant authorises nothing.
    """

    action: OpsAction
    demo_id: str
    reference_id: str
    arguments: Sequence[Any]
    grant_id: str
    expires_at: float

    def as_payload(self) -> Mapping[str, Any]:
        """The object that gets signed. Key order is fixed by ``sort_keys``."""
        return {
            "v": VERSION,
            "action": self.action.value,
            "demo_id": self.demo_id,
            "reference_id": self.reference_id,
            "arguments": list(self.arguments),
            "grant_id": self.grant_id,
            "expires_at": self.expires_at,
        }


def signing_key(ops_key: str) -> bytes:
    """Derive the grant-signing key from the shared ops key.

    One HMAC under :data:`_LABEL`. This is a one-way step, so a process holding
    the derived key cannot recover the ops key from it -- which is the small,
    real thing the derivation buys: see the module docstring on why the secret
    that authenticates a caller should not also be the secret that mints a
    confirmation.

    Args:
        ops_key: The value of :data:`OPS_KEY_VARIABLE`, as both tiers read it.

    Returns:
        Thirty-two bytes.

    Raises:
        ValueError: If ``ops_key`` is empty. An empty secret would make every
            grant verify against every other, and failing to come up is the
            right direction to fail in -- the same argument
            ``function_app._authentic`` makes about an unset key refusing every
            request rather than allowing them all.
    """
    if not ops_key:
        raise ValueError("a confirmation grant cannot be signed with an empty key")
    return hmac.new(ops_key.encode("utf-8"), _LABEL, hashlib.sha256).digest()


def new_grant_id() -> str:
    """Mint a single-use grant id, which is also a retry key.

    Random rather than derived from the record, and that is load-bearing. A key
    derived from the draft would make two *deliberate* placements of two
    different drafts distinguishable and two accidental replays of one draft the
    same -- which is what is wanted -- but it would also mean a caller who can
    guess a draft id can guess a retry key, and a guessed retry key spent
    against the procedure is a receipt returned to somebody who wrote nothing.
    """
    return f"cg-{secrets.token_hex(16)}"


def mint(
    key: bytes,
    action: OpsAction,
    demo_id: str,
    reference_id: str,
    arguments: Sequence[Any],
    *,
    ttl_seconds: float = DEFAULT_GRANT_TTL_SECONDS,
    now: float | None = None,
    grant_id: str | None = None,
) -> tuple[Grant, str]:
    """Sign one claimed record, and return it with its wire form.

    Called by the app **after** it has claimed a confirmed record and never
    before: the claim is what checks that a request carrying the visitor's
    session marked the card confirmed, and this function signs the result of
    that check rather than performing it. There is deliberately no argument here
    through which a caller could assert a confirmation -- the caller either has
    a claimed record to sign or it has nothing to pass.

    Args:
        key: From :func:`signing_key`.
        action: Which write.
        demo_id: The visitor the claim was made on behalf of.
        reference_id: What the visitor was shown.
        arguments: The procedure's arguments after the retry key.
        ttl_seconds: How long the grant is good for. See
            :data:`DEFAULT_GRANT_TTL_SECONDS`.
        now: Epoch seconds, for a test that drives the clock.
        grant_id: The retry key, for a caller that has one. ``None`` mints one.

    Returns:
        The grant, and the token to put on :data:`GRANT_HEADER`.
    """
    moment = time.time() if now is None else now
    grant = Grant(
        action=action,
        demo_id=demo_id,
        reference_id=reference_id,
        arguments=list(arguments),
        grant_id=new_grant_id() if grant_id is None else grant_id,
        expires_at=moment + ttl_seconds,
    )
    body = _encode(
        json.dumps(grant.as_payload(), sort_keys=True, default=str).encode("utf-8")
    )
    return grant, f"{body}{_SEPARATOR}{_encode(_signature(key, body))}"


def verify(
    key: bytes,
    token: str,
    *,
    action: OpsAction,
    demo_id: str,
    reference_id: str,
    now: float | None = None,
) -> Grant:
    """Check one grant against the call it arrived on, and return what it says.

    Every check is a refusal and none of them is a repair. The order is the
    argument, and it is the same order ``function_app._answer`` uses for its own
    three preconditions: authenticity first, so that a caller who cannot sign
    learns nothing about which of the bindings would have failed.

    Args:
        key: From :func:`signing_key`.
        token: What arrived on :data:`GRANT_HEADER`.
        action: The write being attempted, from the route rather than the body.
        demo_id: The visitor the request's session header resolved to.
        reference_id: The reference the call carried. For ``update_preferences``
            this is :func:`~chip_chat.api.confirmations.preferences_reference` of
            the preferences on the *call*, so a call whose preferences differ by
            one character from the card's finds no grant -- the same refusal the
            in-process ledger already produced, by the same route.
        now: Epoch seconds, for a test that drives the clock.

    Returns:
        The verified grant, whose :attr:`Grant.arguments` are what the procedure
        is called with.

    Raises:
        GrantRejectedError: :attr:`GrantCode.INVALID` for anything that does not
            verify or does not match the call, and :attr:`GrantCode.EXPIRED` for
            one that has aged out.
    """
    body, _, signature = token.partition(_SEPARATOR)
    if not body or not signature:
        raise GrantRejectedError(
            GrantCode.INVALID, "the confirmation is not a signed grant"
        )
    try:
        presented = _decode(signature)
        payload = json.loads(_decode(body))
    except (ValueError, UnicodeDecodeError) as malformed:
        raise GrantRejectedError(
            GrantCode.INVALID, f"the confirmation could not be read: {malformed}"
        ) from malformed
    if not hmac.compare_digest(_signature(key, body), presented):
        # Constant time, and before anything is read out of the payload. A
        # verifier that inspected an unauthenticated payload first would be a
        # verifier whose error messages describe an attacker's own guesses back
        # to them.
        raise GrantRejectedError(
            GrantCode.INVALID, "the confirmation was not signed by this app"
        )
    if not isinstance(payload, dict) or payload.get("v") != VERSION:
        raise GrantRejectedError(
            GrantCode.INVALID, "the confirmation is of a version this API does not read"
        )
    try:
        grant = Grant(
            action=OpsAction(str(payload.get("action", ""))),
            demo_id=str(payload.get("demo_id", "")),
            reference_id=str(payload.get("reference_id", "")),
            arguments=list(payload.get("arguments", ())),
            grant_id=str(payload.get("grant_id", "")),
            expires_at=float(payload.get("expires_at", 0.0)),
        )
    except (TypeError, ValueError) as unreadable:
        # An authenticated payload this verifier cannot read is a version skew
        # between two deployments of the same repository -- one tier writing an
        # action or a field shape the other does not know. It is refused rather
        # than raised, because a refusal is an answer the caller understands and
        # an exception here would surface as a 400 about a malformed request,
        # which is a description of the wrong thing.
        raise GrantRejectedError(
            GrantCode.INVALID,
            f"the confirmation is not one this API can read: {unreadable}",
        ) from unreadable
    _bound(grant, action=action, demo_id=demo_id, reference_id=reference_id)
    if grant.expires_at <= (time.time() if now is None else now):
        raise GrantRejectedError(
            GrantCode.EXPIRED,
            f"the confirmation for {reference_id!r} expired before the write "
            "reached this service",
        )
    if not grant.grant_id:
        raise GrantRejectedError(
            GrantCode.INVALID, "the confirmation carries no retry key"
        )
    return grant


def _bound(grant: Grant, *, action: OpsAction, demo_id: str, reference_id: str) -> None:
    """Refuse a grant that is for a different write than the one being made.

    Three bindings, all of them signed, and each closing a distinct replay. The
    action stops a grant for one route being spent on another; the visitor stops
    a captured grant being spent by a stranger; and the reference stops a grant
    for one card authorising a different one. A grant that verified but was not
    bound would be a bearer token for *any* write, which is precisely what a
    confirmation must not be.

    Ordinary comparison rather than :func:`hmac.compare_digest`, and the
    asymmetry with the signature check above is deliberate. The signature is the
    one comparison where timing leaks something an attacker can use, because it
    is the one an attacker can grind against. These three are comparisons between
    two values the attacker *already supplied or already knows* -- a grant that
    reached this line has been authenticated, so its contents are not a secret to
    guess -- and reaching for a constant-time comparison here would only add a
    ``TypeError`` on the day a non-ASCII identifier arrives, which is a refusal
    turning into a crash.

    Raises:
        GrantRejectedError: :attr:`GrantCode.INVALID`, with the same sentence
            whichever binding failed -- the caller is told the confirmation does
            not match, not which field would have made it match.
    """
    if (
        grant.action is not action
        or grant.demo_id != demo_id
        or grant.reference_id != reference_id
    ):
        raise GrantRejectedError(
            GrantCode.INVALID,
            "the confirmation does not authorise this write",
        )


class GrantSigner:
    """A key, and the two things either tier does with it.

    A small object rather than two free functions with a key threaded through
    every call site, for the reason :class:`~chip_chat.api.ops.OpsService` is a
    class: a service that can be constructed without one has a configuration in
    which the gate is absent, and there should be no such configuration.

    Attributes:
        ttl_seconds: How long the grants this signer mints are good for.
    """

    __slots__ = ("_key", "ttl_seconds")

    def __init__(
        self, ops_key: str, *, ttl_seconds: float = DEFAULT_GRANT_TTL_SECONDS
    ) -> None:
        """Derive the signing key from the shared ops key.

        Args:
            ops_key: The value of :data:`OPS_KEY_VARIABLE`.
            ttl_seconds: See :data:`DEFAULT_GRANT_TTL_SECONDS`.

        Raises:
            ValueError: If ``ops_key`` is empty. See :func:`signing_key`.
        """
        self._key = signing_key(ops_key)
        self.ttl_seconds = ttl_seconds

    def mint(
        self,
        action: OpsAction,
        demo_id: str,
        reference_id: str,
        arguments: Sequence[Any],
        *,
        now: float | None = None,
    ) -> tuple[Grant, str]:
        """Sign one claimed record. See :func:`mint`."""
        return mint(
            self._key,
            action,
            demo_id,
            reference_id,
            arguments,
            ttl_seconds=self.ttl_seconds,
            now=now,
        )

    def verify(
        self,
        token: str,
        *,
        action: OpsAction,
        demo_id: str,
        reference_id: str,
        now: float | None = None,
    ) -> Grant:
        """Check one grant against the call it arrived on. See :func:`verify`."""
        return verify(
            self._key,
            token,
            action=action,
            demo_id=demo_id,
            reference_id=reference_id,
            now=now,
        )


def _signature(key: bytes, body: str) -> bytes:
    """HMAC-SHA256 over the *encoded* payload, not the decoded one.

    Over the encoded form because that is what arrived: a verifier that decoded
    first and signed the result would be verifying a re-serialisation, and two
    JSON encoders that disagree about whitespace would then disagree about
    authenticity.
    """
    return hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()


def _encode(raw: bytes) -> str:
    """base64url without padding, which is URL- and header-safe."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(encoded: str) -> bytes:
    """Undo :func:`_encode`, restoring the padding it stripped."""
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))
