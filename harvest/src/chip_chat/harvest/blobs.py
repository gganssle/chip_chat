"""Blob storage, narrowed to the four operations the cache needs.

The raw landing zone is ADLS Gen2 in the deployed system. Nothing in this
package knows that. It talks to :class:`BlobStore`, so the local filesystem
implementation used in development and the in-memory one used in tests are
substitutable for the real container without touching a harvester.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

_SAFE_KEY_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./="
)


def validate_key(key: str) -> str:
    """Return ``key`` unchanged if it is a safe blob key, else raise.

    Keys are ``/``-separated relative paths. Absolute paths and ``..`` segments
    are rejected so a key derived from a remote URL can never escape the
    store's root on a filesystem-backed implementation.

    Args:
        key: The candidate blob key.

    Returns:
        The key, unchanged.

    Raises:
        ValueError: If the key is empty, absolute, contains a ``..`` segment,
            or uses characters outside ``[A-Za-z0-9-_./=]``.
    """
    if not key:
        raise ValueError("blob key must not be empty")
    if key.startswith("/"):
        raise ValueError(f"blob key must be relative, got {key!r}")
    if unsafe := set(key) - _SAFE_KEY_CHARS:
        raise ValueError(f"blob key {key!r} contains illegal characters {sorted(unsafe)}")
    if any(segment in ("", "..", ".") for segment in key.split("/")):
        raise ValueError(f"blob key {key!r} has an empty or relative path segment")
    return key


class BlobStore(Protocol):
    """A flat, keyed store of immutable-by-convention byte blobs."""

    def read(self, key: str) -> bytes | None:
        """Return the blob at ``key``, or ``None`` if it does not exist."""
        ...

    def write(self, key: str, data: bytes) -> None:
        """Write ``data`` at ``key``, replacing anything already there."""
        ...

    def exists(self, key: str) -> bool:
        """Return whether a blob exists at ``key``."""
        ...

    def keys(self, prefix: str = "") -> Iterator[str]:
        """Yield every key beginning with ``prefix``, in sorted order."""
        ...


class InMemoryBlobStore:
    """A :class:`BlobStore` backed by a dict. For tests and dry runs."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def read(self, key: str) -> bytes | None:
        return self._blobs.get(validate_key(key))

    def write(self, key: str, data: bytes) -> None:
        self._blobs[validate_key(key)] = data

    def exists(self, key: str) -> bool:
        return validate_key(key) in self._blobs

    def keys(self, prefix: str = "") -> Iterator[str]:
        yield from sorted(k for k in self._blobs if k.startswith(prefix))


class LocalBlobStore:
    """A :class:`BlobStore` backed by a directory tree.

    Writes land in a temporary file next to the destination and are then
    renamed, so a crash mid-write cannot leave a truncated blob behind that a
    later run would mistake for a complete one.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(validate_key(key))

    def read(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def keys(self, prefix: str = "") -> Iterator[str]:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                yield key
