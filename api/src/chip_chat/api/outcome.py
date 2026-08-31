"""The vocabulary of a stop: which ceiling, how close, and what the visitor sees.

The reason tokens below are a schema, not log text. They land on
``chip_chat.guard.reason`` and Phase 9's evaluations group on them, so they are
stable machine-comparable words rather than sentences -- the sentences are
:data:`STOP_STATE_MESSAGE` and :data:`SESSION_STOP_MESSAGE`, and
:func:`stop_message` is the only thing that decides between them.
"""

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "SESSION_SCOPED_REASONS",
    "SESSION_STOP_MESSAGE",
    "STOP_STATE_MESSAGE",
    "BudgetScope",
    "Stop",
    "StopReason",
    "Usage",
    "stop_message",
]

STOP_STATE_MESSAGE = "Cilantro's had a busy day — come back tomorrow"
"""What a visitor stopped by a *global* ceiling is told, on entry and mid-turn.

PRD requirement S4: this is a designed state, not an error. It is never
accompanied by a 4xx or 5xx status, never says "quota", and never apologises for
a failure -- because nothing failed. The cap worked.

This docstring used to say that there was exactly one such sentence, and the
argument it was making was right about the wrong thing. What S4 is defending is
the *register*: no error framing, no apology, no leaking of the mechanism to
whoever is probing it. It was never defending the claim that every ceiling in
the system stops the same conversation for the same duration -- and issue #108
is what that conflation cost. A visitor who reached the end of one long
conversation was told to come back tomorrow, which is simply false: their next
conversation would have been served immediately, and several testers took the
sentence at its word and left. Two sentences, both in the S4 register, are a
smaller compromise than one sentence that is untrue half the time. See
:data:`SESSION_STOP_MESSAGE`.
"""

SESSION_STOP_MESSAGE = "That's a good long conversation — start a new one to keep going"
"""What a visitor stopped by a *session* ceiling is told.

The same designed state and the same register as :data:`STOP_STATE_MESSAGE` --
no "quota", no apology, no 4xx -- differing only in the one respect where the
two stops genuinely differ: what the visitor can do about it. The session caps
end a conversation, not a day, so this sentence names the remedy that actually
works instead of one that costs the visitor until midnight.

It says nothing about which of the two session ceilings was reached. That
distinction lives on ``chip_chat.budget.scope`` and ``chip_chat.guard.reason``
for the operator, for the same reason
:attr:`StopReason.UPLOAD_RATE_LIMIT` keeps one sentence across two scopes:
telling somebody which counter ran out tells them which one to re-roll.
"""


class BudgetScope(StrEnum):
    """Values for ``chip_chat.budget.scope``: which ceiling was evaluated."""

    GLOBAL = "global"
    SESSION = "session"
    SOURCE_ADDRESS = "source_address"

    SESSION_UPLOADS = "session_uploads"
    """Uploads one conversation has made inside the upload window."""

    SOURCE_UPLOADS = "source_uploads"
    """Uploads one address has made inside the upload window.

    Distinct from :attr:`SESSION_UPLOADS` and from :attr:`SOURCE_ADDRESS`
    because the three count different things and are refused for different
    reasons -- and a dashboard that could not tell "this address is flooding
    uploads" from "this address is chatty" would report the cost attack as
    ordinary traffic.
    """


class StopReason(StrEnum):
    """Values for ``chip_chat.guard.reason``: which layer stopped the turn."""

    KILL_SWITCH = "kill_switch"
    """The manual circuit breaker is thrown. Beats every other consideration."""

    DAILY_CEILING = "daily_ceiling"
    """The global daily token ceiling is reached or would be crossed."""

    SESSION_TURN_CAP = "session_turn_cap"
    SESSION_TOKEN_CAP = "session_token_cap"
    SOURCE_RATE_LIMIT = "source_rate_limit"

    CONTENT_BLOCKED = "content_blocked"
    """Content Safety flagged the inbound message (#79).

    The conversation is not over: the visitor gets
    :data:`~chip_chat.api.moderation.BLOCKED_MESSAGE` and may say something
    else. This is the one stop reason that is not about a ceiling.
    """

    MODERATION_UNAVAILABLE = "moderation_unavailable"
    """Content Safety could not be reached, so the turn failed closed (#79).

    A separate token from :attr:`CONTENT_BLOCKED` on purpose. The visitor sees
    the same neutral sentence either way, but an operator reading traces has to
    be able to tell "somebody typed something we declined" from "our moderation
    has been down for an hour and every turn is being refused" -- and those two
    produce identical visitor-facing behaviour by design.
    """

    UPLOAD_RATE_LIMIT = "upload_rate_limit"
    """Too many uploads, from this session or from this address.

    One token for both scopes, deliberately: which of the two ceilings was hit
    is a fact for the operator, on ``chip_chat.budget.scope``, and telling an
    uploader whether the session or the address ran out would say which one to
    re-roll. What the visitor gets is :data:`STOP_STATE_MESSAGE` rather than
    :data:`SESSION_STOP_MESSAGE`, even though one of the two ceilings is the
    session's: see :data:`SESSION_SCOPED_REASONS` for why "start a new one" is
    the one piece of advice this particular limit must not give.
    """


SESSION_SCOPED_REASONS = frozenset(
    {StopReason.SESSION_TURN_CAP, StopReason.SESSION_TOKEN_CAP}
)
"""The reasons a *new conversation* fixes, which is what makes the copy differ.

Deliberately not "every reason whose scope is
:attr:`BudgetScope.SESSION`". :attr:`StopReason.UPLOAD_RATE_LIMIT` is
session-scoped too and is not in here, because it is a window rather than a
conversation: minting a fresh session is exactly the move that limit exists to
defeat, and inviting it in the copy would be inviting the flood. These two
reasons are the ones where starting again is the intended behaviour rather than
the evasion.
"""


def stop_message(reason: StopReason) -> str:
    """Return the sentence a visitor stopped for ``reason`` should be shown.

    Args:
        reason: Which layer stopped the turn.

    Returns:
        :data:`SESSION_STOP_MESSAGE` for the per-conversation caps,
        :data:`STOP_STATE_MESSAGE` for everything else.
    """
    return (
        SESSION_STOP_MESSAGE if reason in SESSION_SCOPED_REASONS else STOP_STATE_MESSAGE
    )


@dataclass(frozen=True, slots=True)
class Usage:
    """How close one ceiling is, as recorded on ``guard.budget_check``."""

    scope: BudgetScope
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        """Headroom left under this ceiling, never negative."""
        return max(0, self.limit - self.used)


@dataclass(frozen=True, slots=True)
class Stop:
    """A refusal: the turn does not happen and no model is called.

    The copy is derived from the reason rather than passed in. Every refusal in
    this package -- the ledger's, the guard's, the rate limiter's, the upload
    limiter's -- builds a :class:`Stop` with a reason and a usage and nothing
    else, so deriving the sentence here is what makes it impossible for a new
    refusal site to be added that forgets which of the two sentences it owes the
    visitor. A caller may still pass ``message`` explicitly; nothing in the
    request path does.
    """

    reason: StopReason
    usage: Usage
    message: str = ""

    def __post_init__(self) -> None:
        """Fill in the copy this reason implies, when the caller named none."""
        if not self.message:
            object.__setattr__(self, "message", stop_message(self.reason))
