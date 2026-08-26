"""Terraform for all Azure resources."""

from chip_chat.otel import service_name

__all__ = ["SERVICE_NAME", "__version__"]

__version__ = "0.0.0"

SERVICE_NAME = service_name("infra")
"""OpenTelemetry ``service.name`` for this component."""
