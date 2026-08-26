"""The analysis cache, and the one class that actually calls Azure.

Two things are being proved. The first is the bargain the whole package is
built on, struck a second time with a paid API rather than a stranger's web
server: a document already read is never sent again. The second is that the
submit-and-poll dance the service requires is driven by an injected clock and
an injected client, so that neither a subscription nor a real second is needed
to test it.
"""

import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from chip_chat.harvest.analysis import (
    DEFAULT_API_VERSION,
    DEFAULT_MODEL_ID,
    AnalysisCache,
    AzureDocumentIntelligence,
    DocumentAnalysis,
    analyze_once,
)
from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.errors import DocumentAnalysisError
from chip_chat.harvest.testing import EPOCH, FakeClock, FakeDocumentAnalyzer

PDF = b"%PDF-1.4 pretend"
DIGEST = hashlib.sha256(PDF).hexdigest()
RESULT: dict[str, Any] = {
    "modelId": DEFAULT_MODEL_ID,
    "apiVersion": DEFAULT_API_VERSION,
    "pages": [{"pageNumber": 1}],
    "tables": [],
}


@pytest.fixture
def cache() -> AnalysisCache:
    """An empty analysis cache over an in-memory store."""
    return AnalysisCache(InMemoryBlobStore())


def test_the_key_names_the_model_and_the_version_as_well_as_the_bytes(
    cache: AnalysisCache,
) -> None:
    key = cache.key(DIGEST, "prebuilt-layout", "2024-11-30")
    assert key == f"analysis/prebuilt-layout/2024-11-30/{DIGEST[:2]}/{DIGEST}.json"


def test_an_analysis_survives_a_round_trip(cache: AnalysisCache) -> None:
    stored = cache.put(
        DocumentAnalysis(DIGEST, DEFAULT_MODEL_ID, DEFAULT_API_VERSION, EPOCH, RESULT)
    )
    read = cache.get(DIGEST, DEFAULT_MODEL_ID, DEFAULT_API_VERSION)
    assert read is not None
    assert read == stored
    assert read.result == RESULT


def test_nothing_cached_reads_as_nothing(cache: AnalysisCache) -> None:
    assert cache.get(DIGEST, DEFAULT_MODEL_ID, DEFAULT_API_VERSION) is None


def test_a_naive_timestamp_is_refused(cache: AnalysisCache) -> None:
    naive = datetime(2026, 8, 26, 12, 0, 0)  # A naive one is the point of the test.
    with pytest.raises(ValueError, match="timezone-aware"):
        cache.put(
            DocumentAnalysis(DIGEST, DEFAULT_MODEL_ID, DEFAULT_API_VERSION, naive, RESULT)
        )


def test_a_document_already_read_is_never_sent_again(cache: AnalysisCache) -> None:
    """The warm-cache claim, proved by an analyzer that recorded one call."""
    analyzer = FakeDocumentAnalyzer({DIGEST: RESULT})
    clock = FakeClock()

    first = analyze_once(PDF, DIGEST, analyzer, cache, clock=clock)
    second = analyze_once(PDF, DIGEST, analyzer, cache, clock=clock)

    assert analyzer.analyses == [DIGEST]
    assert first == second
    assert first.analyzed_at == EPOCH


def test_the_same_bytes_read_by_a_later_model_are_a_separate_entry(
    cache: AnalysisCache,
) -> None:
    """A newer model's answer must not be served out of an older one's entry."""
    old = FakeDocumentAnalyzer({DIGEST: RESULT}, api_version="2024-11-30")
    new = FakeDocumentAnalyzer({DIGEST: RESULT}, api_version="2099-01-01")

    analyze_once(PDF, DIGEST, old, cache, clock=FakeClock())
    analyze_once(PDF, DIGEST, new, cache, clock=FakeClock())

    assert old.analyses == [DIGEST]
    assert new.analyses == [DIGEST]


def test_an_analyzer_with_nothing_scripted_says_so() -> None:
    with pytest.raises(DocumentAnalysisError):
        FakeDocumentAnalyzer().analyze(PDF)


class _StubResponse:
    """One canned HTTP response, in the shape ``httpx`` returns."""

    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _StubClient:
    """An HTTP client that serves a script and records what it was asked."""

    def __init__(self, post: _StubResponse, gets: list[_StubResponse]) -> None:
        self.post_calls: list[tuple[str, bytes, dict[str, str]]] = []
        self.get_calls: list[str] = []
        self.closed = False
        self._post = post
        self._gets = gets

    def post(
        self, url: str, *, content: bytes, headers: dict[str, str], timeout: float
    ) -> _StubResponse:
        self.post_calls.append((url, content, headers))
        return self._post

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> _StubResponse:
        self.get_calls.append(url)
        return self._gets.pop(0) if len(self._gets) > 1 else self._gets[0]

    def close(self) -> None:
        self.closed = True


