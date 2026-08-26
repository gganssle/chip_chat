"""Foundry agent definition and tool implementations."""

from chip_chat.otel import service_name

__all__ = ["SERVICE_NAME", "__version__"]

__version__ = "0.0.0"

SERVICE_NAME = service_name("agent")
"""OpenTelemetry ``service.name`` for this component."""
