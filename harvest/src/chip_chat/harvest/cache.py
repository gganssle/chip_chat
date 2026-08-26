"""The fetch-once cache: a content-addressed raw landing zone.

Two kinds of blob live here. The body of every response is stored under the
SHA-256 of its own bytes, so re-harvesting a page that has not changed writes
nothing and a page that has changed writes a *new* blob beside the old one
rather than over it — which is what makes a re-harvest diffable instead of
destructive. Beside the bodies sits one small JSON pointer per URL recording
``source_url``, ``harvested_at``, and the digest of the body it points at.

Those two fields are captured here, at the edge, because by the time a chunk
reaches the retrieval index there is nowhere left to recover them from, and
RFC-001 section 08 requires them to survive into the response payload as
citations.

The pointer also records the response's ``ETag`` and ``Last-Modified``, which
are the only reason a *weekly* re-harvest is a different thing from a weekly
re-download. Issue #38 asks that a re-harvest "does not re-fetch what has not
changed", and a client cannot know whether a page changed without asking; what
it can do is ask conditionally, and be answered with a 304 and no body at all.
:meth:`DocumentCache.validators` hands those two headers to the harvester and
:meth:`DocumentCache.touch` records the answer.
"""

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.errors import CacheCorruptError
from chip_chat.harvest.transport import HttpResponse

DEFAULT_PREFIX = "raw"
"""Root of the raw landing zone. Nothing here is parsed or rewritten."""

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonical_url(url: str) -> str:
    """Return ``url`` in the form used as a cache identity.

    Scheme and host are lowercased, a default port is dropped, an empty path
    becomes ``/``, and the fragment is discarded because servers never see it.
    The query is left exactly as given: parameter order is the caller's
    business, and reordering it could change what an API returns.

    Args:
        url: An absolute URL.

    Returns:
        The canonical form.

    Raises:
        ValueError: If ``url`` has no scheme or no host.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"expected an absolute URL, got {url!r}")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port is not None and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def digest_of(data: bytes) -> str:
    """Return the hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _optional_str(value: Any) -> str | None:
    """Return ``value`` as a non-empty string, or ``None``.

    Pointers written before these fields existed simply do not carry them, and
    a cache that predates a re-harvest is the normal case rather than an error
    — the first re-harvest is what fills them in.
    """
    return str(value) if value else None


def _optional_time(value: Any) -> datetime | None:
    """Return ``value`` parsed as an ISO timestamp, or ``None``."""
    return datetime.fromisoformat(str(value)) if value else None


