"""FastAPI service, sessions, budget enforcement, ops API.

Two halves that only make sense together. The spend cap
(:mod:`chip_chat.api.guard` and what it is built from) arrived first, on
purpose: an open URL with no login means anyone can drive tokens on the
subscription, and RFC-001 section 11 is explicit that the cap ships before the
link is shared rather than when a hardening checklist is finally reached.

:mod:`chip_chat.api.app` is the request path it stands inside, and
:mod:`chip_chat.api.turns` is what makes the wiring structural rather than
remembered. That distinction is the whole value of this package: a guard nothing
calls stops nobody spending anything, and "the cap is in place" has to be true
of the running system and not only of the code.

    with chat_turn(session_id=sid, turn_index=n, message=text) as turn:
        with gate.turn(session_id=sid, source_address=ip) as funded:
            if isinstance(funded, Stop):
                return stop_state(funded.message)
            result = funded.run(conversation, text)   # the only route to a model

Read :mod:`chip_chat.api.guard` for the four layers and
:mod:`chip_chat.api.turns` for why there is no second route past them. The one
property the whole package exists to hold is that a refusal happens *before* a
model is called, in the request path, synchronously.
"""

from chip_chat.api.app import (
    SESSION_COOKIE,
    ChatReply,
    ChatRequest,
    Service,
    SessionStore,
    build_service,
    create_app,
    default_kill_switch,
)
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
from chip_chat.api.turns import FundedTurn, SpendGate, UnfundedTurnError
from chip_chat.otel import service_name

__all__ = [
    "KILL_SWITCH_VARIABLE",
    "SERVICE_NAME",
    "SESSION_COOKIE",
    "STOP_STATE_MESSAGE",
    "BudgetLedger",
    "BudgetScope",
    "CachedKillSwitch",
    "ChatReply",
    "ChatRequest",
    "Clock",
    "EnvironmentKillSwitch",
    "FileKillSwitch",
    "FundedTurn",
    "KillSwitch",
    "ManualKillSwitch",
    "Reservation",
    "Service",
    "SessionStore",
    "SourceRateLimiter",
    "SpendGate",
    "SpendGuard",
    "SpendLimits",
    "Stop",
    "StopReason",
    "SystemClock",
    "TurnBudget",
    "UnfundedTurnError",
    "Usage",
    "__version__",
    "any_of",
    "build_service",
    "create_app",
    "default_kill_switch",
    "service_name",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("api")
"""OpenTelemetry ``service.name`` for this component."""
