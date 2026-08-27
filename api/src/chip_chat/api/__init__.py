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

:mod:`chip_chat.api.drafts` is here for the same kind of reason. A draft's
``confirmed`` flag is what RFC-001 section 06's confirmation rule reads, so it
lives in the app tier where no tool argument and no model output can reach it --
and the ops API's refusal of an unconfirmed draft is then a fact about the
system rather than an instruction in a prompt.

:mod:`chip_chat.api.visitors` is the other half of the request path, and it is
where RFC-001 section 05's first clause lives: *identity originates in the app's
server-side session store*. It holds the store, the persona assignment that
fills it, and the roster read that makes an assigned account a populated one --
because the cold start is the product risk and an empty account is how this demo
dies. :mod:`chip_chat.api.pool` is the second clause, and the two meet at one
method: the pool resolves a session id against the store and nothing else in the
system can tell it whose data to return.

:mod:`chip_chat.api.ops` is the refusal itself, and the second launch gate.
Together with :mod:`chip_chat.api.confirmations` -- the same record for the three
writes that have no draft -- it is the only path in the system that writes, and
the only place the confirmation rule is enforced. ``api/functions/`` is the
Azure Functions host that runs it; nothing else in this package imports that
directory.
"""

from chip_chat.api.app import (
    SESSION_COOKIE,
    ChatReply,
    ChatRequest,
    EntryReply,
    EntryRequest,
    Service,
    SessionStore,
    VisitorProfile,
    build_service,
    build_visitors,
    create_app,
    default_kill_switch,
)
from chip_chat.api.clock import Clock, SystemClock
from chip_chat.api.confirmations import (
    DEFAULT_CONFIRMATION_TTL_SECONDS,
    Confirmation,
    ConfirmationCode,
    ConfirmationLedger,
    ConfirmationRejectedError,
    preferences_reference,
)
from chip_chat.api.drafts import (
    DEFAULT_DRAFT_TTL_SECONDS,
    Draft,
    DraftLine,
    DraftRejectedError,
    DraftStore,
    OrderType,
    RejectionCode,
    Selection,
)
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
from chip_chat.api.ops import (
    OPS_UNAVAILABLE_MESSAGE,
    SESSION_HEADER,
    OpsRejectedError,
    OpsService,
    OpsSession,
    OpsUnavailableError,
    Receipt,
    WriteBackend,
    WriteSession,
    offer_cancellation,
    offer_preferences,
    offer_redemption,
    unavailable_card,
)
from chip_chat.api.outcome import (
    STOP_STATE_MESSAGE,
    BudgetScope,
    Stop,
    StopReason,
    Usage,
)
from chip_chat.api.ratelimit import SourceRateLimiter
from chip_chat.api.turns import FundedTurn, SpendGate, UnfundedTurnError
from chip_chat.api.uploads import UploadLimiter
from chip_chat.api.visitors import (
    JOURNAL_VARIABLE,
    MAX_DISPLAY_NAME_CHARS,
    ROSTER_COLUMNS,
    FileJournal,
    NoJournal,
    PersonaFixture,
    PersonaRoster,
    SessionJournal,
    SnowflakeRoster,
    StaticRoster,
    VisitorDesk,
    VisitorSession,
    VisitorSessionStore,
    clean_display_name,
    journal_from_env,
)
from chip_chat.otel import service_name

__all__ = [
    "DEFAULT_CONFIRMATION_TTL_SECONDS",
    "DEFAULT_DRAFT_TTL_SECONDS",
    "JOURNAL_VARIABLE",
    "KILL_SWITCH_VARIABLE",
    "MAX_DISPLAY_NAME_CHARS",
    "OPS_UNAVAILABLE_MESSAGE",
    "ROSTER_COLUMNS",
    "SERVICE_NAME",
    "SESSION_COOKIE",
    "SESSION_HEADER",
    "STOP_STATE_MESSAGE",
    "BudgetLedger",
    "BudgetScope",
    "CachedKillSwitch",
    "ChatReply",
    "ChatRequest",
    "Clock",
    "Confirmation",
    "ConfirmationCode",
    "ConfirmationLedger",
    "ConfirmationRejectedError",
    "Draft",
    "DraftLine",
    "DraftRejectedError",
    "DraftStore",
    "EntryReply",
    "EntryRequest",
    "EnvironmentKillSwitch",
    "FileJournal",
    "FileKillSwitch",
    "FundedTurn",
    "KillSwitch",
    "ManualKillSwitch",
    "NoJournal",
    "OpsRejectedError",
    "OpsService",
    "OpsSession",
    "OpsUnavailableError",
    "OrderType",
    "PersonaFixture",
    "PersonaRoster",
    "Receipt",
    "RejectionCode",
    "Reservation",
    "Selection",
    "Service",
    "SessionJournal",
    "SessionStore",
    "SnowflakeRoster",
    "SourceRateLimiter",
    "SpendGate",
    "SpendGuard",
    "SpendLimits",
    "StaticRoster",
    "Stop",
    "StopReason",
    "SystemClock",
    "TurnBudget",
    "UnfundedTurnError",
    "UploadLimiter",
    "Usage",
    "VisitorDesk",
    "VisitorProfile",
    "VisitorSession",
    "VisitorSessionStore",
    "WriteBackend",
    "WriteSession",
    "__version__",
    "any_of",
    "build_service",
    "build_visitors",
    "clean_display_name",
    "create_app",
    "default_kill_switch",
    "journal_from_env",
    "offer_cancellation",
    "offer_preferences",
    "offer_redemption",
    "preferences_reference",
    "service_name",
    "unavailable_card",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("api")
"""OpenTelemetry ``service.name`` for this component."""
