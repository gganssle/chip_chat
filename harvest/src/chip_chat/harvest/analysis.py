"""The Document Intelligence boundary, and the cache in front of it.

The framework's rule for someone else's website — fetch once, cache the raw
bytes, and let every later run read the cache — applies with more force to a
paid API than it does to a public page. So the same shape appears again here:
one narrow :class:`DocumentAnalyzer` seam that is the only thing in this
package which calls Azure, and an :class:`AnalysisCache` in front of it keyed
by the digest of the very bytes analysed.

Keying on the document's own SHA-256 rather than its URL is what makes the
cache correct across a re-harvest. A PDF republished unchanged has the same
digest and costs nothing to re-parse; a PDF that changed has a different one
and lands a new analysis *beside* the old, exactly as
:mod:`chip_chat.harvest.cache` lands a new body beside the old one. The model
and API version are part of the key too, because the same bytes read by a
later model are a different answer and must not be served from an earlier
one's entry.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.clock import Clock, SystemClock
from chip_chat.harvest.errors import DocumentAnalysisError
from chip_chat.harvest.layout import PDF_CONTENT_TYPE

DEFAULT_PREFIX = "analysis"
"""Root of the analysis cache. Beside ``raw/``, not inside it."""

DEFAULT_MODEL_ID = "prebuilt-layout"
"""The model issue #22 needs: layout and tables, not a trained document type."""

DEFAULT_API_VERSION = "2024-11-30"
"""The generally available Document Intelligence API version."""

DEFAULT_SCOPE = "https://cognitiveservices.azure.com/.default"
"""The token audience for every Azure AI services data plane."""

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_WAIT_SECONDS = 300.0
"""Ceiling on one document's analysis. A layout read of a nutrition sheet takes
seconds; five minutes means something is wrong and waiting longer will not fix
it."""


@dataclass(frozen=True, slots=True)
class DocumentAnalysis:
    """One Document Intelligence result, with what produced it attached.

    Attributes:
        content_sha256: Digest of the bytes that were analysed. The cache key,
            and the join back to the raw document in
            :class:`~chip_chat.harvest.cache.DocumentCache`.
        model_id: The model that read them.
        api_version: The API version it answered on.
        analyzed_at: When the analysis completed, timezone-aware and in UTC.
        result: The service's ``analyzeResult``, verbatim and unparsed. Stored
            whole for the same reason raw response bodies are: a parser change
            should cost a re-parse, not another paid call.
    """

    content_sha256: str
    model_id: str
    api_version: str
    analyzed_at: datetime
    result: Mapping[str, Any]


class DocumentAnalyzer(Protocol):
    """Reads a document and returns Document Intelligence's ``analyzeResult``.

    The only thing in this package that calls Azure. Tests inject a fake, which
    is what lets the whole PDF path — discovery, caching, table extraction,
    reconciliation — be tested without a subscription, and what makes the
    warm-cache assertion meaningful: an analyzer that is never called is proof
    where a mock would be a claim.
    """

    @property
    def model_id(self) -> str:
        """The model this analyzer uses."""
        ...

    @property
    def api_version(self) -> str:
        """The API version it calls."""
        ...

    def analyze(
        self, content: bytes, *, content_type: str = PDF_CONTENT_TYPE
    ) -> Mapping[str, Any]:
        """Analyse ``content`` and return the ``analyzeResult`` object.

        Raises:
            DocumentAnalysisError: If the service refused, failed, or did not
                finish.
        """
        ...

    def close(self) -> None:
        """Release any underlying connections."""
        ...


def default_token_provider(scope: str = DEFAULT_SCOPE) -> Callable[[], str]:
    """Return a callable handing out Entra ID bearer tokens for ``scope``.

    ``DefaultAzureCredential`` is what makes one code path work in both places
    this runs: the user-assigned managed identity Terraform grants *Cognitive
    Services User* to in the deployed container, and the developer's own
    ``az login`` locally. No key is read, and none is stored — the Document
    Intelligence account is reached over a role assignment, like every other
    resource in this estate.

    Args:
        scope: The token audience.

    Returns:
        A callable returning a bearer token, refreshing it as needed.

    Raises:
        DocumentAnalysisError: If ``azure-identity`` is not installed.
    """
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as error:  # pragma: no cover - exercised by absence, not by CI
        raise DocumentAnalysisError(
            "document intelligence", f"azure-identity is not installed: {error}"
        ) from error
    provider: Callable[[], str] = get_bearer_token_provider(
        DefaultAzureCredential(), scope
    )
    return provider


