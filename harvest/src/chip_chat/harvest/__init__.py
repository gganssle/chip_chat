"""Public menu, nutrition, and policy ingestion.

This package is the framework the Phase 1 harvesters sit on. It exists so that
the manners — reading ``robots.txt`` and obeying it, a politely slow request
rate, an honest User-Agent, and caching every response so a source is fetched
exactly once — are implemented here and inherited, rather than reimplemented
per source and quietly skipped by whichever one is in a hurry.

Typical use::

    from pathlib import Path

    from chip_chat.harvest import Harvester, HttpxTransport, LocalBlobStore

    with Harvester(LocalBlobStore(Path("landing")), HttpxTransport()) as harvester:
        menu = harvester.fetch_json("https://example.com/api/menu")

The second run of that program makes no network requests at all.
"""

from chip_chat.harvest.blobs import BlobStore, InMemoryBlobStore, LocalBlobStore
from chip_chat.harvest.cache import CachedDocument, DocumentCache, canonical_url
from chip_chat.harvest.clock import Clock, SystemClock
from chip_chat.harvest.errors import (
    CacheCorruptError,
    FetchError,
    HarvestError,
    PermanentFetchError,
    RobotsDisallowedError,
    TransientFetchError,
)
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import GLOBAL_GATE, PolitenessGate, RateLimiter
from chip_chat.harvest.robots import RobotsPolicy
from chip_chat.harvest.transport import (
    HttpResponse,
    HttpxTransport,
    Transport,
    build_user_agent,
)
from chip_chat.harvest.version import __version__
from chip_chat.otel import service_name

__all__ = [
    "GLOBAL_GATE",
    "SERVICE_NAME",
    "BlobStore",
    "CacheCorruptError",
    "CachedDocument",
    "Clock",
    "DocumentCache",
    "FetchError",
    "HarvestError",
    "Harvester",
    "HttpResponse",
    "HttpxTransport",
    "InMemoryBlobStore",
    "LocalBlobStore",
    "PermanentFetchError",
    "PolitenessGate",
    "RateLimiter",
    "RobotsDisallowedError",
    "RobotsPolicy",
    "SystemClock",
    "TransientFetchError",
    "Transport",
    "__version__",
    "build_user_agent",
    "canonical_url",
    "service_name",
]

SERVICE_NAME = service_name("harvest")
"""OpenTelemetry ``service.name`` for this component."""
