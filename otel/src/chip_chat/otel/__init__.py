"""Shared instrumentation for Chip Chat.

This package is the one shared library in the monorepo. Every other package may
import it; it imports none of them. That direction is enforced structurally by
the import-linter contract in the root ``pyproject.toml`` rather than by
convention, because retrofitting instrumentation is the mistake the build plan
warns about twice.

What it holds is a *schema*. RFC-001 section 09 fixes the span tree a turn emits,
and :mod:`chip_chat.otel.schema` is that tree in executable form. Phase 9's
evaluations and every dashboard axis attach to these names, so they are not a
debugging convenience a call site may improvise on. The context managers in
:mod:`chip_chat.otel.spans` are the only way to open a span, no tracer leaves
this package, and each helper refuses to open its node in the wrong place --
between them there is no route from application code to an off-schema span.

Two backends, one instrumentation: Application Insights answers whether the
service is healthy and Phoenix (later Arize AX) answers whether the agent is
behaving. Which product answers on the OTLP endpoint is a configuration value
and nothing in :mod:`chip_chat.otel.exporters` knows either name.

``otel/README.md`` is the schema of record. Read it before adding a span.
"""

from chip_chat.otel.attributes import (
    ChipChatAttributes,
    ConfirmationState,
    DbAttributes,
    GuardOutcome,
)
from chip_chat.otel.config import TelemetryConfig
from chip_chat.otel.exporters import build_span_exporters
from chip_chat.otel.schema import (
    OPS_SPAN_PREFIX,
    SPAN_NAMES,
    TOOL_SPAN_PREFIX,
    WRITE_TOOLS,
    OpsAction,
    SpanName,
    ToolName,
    allowed_parents,
    ops_span_name,
    span_kind,
    tool_span_name,
)
from chip_chat.otel.service import SERVICE_NAMESPACE, service_name
from chip_chat.otel.spans import (
    Document,
    Message,
    SpanSchemaError,
    TurnIdentity,
    agent_step,
    budget_check,
    chat_turn,
    content_safety,
    cortex_analyst_query,
    current_turn,
    llm_completion,
    matcher_resolve,
    ops_write,
    render_response,
    retriever_search,
    tool_call,
    vision_describe,
)
from chip_chat.otel.tracing import (
    build_tracer_provider,
    configure_tracing,
    shutdown_tracing,
)

__all__ = [
    "OPS_SPAN_PREFIX",
    "SERVICE_NAMESPACE",
    "SPAN_NAMES",
    "TOOL_SPAN_PREFIX",
    "WRITE_TOOLS",
    "ChipChatAttributes",
    "ConfirmationState",
    "DbAttributes",
    "Document",
    "GuardOutcome",
    "Message",
    "OpsAction",
    "SpanName",
    "SpanSchemaError",
    "TelemetryConfig",
    "ToolName",
    "TurnIdentity",
    "__version__",
    "agent_step",
    "allowed_parents",
    "budget_check",
    "build_span_exporters",
    "build_tracer_provider",
    "chat_turn",
    "configure_tracing",
    "content_safety",
    "cortex_analyst_query",
    "current_turn",
    "llm_completion",
    "matcher_resolve",
    "ops_span_name",
    "ops_write",
    "render_response",
    "retriever_search",
    "service_name",
    "shutdown_tracing",
    "span_kind",
    "tool_call",
    "tool_span_name",
    "vision_describe",
]

__version__ = "0.0.0"
