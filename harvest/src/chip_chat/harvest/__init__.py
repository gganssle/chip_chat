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

from chip_chat.harvest.analysis import (
    AnalysisCache,
    AzureDocumentIntelligence,
    DocumentAnalysis,
    DocumentAnalyzer,
    analyze_once,
    default_token_provider,
)
from chip_chat.harvest.blobs import BlobStore, InMemoryBlobStore, LocalBlobStore
from chip_chat.harvest.cache import CachedDocument, DocumentCache, canonical_url
from chip_chat.harvest.changes import (
    DocumentChange,
    RowChange,
    TableChange,
    TableSnapshot,
    diff_documents,
    diff_tables,
    render_report,
    snapshot_documents,
    snapshot_tables,
)
from chip_chat.harvest.clock import Clock, SystemClock
from chip_chat.harvest.errors import (
    CacheCorruptError,
    DocumentAnalysisError,
    FetchError,
    HarvestError,
    PermanentFetchError,
    RobotsDisallowedError,
    TransientFetchError,
)
from chip_chat.harvest.freshness import (
    DEFAULT_MAX_AGE_DAYS,
    CorpusFreshness,
    DocumentAge,
    read_freshness,
)
from chip_chat.harvest.harvester import Harvester, conditional_headers
from chip_chat.harvest.layout import (
    PDF_CONTENT_TYPE,
    LayoutCell,
    LayoutDocument,
    LayoutTable,
    is_pdf,
    parse_layout,
)
from chip_chat.harvest.ratelimit import GLOBAL_GATE, PolitenessGate, RateLimiter
from chip_chat.harvest.release import (
    Release,
    ReleaseError,
    ReleaseStore,
    read_current,
    run_id_for,
)
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
    "DEFAULT_MAX_AGE_DAYS",
    "GLOBAL_GATE",
    "PDF_CONTENT_TYPE",
    "SERVICE_NAME",
    "AnalysisCache",
    "AzureDocumentIntelligence",
    "BlobStore",
    "CacheCorruptError",
    "CachedDocument",
    "Clock",
    "CorpusFreshness",
    "DocumentAge",
    "DocumentAnalysis",
    "DocumentAnalysisError",
    "DocumentAnalyzer",
    "DocumentCache",
    "DocumentChange",
    "FetchError",
    "HarvestError",
    "Harvester",
    "HttpResponse",
    "HttpxTransport",
    "InMemoryBlobStore",
    "LayoutCell",
    "LayoutDocument",
    "LayoutTable",
    "LocalBlobStore",
    "PermanentFetchError",
    "PolitenessGate",
    "RateLimiter",
    "Release",
    "ReleaseError",
    "ReleaseStore",
    "RobotsDisallowedError",
    "RobotsPolicy",
    "RowChange",
    "SystemClock",
    "TableChange",
    "TableSnapshot",
    "TransientFetchError",
    "Transport",
    "__version__",
    "analyze_once",
    "build_user_agent",
    "canonical_url",
    "conditional_headers",
    "default_token_provider",
    "diff_documents",
    "diff_tables",
    "is_pdf",
    "parse_layout",
    "read_current",
    "read_freshness",
    "render_report",
    "run_id_for",
    "service_name",
    "snapshot_documents",
    "snapshot_tables",
]

SERVICE_NAME = service_name("harvest")
"""OpenTelemetry ``service.name`` for this component."""
