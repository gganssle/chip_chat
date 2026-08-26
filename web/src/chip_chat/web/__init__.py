"""Chat widget and entry flow.

One page, no build step, and every sentence on it in :mod:`chip_chat.web.copy`.
:mod:`chip_chat.api.app` serves what is here; nothing in this package knows how
a request reaches it.
"""

from chip_chat.otel import service_name
from chip_chat.web.copy import (
    BANNER,
    OPENING_MESSAGE,
    STOP_STATE_HEADING,
    SUGGESTIONS,
    TITLE,
)
from chip_chat.web.page import chat_page, stop_page
from chip_chat.web.persona import ACCOUNT_SUMMARY

__all__ = [
    "ACCOUNT_SUMMARY",
    "BANNER",
    "OPENING_MESSAGE",
    "SERVICE_NAME",
    "STOP_STATE_HEADING",
    "SUGGESTIONS",
    "TITLE",
    "__version__",
    "chat_page",
    "service_name",
    "stop_page",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("web")
"""OpenTelemetry ``service.name`` for this component."""