class AzureDocumentIntelligence:
    """The real analyzer: Azure Document Intelligence over its REST API.

    The call is asynchronous at the service — a POST that returns ``202`` and
    an operation URL, then polling until it succeeds — so the wait goes through
    a :class:`~chip_chat.harvest.clock.Clock` rather than :func:`time.sleep`,
    and a test can drive it without one.
    """

    def __init__(
        self,
        endpoint: str,
        token_provider: Callable[[], str] | None = None,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        api_version: str = DEFAULT_API_VERSION,
        clock: Clock | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        client: Any = None,
    ) -> None:
        """Initialise the analyzer.

        Args:
            endpoint: The account's endpoint, e.g.
                ``https://di-chip-chat-xxxx.cognitiveservices.azure.com``. It
                is a Terraform output, not a secret.
            token_provider: Where bearer tokens come from. Defaults to
                :func:`default_token_provider`, which is resolved on the first
                call rather than here, so that constructing an analyzer for a
                harvest that turns out to have no PDFs in it needs no
                credential at all.
            model_id: The Document Intelligence model.
            api_version: The API version to call.
            clock: Source of time and sleeping.
            poll_seconds: How long to wait between polls of the operation.
            timeout: Per-request timeout, in seconds.
            max_wait_seconds: How long one document may take before the
                analysis is abandoned.
            client: The HTTP client to use. Defaults to a pooled
                :class:`httpx.Client` opened on first use. It is a parameter
                so that the submit-and-poll loop — which is the only real
                logic in this class — can be tested without a subscription.

        Raises:
            ValueError: If ``endpoint`` is empty.
        """
        if not endpoint.strip():
            raise ValueError("a Document Intelligence endpoint is required")
        self._endpoint = endpoint.strip().rstrip("/")
        self._token_provider = token_provider
        self._model_id = model_id
        self._api_version = api_version
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._poll_seconds = poll_seconds
        self._timeout = timeout
        self._max_wait_seconds = max_wait_seconds
        self._client: Any = client

    @property
    def model_id(self) -> str:
        """The model this analyzer uses."""
        return self._model_id

    @property
    def api_version(self) -> str:
        """The API version it calls."""
        return self._api_version

    @property
    def analyze_url(self) -> str:
        """The URL a document is posted to."""
        return (
            f"{self._endpoint}/documentintelligence/documentModels/"
            f"{self._model_id}:analyze?api-version={self._api_version}"
        )

    def analyze(
        self, content: bytes, *, content_type: str = PDF_CONTENT_TYPE
    ) -> Mapping[str, Any]:
        """Analyse ``content`` and return the ``analyzeResult`` object.

        Args:
            content: The document bytes.
            content_type: What they are. ``application/pdf`` here.

        Returns:
            The ``analyzeResult`` object, verbatim.

        Raises:
            DocumentAnalysisError: If the service refused the document, the
                operation failed, or it did not finish inside
                ``max_wait_seconds``.
        """
        operation_url = self._submit(content, content_type)
        return self._await_result(operation_url)

    def close(self) -> None:
        """Close the underlying HTTP client, if one was ever opened."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "AzureDocumentIntelligence":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _http(self) -> Any:
        """Return the pooled client, opening it on first use."""
        if self._client is None:
            import httpx

            self._client = httpx.Client(follow_redirects=False)
        return self._client

    def _token(self) -> str:
        """Return a bearer token, resolving the default provider on first use."""
        if self._token_provider is None:
            self._token_provider = default_token_provider()
        return self._token_provider()

    def _submit(self, content: bytes, content_type: str) -> str:
        """POST the document and return the operation URL to poll."""
        import httpx

        try:
            response = self._http().post(
                self.analyze_url,
                content=content,
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "Content-Type": content_type,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise DocumentAnalysisError(
                self.analyze_url, f"{type(error).__name__}: {error}"
            ) from error
        if response.status_code != 202:
            raise DocumentAnalysisError(
                self.analyze_url,
                f"expected HTTP 202, got {response.status_code}: {response.text[:400]}",
            )
        operation_url = response.headers.get("operation-location")
        if not operation_url:
            raise DocumentAnalysisError(
                self.analyze_url, "accepted the document but returned no operation URL"
            )
        return str(operation_url)

    def _await_result(self, operation_url: str) -> Mapping[str, Any]:
        """Poll ``operation_url`` until it succeeds, fails, or runs out of time."""
        import httpx

        waited = 0.0
        while True:
            try:
                response = self._http().get(
                    operation_url,
                    headers={"Authorization": f"Bearer {self._token()}"},
                    timeout=self._timeout,
                )
            except httpx.HTTPError as error:
                raise DocumentAnalysisError(
                    operation_url, f"{type(error).__name__}: {error}"
                ) from error
            if response.status_code != 200:
                raise DocumentAnalysisError(
                    operation_url,
                    f"HTTP {response.status_code}: {response.text[:400]}",
                )
            payload = response.json()
            status = str(payload.get("status") or "")
            if status == "succeeded":
                result = payload.get("analyzeResult")
                if not isinstance(result, Mapping):
                    raise DocumentAnalysisError(
                        operation_url, "succeeded without an analyzeResult"
                    )
                return result
            if status in ("failed", "canceled"):
                raise DocumentAnalysisError(
                    operation_url, f"analysis {status}: {json.dumps(payload)[:400]}"
                )
            if waited >= self._max_wait_seconds:
                raise DocumentAnalysisError(
                    operation_url,
                    f"still {status!r} after {waited:g}s; giving up",
                )
            self._clock.sleep(self._poll_seconds)
            waited += self._poll_seconds


class AnalysisCache:
    """Document Intelligence results, stored beside the bytes they describe."""

    def __init__(self, blobs: BlobStore, prefix: str = DEFAULT_PREFIX) -> None:
        """Initialise the cache.

        Args:
            blobs: Where blobs are written. The same store the raw bytes
                landed in, under a different prefix.
            prefix: Key prefix for everything this cache owns.
        """
        self._blobs = blobs
        self._prefix = prefix.strip("/")

    def key(self, content_sha256: str, model_id: str, api_version: str) -> str:
        """Return the blob key holding one analysis.

        Args:
            content_sha256: Digest of the analysed bytes.
            model_id: The model that read them.
            api_version: The API version it answered on.

        Returns:
            The blob key.
        """
        return (
            f"{self._prefix}/{model_id}/{api_version}/"
            f"{content_sha256[:2]}/{content_sha256}.json"
        )

    def get(
        self, content_sha256: str, model_id: str, api_version: str
    ) -> DocumentAnalysis | None:
        """Return a stored analysis, or ``None`` if there is none.

        Args:
            content_sha256: Digest of the analysed bytes.
            model_id: The model wanted.
            api_version: The API version wanted.

        Returns:
            The analysis, result included.
        """
        raw = self._blobs.read(self.key(content_sha256, model_id, api_version))
        if raw is None:
            return None
        stored = json.loads(raw)
        return DocumentAnalysis(
            content_sha256=str(stored["content_sha256"]),
            model_id=str(stored["model_id"]),
            api_version=str(stored["api_version"]),
            analyzed_at=datetime.fromisoformat(str(stored["analyzed_at"])),
            result=stored["result"],
        )

    def put(self, analysis: DocumentAnalysis) -> DocumentAnalysis:
        """Store ``analysis``.

        Args:
            analysis: What to store.

        Returns:
            The analysis, unchanged.

        Raises:
            ValueError: If ``analyzed_at`` is naive.
        """
        if analysis.analyzed_at.tzinfo is None:
            raise ValueError("analyzed_at must be timezone-aware")
        payload = {
            "content_sha256": analysis.content_sha256,
            "model_id": analysis.model_id,
            "api_version": analysis.api_version,
            "analyzed_at": analysis.analyzed_at.isoformat(),
            "result": analysis.result,
        }
        self._blobs.write(
            self.key(analysis.content_sha256, analysis.model_id, analysis.api_version),
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )
        return analysis


def analyze_once(
    content: bytes,
    content_sha256: str,
    analyzer: DocumentAnalyzer,
    cache: AnalysisCache,
    *,
    clock: Clock | None = None,
    content_type: str = PDF_CONTENT_TYPE,
) -> DocumentAnalysis:
    """Return the analysis of ``content``, calling Azure only if it is not cached.

    Args:
        content: The document bytes.
        content_sha256: Their digest, already computed by the document cache.
        analyzer: The seam to Document Intelligence.
        cache: Where results are kept.
        clock: Source of ``analyzed_at``. Defaults to the system clock.
        content_type: What the bytes are.

    Returns:
        The analysis, from the cache where one exists.

    Raises:
        DocumentAnalysisError: If the service refused or failed.
    """
    cached = cache.get(content_sha256, analyzer.model_id, analyzer.api_version)
    if cached is not None:
        return cached
    now = (clock if clock is not None else SystemClock()).now()
    result = analyzer.analyze(content, content_type=content_type)
    return cache.put(
        DocumentAnalysis(
            content_sha256=content_sha256,
            model_id=analyzer.model_id,
            api_version=analyzer.api_version,
            analyzed_at=now,
            result=result,
        )
    )
