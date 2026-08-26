"""Shared instrumentation for Chip Chat.

This package is the one shared library in the monorepo. Every other package may
import it; it imports none of them. That direction is enforced structurally by
the import-linter contract in the root ``pyproject.toml`` rather than by
convention, because retrofitting instrumentation is the mistake the build plan
warns about twice.
"""

from chip_chat.otel.service import SERVICE_NAMESPACE, service_name

__all__ = ["SERVICE_NAMESPACE", "__version__", "service_name"]

__version__ = "0.0.0"
