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
"""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
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
    """

    requested_url: str
    source_url: str
    harvested_at: datetime
    status_code: int
    content_type: str
    content_sha256: str
    content: bytes
    previous_sha256: str | None = None

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
        )
        self._blobs.write(self.pointer_key(url), self._dump(document))
        return document

    def urls(self) -> Iterator[str]:
        """Yield the canonical URL of every document in the cache."""
        for key in self._blobs.keys(self.index_prefix):
            raw_pointer = self._blobs.read(key)
            if raw_pointer is not None:
                yield str(json.loads(raw_pointer)["requested_url"])

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
        )