OPERATION = "https://di.example.test/documentintelligence/analyzeResults/abc"


def _accepted() -> _StubResponse:
    return _StubResponse(202, headers={"operation-location": OPERATION})


def _analyzer(
    client: _StubClient, clock: FakeClock | None = None
) -> AzureDocumentIntelligence:
    return AzureDocumentIntelligence(
        "https://di.example.test/",
        lambda: "a-token",
        clock=clock or FakeClock(),
        client=client,
    )


def test_the_document_is_posted_to_the_model_and_polled_until_it_succeeds() -> None:
    client = _StubClient(
        _accepted(),
        [
            _StubResponse(200, payload={"status": "running"}),
            _StubResponse(200, payload={"status": "succeeded", "analyzeResult": RESULT}),
        ],
    )
    clock = FakeClock()

    assert _analyzer(client, clock).analyze(PDF) == RESULT

    url, body, headers = client.post_calls[0]
    assert url == (
        "https://di.example.test/documentintelligence/documentModels/"
        "prebuilt-layout:analyze?api-version=2024-11-30"
    )
    assert body == PDF
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Authorization"] == "Bearer a-token"
    assert client.get_calls == [OPERATION, OPERATION]
    assert clock.sleeps == [1.0]


def test_a_refused_document_raises_rather_than_returning_nothing() -> None:
    client = _StubClient(_StubResponse(400, text="InvalidContent"), [])
    with pytest.raises(DocumentAnalysisError, match="expected HTTP 202"):
        _analyzer(client).analyze(PDF)


def test_an_acceptance_with_no_operation_url_raises() -> None:
    client = _StubClient(_StubResponse(202), [])
    with pytest.raises(DocumentAnalysisError, match="no operation URL"):
        _analyzer(client).analyze(PDF)


def test_a_failed_analysis_raises_rather_than_looping_for_ever() -> None:
    client = _StubClient(_accepted(), [_StubResponse(200, payload={"status": "failed"})])
    with pytest.raises(DocumentAnalysisError, match="analysis failed"):
        _analyzer(client).analyze(PDF)


def test_a_success_without_a_result_is_not_treated_as_a_success() -> None:
    client = _StubClient(
        _accepted(), [_StubResponse(200, payload={"status": "succeeded"})]
    )
    with pytest.raises(DocumentAnalysisError, match="without an analyzeResult"):
        _analyzer(client).analyze(PDF)


def test_an_analysis_that_never_finishes_is_abandoned() -> None:
    client = _StubClient(_accepted(), [_StubResponse(200, payload={"status": "running"})])
    analyzer = AzureDocumentIntelligence(
        "https://di.example.test",
        lambda: "a-token",
        clock=FakeClock(),
        poll_seconds=1.0,
        max_wait_seconds=3.0,
        client=client,
    )
    with pytest.raises(DocumentAnalysisError, match="giving up"):
        analyzer.analyze(PDF)
    assert len(client.get_calls) == 4


def test_an_endpoint_is_required() -> None:
    with pytest.raises(ValueError, match="endpoint is required"):
        AzureDocumentIntelligence("   ")


def test_closing_releases_the_client() -> None:
    client = _StubClient(_accepted(), [])
    analyzer = _analyzer(client)
    analyzer.close()
    assert client.closed


def test_a_run_that_analyses_nothing_never_asks_for_a_credential() -> None:
    """Constructing the analyzer must not authenticate.

    Chipotle publishes no PDFs, so every run today builds an analyzer and
    never uses it. If that cost a token, a developer with no Azure
    subscription could not build the other three datasets.
    """
    analyzer = AzureDocumentIntelligence("https://di.example.test")
    assert analyzer.model_id == DEFAULT_MODEL_ID
    assert analyzer.api_version == DEFAULT_API_VERSION
    analyzer.close()


def test_the_analysis_records_when_it_was_read(cache: AnalysisCache) -> None:
    clock = FakeClock(start=datetime(2026, 8, 26, 5, 35, tzinfo=UTC))
    analysis = analyze_once(
        PDF, DIGEST, FakeDocumentAnalyzer({DIGEST: RESULT}), cache, clock=clock
    )
    assert analysis.analyzed_at == datetime(2026, 8, 26, 5, 35, tzinfo=UTC)
    assert analysis.model_id == DEFAULT_MODEL_ID
