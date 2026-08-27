"""The vocabulary of a stop: which ceiling, how close, and what the visitor sees.

The reason tokens below are a schema, not log text. They land on
``chip_chat.guard.reason`` and Phase 9's evaluations group on them, so they are
stable machine-comparable words rather than sentences -- the sentence is
:data:`STOP_STATE_MESSAGE`, and there is exactly one of those.
"""

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["STOP_STATE_MESSAGE", "BudgetScope", "Stop", "StopReason", "Usage"]

STOP_STATE_MESSAGE = "Cilantro's had a busy day — come back tomorrow"
"""What a stopped visitor is told, on entry and mid-conversation alike.

PRD requirement S4: this is a designed state, not an error. It is never
accompanied by a 4xx or 5xx status, never says "quota", and never apologises for
a failure -- because nothing failed. The cap worked.
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
    re-roll. What the visitor gets is :data:`STOP_STATE_MESSAGE`, the same
    designed state every other layer returns.
    """


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
    """A refusal: the turn does not happen and no model is called."""

    reason: StopReason
    usage: Usage
    message: str = STOP_STATE_MESSAGE
