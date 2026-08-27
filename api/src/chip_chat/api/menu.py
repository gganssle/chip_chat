"""The production catalogue loader: the seam two other modules named and neither owned.

Both halves of the write path stop at the same missing function.
:func:`chip_chat.api.app.build_service` records it for the photo lane, and
``api/functions/function_app.py`` records it for the ops API in as many words:

    :func:`build_ops_service` needs a catalogue, because the draft store prices
    against one, and the production catalogue loader is #66's.

This is that loader. It is small, and the reason it did not exist is not that it
is hard -- it is that it is the one place where "where does the built catalogue
live in production" has to be answered concretely, and nothing had needed to
answer it until a service refused to start.

**The answer.** ``chip_chat.catalog.MenuCatalog.write`` puts nine JSONL tables
and a manifest under ``catalog/chipotle/`` in a
:class:`chip_chat.harvest.blobs.BlobStore`, and ``#24``'s build publishes that to
the ``raw`` container of the storage account Terraform stands up. So the loader
is a blob store pointed at that container, and
:func:`chip_chat.catalog.load_catalog` reading the same prefix it was written
under. :data:`CONTAINER_VARIABLE` and :data:`PREFIX_VARIABLE` are the two
settings, both defaulted to what the publish actually uses, so a deployment that
sets neither still finds it.

**Why the store is here rather than in** ``harvest/``. ``harvest/`` ships three
implementations of its own protocol -- in memory, on disk, and nothing else --
and the reason is stated there: a harvest runs on a laptop. The Azure-backed one
is a *deployment* concern, it authenticates with the app's managed identity, and
it is the app tier that has an identity to authenticate with.
:class:`chip_chat.vision.store.AzureBlobStore` makes the same argument for the
uploads container and this file follows its shape deliberately, down to the
import being inside the method so that a unit test can exercise the module
without an identity chain to resolve.

**Read-only, on purpose.** :meth:`AzureCatalogStore.write` raises. Nothing in a
request path should be able to publish a catalogue, and a store that could would
be a store somebody eventually calls: the nightly build writes the catalogue and
the app reads it, and the direction of that arrow is worth making unstateable
rather than merely conventional.

**And it is cached.** The catalogue is tens of thousands of rows and does not
change between deployments -- the build publishes under a new content version
and the app picks it up on its next restart. :func:`build_catalog` reads it once
per process and hands the same object out afterwards, because a scale-to-zero
app that re-read nine blobs on every cold start would be paying for it in the
one number issue #71 asks to be measured.
"""

import logging
import os
import threading
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Final

from chip_chat.catalog import MenuCatalog, load_catalog
from chip_chat.catalog.records import DEFAULT_PREFIX

if TYPE_CHECKING:  # pragma: no cover - import cost, not behaviour
    from azure.storage.blob import ContainerClient

__all__ = [
    "ACCOUNT_VARIABLE",
    "CONTAINER_VARIABLE",
    "PREFIX_VARIABLE",
    "AzureCatalogStore",
    "build_catalog",
    "catalog_from_env",
]

ACCOUNT_VARIABLE: Final = "AZURE_STORAGE_ACCOUNT"
"""The storage account name. Set on the Container App by ``infra/terraform``."""

CONTAINER_VARIABLE: Final = "AZURE_CATALOG_CONTAINER"
"""Which container the built catalogue was published to. Defaults to ``raw``.

Named rather than hardcoded because #24's publish takes a container argument,
and defaulted because it has only ever been given one.
"""

PREFIX_VARIABLE: Final = "AZURE_CATALOG_PREFIX"
"""The key prefix under the container. Defaults to
:data:`chip_chat.catalog.records.DEFAULT_PREFIX`, which is what
:meth:`MenuCatalog.write` uses when nobody says otherwise."""

DEFAULT_CONTAINER: Final = "raw"
"""Where the harvest and the catalogue build both write."""

_log = logging.getLogger("chip_chat.api.menu")

_lock = threading.Lock()
_catalog: MenuCatalog | None = None


