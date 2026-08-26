"""The framework every Chip Chat harvester runs through.

The order of operations is the whole point, so it is stated once here and
implemented once below:

1. Consult ``robots.txt`` for the origin. If it disallows the path, refuse.
2. Look in the cache. A hit returns without touching the network — which is
   what makes a warm re-run cost the site nothing at all. A *refresh* instead
   offers the cached ``ETag`` and ``Last-Modified`` back as conditional
   headers, so a weekly re-harvest of an unchanged page costs the site a 304
   and no body.
3. Wait for the politeness gate: a real delay since the last request, and a
   process-wide ceiling on requests in flight.
4. Fetch, retrying transient failures with backoff and never retrying a 4xx.
5. Write the raw bytes to the landing zone with ``source_url`` and
   ``harvested_at`` attached, and return them.

No harvester in a later issue re-implements any of this, which is the reason
the politeness controls live down here rather than up in each source.
"""

import threading
from collections.abc import Mapping
from types import TracebackType
from typing import Any

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.cache import CachedDocument, DocumentCache, canonical_url
from chip_chat.harvest.clock import Clock, SystemClock
from chip_chat.harvest.errors import (
    FetchError,
    PermanentFetchError,
    RobotsDisallowedError,
    TransientFetchError,
)
from chip_chat.harvest.ratelimit import GLOBAL_GATE, PolitenessGate
from chip_chat.harvest.robots import (
    RobotsPolicy,
    origin_of,
    policy_from_response,
    robots_url_for,
)
from chip_chat.harvest.transport import (
    DEFAULT_CONTACT,
    DEFAULT_TIMEOUT_SECONDS,
    HttpResponse,
    Transport,
    build_user_agent,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_MAX_BACKOFF_SECONDS = 60.0
DEFAULT_ROBOTS_MAX_AGE_SECONDS = 24 * 60 * 60
"""How long a cached ``robots.txt`` is trusted. A day, so a site that changes
its mind is honoured within a day rather than never."""

JSON_ACCEPT = "application/json, text/javascript;q=0.9, */*;q=0.1"

NOT_MODIFIED = 304
"""The whole reason issue #38's weekly re-harvest is not a weekly re-download."""


def conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    """Return the request headers that ask "only if it changed".

    Both validators are offered when both are known. They are not redundant:
    an ``ETag`` is exact but some CDNs vary it per edge, and a
    ``Last-Modified`` date is coarse but stable, so a server that ignores one
    may still honour the other. RFC 9110 has the server prefer ``If-None-Match``
    where it understands both, which is the behaviour we want.

    Args:
        etag: The stored ``ETag``, or ``None``.
        last_modified: The stored ``Last-Modified``, or ``None``.

    Returns:
        Zero, one or two headers. An empty mapping means there is nothing to
        be conditional about, and the caller should ask unconditionally.
    """
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def _retry_after_seconds(response: HttpResponse) -> float | None:
    """Return the ``Retry-After`` delay in seconds, if the server gave a usable one."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class Harvester:
    """Fetches public documents politely, and only ever once.

    Attributes:
        user_agent: What this harvester tells a site it is.
        requests_made: Every request that reached the transport, retries and
            ``robots.txt`` included.
        revalidations: How many of those were answered 304 — a page confirmed
            unchanged without its body being sent again. Issue #38's claim
            that a re-harvest "does not re-fetch what has not changed" is this
            number set against ``requests_made``, which is why it is counted
            rather than described.
        bytes_fetched: Response bodies stored, in bytes. A revalidation adds
            nothing to it, which is the point.
    """

    def __init__(
        self,
        blobs: BlobStore,
        transport: Transport,
        *,
        clock: Clock | None = None,
        contact: str = DEFAULT_CONTACT,
        gate: PolitenessGate | None = None,
        cache: DocumentCache | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        robots_max_age_seconds: float = DEFAULT_ROBOTS_MAX_AGE_SECONDS,
    ) -> None:
        """Initialise the harvester.

        Args:
            blobs: The raw landing zone.
            transport: The HTTP boundary. The only thing here that touches a
                network.
            clock: Source of time and sleeping. Defaults to the system clock.
            contact: Address a site owner can reach us at; goes in the
                User-Agent.
            gate: The politeness gate. Defaults to the process-wide
                :data:`~chip_chat.harvest.ratelimit.GLOBAL_GATE`, so separate
                harvesters share one rate limit and one concurrency ceiling.
            cache: Where raw responses are stored. Defaults to a
                :class:`~chip_chat.harvest.cache.DocumentCache` over ``blobs``.
            timeout: Per-request timeout, in seconds.
            max_attempts: How many times a transient failure is retried,
                including the first attempt.
            backoff_seconds: Delay before the second attempt.
            backoff_multiplier: Factor the delay grows by each attempt.
            max_backoff_seconds: Ceiling on any single backoff delay.
            robots_max_age_seconds: How long a cached ``robots.txt`` is
                trusted before it is read again.

        Raises:
            ValueError: If ``max_attempts`` is less than one, or ``contact``
                is empty.
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        self.user_agent = build_user_agent(contact)
        self.requests_made = 0
        self.revalidations = 0
        self.bytes_fetched = 0
        self._transport = transport
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._gate = gate if gate is not None else GLOBAL_GATE
        self._cache = cache if cache is not None else DocumentCache(blobs)
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._backoff_multiplier = backoff_multiplier
        self._max_backoff_seconds = max_backoff_seconds
        self._robots_max_age_seconds = robots_max_age_seconds
        self._policies: dict[str, RobotsPolicy] = {}
        self._policy_lock = threading.RLock()
        self._counter_lock = threading.Lock()

    @property
    def cache(self) -> DocumentCache:
        """The document cache this harvester reads and writes."""
        return self._cache

    def fetch(
        self,
        url: str,
        *,
        refresh: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> CachedDocument:
        """Return the document at ``url``, fetching it only if it is not cached.

        Args:
            url: An absolute ``http`` or ``https`` URL.
            refresh: Ask the server again even on a cache hit. The request is
                *conditional* where the stored pointer carries an ``ETag`` or
                a ``Last-Modified``, so an unchanged page is answered with a
                304 and no body; the previous body stays in the store either
                way, so the two can be diffed.
            headers: Extra request headers. ``User-Agent`` cannot be
                overridden, and neither can the conditional headers a refresh
                supplies.

        Returns:
            The cached document, carrying ``source_url`` and ``harvested_at``.

        Returns from cache without any network activity whenever the URL has
        been fetched before and ``refresh`` is false.

        Raises:
            RobotsDisallowedError: If the site's ``robots.txt`` forbids the path.
            PermanentFetchError: On a 4xx or an unfollowed redirect.
            TransientFetchError: If every attempt failed.
            ValueError: If ``url`` is not absolute.
        """
        target = canonical_url(url)
        policy = self._policy_for(target)
        if not policy.can_fetch(self.user_agent, target):
            raise RobotsDisallowedError(target, self.user_agent)

        if not refresh:
            cached = self._cache.get(target)
            if cached is not None:
                return cached
            return self._fetch_and_store(target, headers)

        etag, last_modified = self._cache.validators(target)
        conditional = conditional_headers(etag, last_modified)
        if not conditional:
            return self._fetch_and_store(target, headers)

        merged = {**dict(headers or {}), **conditional}
        response = self._request(target, merged, accept_not_modified=True)
        if response.status_code != NOT_MODIFIED:
            return self._store(target, response)

        with self._counter_lock:
            self.revalidations += 1
        revalidated = self._cache.touch(target, self._clock.now())
        if revalidated is not None:
            return revalidated
        # A 304 with nothing to revalidate. Only reachable if the pointer was
        # deleted between reading its validators and recording the answer, so
        # the honest response is to go and get the bytes rather than to invent
        # a document.
        return self._fetch_and_store(target, headers)

    def _fetch_and_store(
        self, target: str, headers: Mapping[str, str] | None
    ) -> CachedDocument:
        """Fetch ``target`` unconditionally and store what came back."""
        return self._store(target, self._request(target, headers))

    def _store(self, target: str, response: HttpResponse) -> CachedDocument:
        """Store one response, counting the bytes the site had to send us."""
        with self._counter_lock:
            self.bytes_fetched += len(response.content)
        return self._cache.put(target, response, self._clock.now())

    def fetch_json(
        self,
        url: str,
        *,
        refresh: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Fetch ``url`` asking for JSON, and return the parsed body.

        Preferring the JSON endpoints a site's own front end calls is the
        first thing to look for in a new source: they are more stable than the
        markup around them, far lighter to fetch, and they arrive structured
        instead of scraped.

        Args:
            url: An absolute URL.
            refresh: Fetch even on a cache hit.
            headers: Extra request headers, merged over the JSON ``Accept``.

        Returns:
            Whatever the document decodes to.

        Raises:
            json.JSONDecodeError: If the body is not valid JSON.
        """
        merged = {"Accept": JSON_ACCEPT, **dict(headers or {})}
        return self.fetch(url, refresh=refresh, headers=merged).json()

    def is_allowed(self, url: str) -> bool:
        """Return whether ``robots.txt`` permits fetching ``url``.

        Args:
            url: An absolute URL.

        Returns:
            ``True`` if a fetch would be permitted.
        """
        target = canonical_url(url)
        return self._policy_for(target).can_fetch(self.user_agent, target)

    def close(self) -> None:
        """Close the underlying transport."""
        self._transport.close()

    def __enter__(self) -> "Harvester":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _policy_for(self, url: str) -> RobotsPolicy:
        """Return the cached or freshly read ``robots.txt`` policy for an origin.

        The lock is held across the read, not just around the dictionary, so
        that concurrent harvesting of one origin asks the site for its rules
        once rather than once per thread.
        """
        origin = origin_of(url)
        with self._policy_lock:
            memoized = self._policies.get(origin)
            if memoized is not None:
                return memoized

            policy = policy_from_response(self._robots_response(url))
            delay = policy.crawl_delay(self.user_agent)
            if delay is not None:
                self._gate.limiter.slow_to(delay)
            self._policies[origin] = policy
            return policy

    def _robots_response(self, url: str) -> HttpResponse | None:
        """Read ``robots.txt`` for ``url``'s origin, from cache when it is fresh."""
        robots_url = robots_url_for(url)
        cached = self._cache.get(robots_url)
        if cached is not None and self._is_fresh(cached):
            return HttpResponse(
                url=cached.source_url,
                status_code=cached.status_code,
                content=cached.content,
                headers={"content-type": cached.content_type},
            )
        try:
            response = self._request(robots_url, None, accept_error_statuses=True)
        except FetchError:
            return None
        self._cache.put(robots_url, response, self._clock.now())
        return response

    def _is_fresh(self, document: CachedDocument) -> bool:
        """Return whether a cached ``robots.txt`` is still inside its max age."""
        age = (self._clock.now() - document.harvested_at).total_seconds()
        return age <= self._robots_max_age_seconds

    def _request(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        *,
        accept_error_statuses: bool = False,
        accept_not_modified: bool = False,
    ) -> HttpResponse:
        """Fetch ``url`` through the gate, retrying transient failures.

        Args:
            url: The canonical URL to fetch.
            headers: Extra request headers.
            accept_error_statuses: Return 4xx responses instead of raising.
                Used for ``robots.txt``, where a 404 is a meaningful answer.
            accept_not_modified: Return a 304 instead of raising. Set only
                when the request actually carried a validator; a 304 to an
                unconditional request is a broken server, and treating it as
                "unchanged" would silently freeze that document forever.

        Returns:
            The response.

        Raises:
            PermanentFetchError: On a 4xx or unfollowed redirect, unless
                ``accept_error_statuses`` is set.
            TransientFetchError: If every attempt failed.
        """
        request_headers = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower() != "user-agent"
        }
        request_headers["User-Agent"] = self.user_agent
        last_error: FetchError | None = None

        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            response: HttpResponse | None = None
            with self._gate.slot():
                with self._counter_lock:
                    self.requests_made += 1
                try:
                    response = self._transport.get(
                        url, headers=request_headers, timeout=self._timeout
                    )
                except OSError as error:
                    last_error = TransientFetchError(url, f"transport failure: {error}")

            if response is not None:
                status = response.status_code
                if 200 <= status < 300:
                    return response
                if accept_not_modified and status == NOT_MODIFIED:
                    return response
                if status == 429 or status >= 500:
                    last_error = TransientFetchError(url, f"HTTP {status}", status)
                    retry_after = _retry_after_seconds(response)
                elif accept_error_statuses and 400 <= status < 500:
                    return response
                else:
                    raise PermanentFetchError(url, f"HTTP {status}", status)

            if attempt < self._max_attempts:
                self._clock.sleep(self._backoff(attempt, retry_after))

        if last_error is None:
            last_error = TransientFetchError(url, "no attempt was made")
        raise last_error

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Return how long to wait before the attempt after ``attempt``."""
        if retry_after is not None:
            return min(retry_after, self._max_backoff_seconds)
        growth = self._backoff_multiplier ** (attempt - 1)
        return min(self._backoff_seconds * growth, self._max_backoff_seconds)
