"""FastAPI service, sessions, budget enforcement, ops API.

What lives here today is the spend cap, and it is here first on purpose. An open
URL with no login means anyone can drive tokens on the subscription, and
RFC-001 section 11 is explicit that the cap ships before the link is shared
rather than when a hardening checklist is finally reached.

The cap is a library rather than a middleware because the shape of the request
path is not settled yet. :class:`~chip_chat.api.guard.SpendGuard` drops into
whichever one arrives::

    guard = SpendGuard(SpendLimits.from_env(), kill_switch=any_of(
        EnvironmentKillSwitch(),
        FileKillSwitch("/mnt/ops/stop"),
    ))

    with chat_turn(session_id=sid, turn_index=n, message=text) as turn:
        with guard.turn(session_id=sid, source_address=ip) as budget:
            if not budget.allowed:
                turn.record_output(budget.message)
                return stop_state(budget.message)
            ...

Read :mod:`chip_chat.api.guard` before changing any of it. The one property the
whole module exists to hold is that a refusal happens *before* a model is
called, in the request path, synchronously.
"""

from chip_chat.api.clock import Clock, SystemClock
from chip_chat.api.guard import SpendGuard, TurnBudget
from chip_chat.api.killswitch import (
    KILL_SWITCH_VARIABLE,
    CachedKillSwitch,
    EnvironmentKillSwitch,
    FileKillSwitch,
    KillSwitch,
    ManualKillSwitch,
    any_of,
)
from chip_chat.api.ledger import BudgetLedger, Reservation
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import (
    STOP_STATE_MESSAGE,
    BudgetScope,
    Stop,
    StopReason,
    Usage,
)
from chip_chat.api.ratelimit import SourceRateLimiter
from chip_chat.api.uploads import UploadLimiter
from chip_chat.otel import service_name

__all__ = [
    "KILL_SWITCH_VARIABLE",
    "SERVICE_NAME",
    "STOP_STATE_MESSAGE",
    "BudgetLedger",
    "BudgetScope",
    "CachedKillSwitch",
    "Clock",
    "EnvironmentKillSwitch",
    "FileKillSwitch",
    "KillSwitch",
    "ManualKillSwitch",
    "Reservation",
    "SourceRateLimiter",
    "SpendGuard",
    "SpendLimits",
    "Stop",
    "StopReason",
    "SystemClock",
    "TurnBudget",
    "UploadLimiter",
    "Usage",
    "__version__",
    "any_of",
    "service_name",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("api")
"""OpenTelemetry ``service.name`` for this component."""
