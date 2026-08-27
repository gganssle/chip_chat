"""Foundry agent definition and tool implementations.

All six read tools of RFC-001 §06 are implemented (#61), each against its own
backing service: retrieval in ``search/``, Cortex Analyst and the gold marts in
``snowflake/``, the photo path in ``vision/``. None of those services is built
here -- they arrive as a :class:`~chip_chat.agent.lanes.Lanes` value, and what
is absent from it is withdrawn from the model's tool list rather than left as a
tool nothing can answer.

A deployment with nothing wired is still the week-one slice of issue #16: a chat
loop over the hardcoded three-item menu in :mod:`chip_chat.agent.hardcoded`,
which says in every tool result what it is reading. The data is a placeholder
and says so. The *shapes* are not -- the span tree, the tool contracts and the
rule that a write needs a confirmation the model cannot grant itself all arrive
here in their final form, because those are the parts that are expensive to
change later.

:mod:`chip_chat.agent.foundry` is the other half and is not a placeholder at
all: it is where the deployments live and how the process authenticates.
"""

from chip_chat.agent.foundry import (
    COGNITIVE_SERVICES_SCOPE,
    FoundryConfig,
    FoundryConfigError,
    chat_client,
    credential,
)
from chip_chat.agent.hardcoded import (
    ACCOUNT,
    MENU,
    SIMULATION_NOTICE,
    STORE,
    Account,
    MenuItem,
    Store,
    menu_item,
    search_menu,
)
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.loop import (
    DEFAULT_MAX_STEPS,
    PROMPT_VERSION,
    RUNTIME_CONTEXT,
    SYSTEM_PROMPT,
    Conversation,
    TurnResult,
    run_turn,
)
from chip_chat.agent.model import (
    AzureChatModel,
    ChatModel,
    ModelReply,
    ToolInvocation,
    UnknownToolError,
)
from chip_chat.agent.orders import (
    Draft,
    DraftLine,
    OrderDesk,
    OrderRejectedError,
    Receipt,
    RejectionCode,
)
from chip_chat.agent.tools import TOOL_SCHEMAS, TOOLS, dispatch
from chip_chat.otel import service_name

__all__ = [
    "ACCOUNT",
    "COGNITIVE_SERVICES_SCOPE",
    "DEFAULT_MAX_STEPS",
    "MENU",
    "NO_LANES",
    "PROMPT_VERSION",
    "RUNTIME_CONTEXT",
    "SERVICE_NAME",
    "SIMULATION_NOTICE",
    "STORE",
    "SYSTEM_PROMPT",
    "TOOLS",
    "TOOL_SCHEMAS",
    "Account",
    "AzureChatModel",
    "ChatModel",
    "Conversation",
    "Draft",
    "DraftLine",
    "FoundryConfig",
    "FoundryConfigError",
    "Lanes",
    "MenuItem",
    "ModelReply",
    "OrderDesk",
    "OrderRejectedError",
    "Receipt",
    "RejectionCode",
    "Store",
    "ToolInvocation",
    "TurnResult",
    "UnknownToolError",
    "__version__",
    "chat_client",
    "credential",
    "dispatch",
    "menu_item",
    "run_turn",
    "search_menu",
    "service_name",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("agent")
"""OpenTelemetry ``service.name`` for this component."""