@dataclass(frozen=True, slots=True)
class CachedDocument:
    """One harvested response, plus the provenance that travels with it.

    Attributes:
        requested_url: The canonical URL that was asked for. The cache key.
        source_url: The URL the bytes actually came from, after redirects.
            This is the citation field.
        harvested_at: When the fetch completed, timezone-aware and in UTC.
        status_code: The HTTP status of the stored response.
        content_type: The ``Content-Type`` header, verbatim.
        content_sha256: The digest of ``content``, and its blob key.
        content: The raw body, exactly as received.
        previous_sha256: The digest this URL resolved to before the most
            recent fetch changed it, or ``None`` if it never changed.
        etag: The ``ETag`` header, verbatim, or ``None`` if the server sent
            none. Offered back as ``If-None-Match`` on a re-harvest.
        last_modified: The ``Last-Modified`` header, verbatim, or ``None``.
            Offered back as ``If-Modified-Since``. Kept as the server's own
            string rather than parsed into a datetime: HTTP dates round-trip
            through :mod:`datetime` badly, and the only thing this value is
            ever used for is to be handed straight back.
        revalidated_at: When a conditional request last confirmed this body
            was still current *without* re-fetching it, or ``None`` if that
            has never happened. ``harvested_at`` moves with it — the corpus is
            as fresh as its last confirmation — and this field is what keeps
            the distinction recoverable afterwards.
    """

    requested_url: str
    source_url: str
    harvested_at: datetime
    status_code: int
    content_type: str
    content_sha256: str
    content: bytes
    previous_sha256: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    revalidated_at: datetime | None = None

    @property
    def text(self) -> str:
        """Return the body decoded as UTF-8, replacing undecodable bytes."""
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON.

        Returns:
            Whatever the document decodes to.

        Raises:
            json.JSONDecodeError: If the body is not valid JSON.
        """
        return json.loads(self.content)


class DocumentCache:
    """Content-addressed storage for raw responses over a :class:`BlobStore`."""

    def __init__(self, blobs: BlobStore, prefix: str = DEFAULT_PREFIX) -> None:
        """Initialise the cache.

        Args:
            blobs: Where blobs are written.
            prefix: Key prefix for everything this cache owns.
        """
        self._blobs = blobs
        self._prefix = prefix.strip("/")

    @property
    def index_prefix(self) -> str:
        """Key prefix under which the per-URL pointers live."""
        return f"{self._prefix}/index/"

    def content_key(self, content_sha256: str) -> str:
        """Return the blob key holding the body with this digest.

        Args:
            content_sha256: A hex SHA-256 digest.

        Returns:
            The blob key.
        """
        return f"{self._prefix}/blobs/sha256/{content_sha256[:2]}/{content_sha256}"

    def pointer_key(self, url: str) -> str:
        """Return the blob key holding the pointer for ``url``.

        Args:
            url: An absolute URL; canonicalised before hashing.

        Returns:
            The blob key.
        """
        identity = digest_of(canonical_url(url).encode("utf-8"))
        return f"{self.index_prefix}{identity[:2]}/{identity}.json"

    def get(self, url: str) -> CachedDocument | None:
        """Return the cached document for ``url``, or ``None`` if there is none.

        Args:
            url: An absolute URL.

        Returns:
            The cached document, body included.

        Raises:
            CacheCorruptError: If a pointer exists but the body it names does not,
                or the body's digest no longer matches the pointer.
        """
        raw_pointer = self._blobs.read(self.pointer_key(url))
        if raw_pointer is None:
            return None
        return self._load(raw_pointer)

    def put(
        self,
        url: str,
        response: HttpResponse,
        harvested_at: datetime,
    ) -> CachedDocument:
        """Store ``response`` as the current document for ``url``.

        The body is written only if no blob with its digest exists yet, so an
        unchanged page costs nothing and a changed one leaves the previous
        body in place to diff against.

        Args:
            url: The URL that was requested.
            response: The response to store, unparsed.
            harvested_at: When the fetch completed. Must be timezone-aware.

        Returns:
            The stored document.

        Raises:
            ValueError: If ``harvested_at`` is naive.
        """
        if harvested_at.tzinfo is None:
            raise ValueError("harvested_at must be timezone-aware")
        requested = canonical_url(url)
        content_sha256 = digest_of(response.content)
        content_key = self.content_key(content_sha256)
        if not self._blobs.exists(content_key):
            self._blobs.write(content_key, response.content)

        try:
            existing = self.get(url)
        except CacheCorruptError:
            # A corrupt entry is not a reason to refuse the fresh bytes we are
            # holding. Overwrite it and lose only the record of what it was.
            existing = None
        previous = None
        if existing is not None and existing.content_sha256 != content_sha256:
            previous = existing.content_sha256
        elif existing is not None:
            previous = existing.previous_sha256

        document = CachedDocument(
            requested_url=requested,
            source_url=response.url or requested,
            harvested_at=harvested_at,
            status_code=response.status_code,
            content_type=response.content_type,
            content_sha256=content_sha256,
            content=response.content,
            previous_sha256=previous,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            revalidated_at=None,
        )
        self._blobs.write(self.pointer_key(url), self._dump(document))
        return document

    def validators(self, url: str) -> tuple[str | None, str | None]:
        """Return the ``ETag`` and ``Last-Modified`` recorded for ``url``.

        Only the pointer is read, never the body — the caller wants two short
        strings to put in request headers, and reading a megabyte of PDF to
        find them would make revalidation cost more than the fetch it saves.

        Args:
            url: An absolute URL.

        Returns:
            ``(etag, last_modified)``, either of which may be ``None``. Both
            are ``None`` when the URL has never been fetched, which is the
            same answer as a server that offers no validators: ask
            unconditionally.
        """
        raw_pointer = self._blobs.read(self.pointer_key(url))
        if raw_pointer is None:
            return (None, None)
        pointer = json.loads(raw_pointer)
        etag = pointer.get("etag")
        last_modified = pointer.get("last_modified")
        return (
            str(etag) if etag else None,
            str(last_modified) if last_modified else None,
        )

    def touch(self, url: str, harvested_at: datetime) -> CachedDocument | None:
        """Record that ``url`` was confirmed unchanged at ``harvested_at``.

        This is what a 304 means: the body already in the store is the current
        one, and the corpus is fresh as of now. So ``harvested_at`` moves and
        nothing else does — no blob is written, ``previous_sha256`` is left
        alone because nothing changed, and ``revalidated_at`` records that this
        timestamp came from a confirmation rather than from bytes.

        Args:
            url: An absolute URL.
            harvested_at: When the server confirmed it. Must be timezone-aware.

        Returns:
            The updated document, or ``None`` if there is no pointer for
            ``url`` — which a well-behaved server cannot cause, since a 304 is
            only ever a reply to validators this cache supplied.

        Raises:
            ValueError: If ``harvested_at`` is naive.
            CacheCorruptError: If the body the pointer names is missing or
                altered. A 304 asserts a body is current; it cannot make one
                exist.
        """
        if harvested_at.tzinfo is None:
            raise ValueError("harvested_at must be timezone-aware")
        key = self.pointer_key(url)
        raw_pointer = self._blobs.read(key)
        if raw_pointer is None:
            return None
        document = self._load(raw_pointer)
        touched = replace(
            document, harvested_at=harvested_at, revalidated_at=harvested_at
        )
        self._blobs.write(key, self._dump(touched))
        return touched

    def pointers(self) -> Iterator[Mapping[str, Any]]:
        """Yield every pointer in the cache, as written, in key order.

        The bodies are deliberately not read. Everything freshness is measured
        on — ``requested_url``, ``harvested_at``, ``content_sha256`` — is in
        the pointer, and a corpus report that had to read every PDF to count
        them would be a report nobody runs weekly.

        Yields:
            The decoded pointer objects.

        Raises:
            CacheCorruptError: If a pointer is not readable as JSON. A
                freshness number computed over a store with an unreadable
                pointer in it would be quietly short.
        """
        for key in self._blobs.keys(self.index_prefix):
            raw_pointer = self._blobs.read(key)
            if raw_pointer is None:
                continue
            try:
                pointer = json.loads(raw_pointer)
            except json.JSONDecodeError as error:
                raise CacheCorruptError(f"{key}: pointer is not JSON: {error}") from error
            if not isinstance(pointer, dict):
                raise CacheCorruptError(f"{key}: pointer is not an object")
            yield pointer

    def urls(self) -> Iterator[str]:
        """Yield the canonical URL of every document in the cache."""
        for pointer in self.pointers():
            yield str(pointer["requested_url"])

    def _dump(self, document: CachedDocument) -> bytes:
        pointer = {
            "requested_url": document.requested_url,
            "source_url": document.source_url,
            "harvested_at": document.harvested_at.isoformat(),
            "status_code": document.status_code,
            "content_type": document.content_type,
            "content_sha256": document.content_sha256,
            "content_key": self.content_key(document.content_sha256),
            "previous_sha256": document.previous_sha256,
            "etag": document.etag,
            "last_modified": document.last_modified,
            "revalidated_at": (
                document.revalidated_at.isoformat()
                if document.revalidated_at is not None
                else None
            ),
        }
        return json.dumps(pointer, indent=2, sort_keys=True).encode("utf-8")

    def _load(self, raw_pointer: bytes) -> CachedDocument:
        pointer = json.loads(raw_pointer)
        content_sha256 = str(pointer["content_sha256"])
        content = self._blobs.read(self.content_key(content_sha256))
        source_url = str(pointer["source_url"])
        if content is None:
            raise CacheCorruptError(
                f"{source_url}: cached body {content_sha256} is missing from the store"
            )
        if digest_of(content) != content_sha256:
            raise CacheCorruptError(
                f"{source_url}: cached body does not match digest {content_sha256}"
            )
        return CachedDocument(
            requested_url=str(pointer["requested_url"]),
            source_url=source_url,
            harvested_at=datetime.fromisoformat(str(pointer["harvested_at"])),
            status_code=int(pointer["status_code"]),
            content_type=str(pointer["content_type"]),
            content_sha256=content_sha256,
            content=content,
            previous_sha256=pointer["previous_sha256"],
            etag=_optional_str(pointer.get("etag")),
            last_modified=_optional_str(pointer.get("last_modified")),
            revalidated_at=_optional_time(pointer.get("revalidated_at")),
        )
