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


class StopReason(StrEnum):
    """Values for ``chip_chat.guard.reason``: which layer stopped the turn."""

    KILL_SWITCH = "kill_switch"
    """The manual circuit breaker is thrown. Beats every other consideration."""

    DAILY_CEILING = "daily_ceiling"
    """The global daily token ceiling is reached or would be crossed."""

    SESSION_TURN_CAP = "session_turn_cap"
    SESSION_TOKEN_CAP = "session_token_cap"
    SOURCE_RATE_LIMIT = "source_rate_limit"


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