class AzureCatalogStore:
    """A read-only :class:`chip_chat.harvest.blobs.BlobStore` over one container.

    Satisfies the protocol structurally rather than by inheritance, which is
    what lets this live in ``api/`` without ``api/`` importing ``harvest/`` for
    a base class it would not otherwise need.
    """

    __slots__ = ("_client",)

    def __init__(self, client: "ContainerClient") -> None:
        """Wrap a container client.

        Args:
            client: A client already pointed at the container the catalogue was
                published to.
        """
        self._client = client

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AzureCatalogStore":
        """Build a store from the environment the Container App is given.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            A store pointed at the catalogue's container.

        Raises:
            RuntimeError: If :data:`ACCOUNT_VARIABLE` is unset. Only that one:
                the container and the prefix have correct defaults and an
                account name cannot have one.
        """
        # Imported here rather than at module scope, following
        # `chip_chat.vision.store`: this module stays importable, and
        # unit-testable, without the Azure SDK's import cost or an identity
        # chain to resolve.
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient

        source = os.environ if env is None else env
        account = source.get(ACCOUNT_VARIABLE, "").strip()
        if not account:
            raise RuntimeError(
                f"the catalogue's storage account is not configured: "
                f"{ACCOUNT_VARIABLE} is unset"
            )
        container = source.get(CONTAINER_VARIABLE, "").strip() or DEFAULT_CONTAINER
        return cls(
            ContainerClient(
                account_url=f"https://{account}.blob.core.windows.net",
                container_name=container,
                credential=DefaultAzureCredential(),
            )
        )

    @property
    def container(self) -> str:
        """The container this store reads."""
        return self._client.container_name

    def read(self, key: str) -> bytes | None:
        """Return the blob at ``key``, or ``None`` if it is not there.

        ``None`` rather than an exception for a missing blob, because that is
        what the protocol says and because
        :func:`~chip_chat.catalog.load_catalog` turns a missing table into a
        ``CatalogLoadError`` naming which one -- a better message than a
        ``ResourceNotFoundError`` carrying a URL.
        """
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return bytes(self._client.download_blob(key).readall())
        except ResourceNotFoundError:
            return None

    def exists(self, key: str) -> bool:
        """Return whether a blob exists at ``key``."""
        return self._client.get_blob_client(key).exists()

    def keys(self, prefix: str = "") -> Iterator[str]:
        """Yield every key beginning with ``prefix``, in sorted order."""
        yield from sorted(
            blob.name for blob in self._client.list_blobs(name_starts_with=prefix)
        )

    def write(self, key: str, data: bytes) -> None:
        """Refuse. Nothing in a request path publishes a catalogue.

        Raises:
            RuntimeError: Always. See the module docstring.
        """
        raise RuntimeError(
            f"the app tier reads the catalogue and does not publish it; "
            f"refusing to write {key!r} ({len(data)} bytes)"
        )


def catalog_from_env(env: Mapping[str, str] | None = None) -> MenuCatalog:
    """Read the published catalogue, without caching.

    Args:
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        The catalogue.

    Raises:
        RuntimeError: If the storage account is not configured.
        chip_chat.catalog.load.CatalogLoadError: If a table or the manifest is
            missing, or the manifest's counts do not match what was read. A
            catalogue that loads short is worse than one that does not load,
            because nothing downstream would notice.
    """
    source = os.environ if env is None else env
    prefix = source.get(PREFIX_VARIABLE, "").strip() or DEFAULT_PREFIX
    return load_catalog(AzureCatalogStore.from_env(source), prefix)


def build_catalog(env: Mapping[str, str] | None = None) -> MenuCatalog | None:
    """Return the published catalogue, read once per process, or ``None``.

    ``None`` rather than a raise, because the two callers want the same thing
    and neither wants a stack trace: a service that cannot price drafts should
    say so through the copy RFC-001 §10 gives it, and an app that cannot price
    drafts should still answer menu questions. The refusal is logged with the
    reason, so an operator reading the container's first lines can see which of
    the two states the deployment is in.

    Args:
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        The catalogue, or ``None`` if it could not be read.
    """
    global _catalog
    with _lock:
        if _catalog is not None:
            return _catalog
        try:
            _catalog = catalog_from_env(env)
        except Exception:
            _log.warning(
                "no published catalogue: drafts cannot be priced from real menu "
                "rows on this deployment. Set %s (and %s / %s if the publish did "
                "not use the defaults).",
                ACCOUNT_VARIABLE,
                CONTAINER_VARIABLE,
                PREFIX_VARIABLE,
                exc_info=True,
            )
            return None
        _log.info(
            "catalogue loaded: content version %s, %d menu items, %d stores",
            _catalog.content_version(),
            len(_catalog.menu_items),
            len(_catalog.stores),
        )
        return _catalog
