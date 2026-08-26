"""Seeded synthetic account generator."""

from chip_chat.otel import service_name

__all__ = ["SERVICE_NAME", "__version__"]

__version__ = "0.0.0"

SERVICE_NAME = service_name("data-gen")
"""OpenTelemetry ``service.name`` for this component."""
