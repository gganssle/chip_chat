"""Test doubles for anything built on the harvest framework.

These ship with the package rather than living in ``tests/`` because the
harvesters in later issues need them too, and because a source-specific
harvester that had to hand-roll a fake transport would be tempted to test
against the live site instead.

None of them touches a network, a real clock, or a paid API. That is the
point: the one property this package exists to guarantee — that a warm cache
costs a site nothing, and a warm analysis cache costs Azure nothing — can only
be *proved* by doubles that record every call and are then asserted to have
recorded none.
"""

import hashlib
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from chip_chat.harvest.errors import DocumentAnalysisError
from chip_chat.harvest.transport import HttpResponse

EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
"""An arbitrary but fixed instant, so ``harvested_at`` is assertable."""


class FakeClock:
    """A clock the test drives."""

    def __init__(self, *, auto_advance: bool = True, start: datetime = EPOCH) -> None:
        """Initialise the clock.

        Args:
            auto_advance: When true, :meth:`sleep` moves time forward, which
                is what a sequential test wants. Concurrency tests set it
                false and freeze time, so that what a thread waited for is
                visible in the wait it was handed rather than in a racy
                timestamp.
            start: The wall-clock instant :meth:`now` starts from.
        """
        self.auto_advance = auto_advance
        self.sleeps: list[float] = []
        self._monotonic = 0.0
        self._now = start
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            if self.auto_advance:
                self._monotonic += seconds
                self._now += timedelta(seconds=seconds)

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        """Move both clocks forward without recording a sleep.

        Args:
            seconds: How far forward to move.
        """
        with self._lock:
            self._monotonic += seconds
            self._now += timedelta(seconds=seconds)


class RecordedRequest:
    """One call a :class:`FakeTransport` received.

    Attributes:
        url: The URL requested.
        headers: The headers the harvester sent.
        timeout: The timeout it asked for.
    """

    __slots__ = ("headers", "timeout", "url")

    def __init__(self, url: str, headers: Mapping[str, str], timeout: float) -> None:
        self.url = url
        self.headers = dict(headers)
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"RecordedRequest(url={self.url!r})"


class FakeTransport:
    """Serves canned responses and records every request it was asked to make."""

    def __init__(
        self,
        responses: Mapping[str, object] | None = None,
        default: HttpResponse | None = None,
    ) -> None:
        """Initialise the transport.

        Args:
            responses: URL to either one :class:`HttpResponse`, a list of them
                served in order (the last one repeating once the rest are
                used), or an :class:`Exception` to raise.
            default: What to serve for a URL that was not scripted. Defaults
                to a 404.
        """
        self.responses: dict[str, object] = dict(responses or {})
        self.default = default
        self.requests: list[RecordedRequest] = []
        self.closed = False
        self._lock = threading.Lock()

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float = 30.0,
    ) -> HttpResponse:
        """Record the request and return whatever was scripted for ``url``."""
        with self._lock:
            self.requests.append(RecordedRequest(url, headers, timeout))
            scripted = self.responses.get(url, self.default)
            if isinstance(scripted, list):
                scripted = scripted.pop(0) if len(scripted) > 1 else scripted[0]
        if isinstance(scripted, Exception):
            raise scripted
        if isinstance(scripted, HttpResponse):
            return scripted
        return HttpResponse(url=url, status_code=404, content=b"not scripted")

    def close(self) -> None:
        self.closed = True

    @property
    def urls(self) -> list[str]:
        """Every URL requested, in order."""
        return [request.url for request in self.requests]


def fake_response(
    url: str,
    body: bytes = b"{}",
    status_code: int = 200,
    content_type: str = "application/json",
    **headers: str,
) -> HttpResponse:
    """Build an :class:`HttpResponse` without spelling out every field.

    Args:
        url: The response's final URL.
        body: The raw body.
        status_code: The HTTP status.
        content_type: The ``Content-Type`` header.
        **headers: Any further response headers, lowercase.

    Returns:
        The response.
    """
    return HttpResponse(
        url=url,
        status_code=status_code,
        content=body,
        headers={"content-type": content_type, **headers},
    )


class FakeDocumentAnalyzer:
    """Serves canned Document Intelligence results and records every call.

    Scripted by the digest of the bytes handed to it rather than by a URL,
    because that is what the analysis cache keys on: a test that scripts one
    document and then asserts ``analyses == 1`` after two runs has proved the
    cache, not merely described it.
    """

    def __init__(
        self,
        results: Mapping[str, object] | None = None,
        *,
        model_id: str = "prebuilt-layout",
        api_version: str = "2024-11-30",
    ) -> None:
        """Initialise the analyzer.

        Args:
            results: Hex SHA-256 of the document bytes to either the
                ``analyzeResult`` mapping to return, or an exception to raise.
            model_id: The model this double claims to be.
            api_version: The API version it claims to answer on.
        """
        self.results: dict[str, object] = dict(results or {})
        self.analyses: list[str] = []
        self.closed = False
        self._model_id = model_id
        self._api_version = api_version

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def api_version(self) -> str:
        return self._api_version

    def analyze(
        self, content: bytes, *, content_type: str = "application/pdf"
    ) -> Mapping[str, Any]:
        """Record the call and return whatever was scripted for these bytes."""
        digest = hashlib.sha256(content).hexdigest()
        self.analyses.append(digest)
        scripted = self.results.get(digest)
        if isinstance(scripted, Exception):
            raise scripted
        if isinstance(scripted, Mapping):
            return scripted
        raise DocumentAnalysisError(digest[:12], "no result scripted for these bytes")

    def close(self) -> None:
        self.closed = True
