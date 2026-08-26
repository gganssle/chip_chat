"""Where a normalized photo is written, and the reference that is all anyone gets.

RFC-001 section 07 is precise about the boundary: the upload returns a
``blob_ref``, and **the ref is the only thing that ever crosses into a tool
argument.** The image does not. That is not a size optimisation -- it is what
keeps a photograph out of the model's context, out of a tool call's recorded
arguments, and out of every span, log line and trace those produce.

:class:`BlobRef` is therefore deliberately dull: a container, a name, and no
method that returns image bytes. Reading the image back is the moderation and
describe stages' business (issues #52 and #53), through their own clients.

Naming is ``<uuid4>.jpg`` under a date prefix::

    uploads/2026-08-26/8f14e45f-ea0b-4c0d-9f2a-1b3d5e7a9c11.jpg

The uuid is unguessable, which matters on a container whose blobs are readable
by anything holding the app's identity, and it carries nothing about the visitor
who uploaded it -- no session id, no address, no filename. The date prefix is
for the human reading a container listing at 2am; the lifecycle rule matches on
the container prefix and does not care.

Two implementations. :class:`AzureBlobStore` writes to the real account with the
app's managed identity -- ``shared_access_key_enabled = false`` on that account,
so there is no connection string and nothing that could leak as one.
:class:`~chip_chat.vision.testing.InMemoryBlobStore` is the test double.
"""

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - import cost, not behaviour
    from azure.storage.blob import ContainerClient

__all__ = [
    "ACCOUNT_VARIABLE",
    "CONTAINER_VARIABLE",
    "AzureBlobStore",
    "BlobRef",
    "BlobStore",
    "blob_name",
]

ACCOUNT_VARIABLE = "AZURE_STORAGE_ACCOUNT"
"""The storage account name. Set on the Container App by ``infra/terraform``."""

CONTAINER_VARIABLE = "AZURE_UPLOADS_CONTAINER"
"""The uploads container name, set by the same file. ``uploads`` in practice."""


@dataclass(frozen=True, slots=True)
class BlobRef:
    """A pointer to one stored photo. The only thing that crosses a tool boundary.

    Frozen, and carrying no bytes: passing one around cannot accidentally pass
    an image around.
    """

    container: str
    name: str

    def __str__(self) -> str:
        """Return ``container/name`` -- the form a tool argument takes."""
        return f"{self.container}/{self.name}"

    @classmethod
    def parse(cls, reference: str) -> "BlobRef":
        """Rebuild a ref from its string form.

        Args:
            reference: A ``container/name`` string, as produced by :meth:`__str__`.

        Returns:
            The parsed ref.

        Raises:
            ValueError: If ``reference`` has no container part, or names a path
                that climbs out of its container. The parse is where a ref
                arriving from outside this process is checked, and a model that
                invents ``uploads/../functions/host.json`` must fail here rather
                than reach a container client.
        """
        container, separator, name = reference.partition("/")
        if not separator or not container or not name:
            raise ValueError(f"not a blob reference: {reference!r}")
        if ".." in name.split("/") or name.startswith("/"):
            raise ValueError(f"blob reference escapes its container: {reference!r}")
        return cls(container=container, name=name)


def blob_name(*, now: datetime | None = None) -> str:
    """Mint a name for one upload.

    Args:
        now: The instant to date the prefix from. Defaults to now, in UTC --
            named explicitly because a local-time prefix would put the same
            evening's photographs in two different folders.

    Returns:
        A ``YYYY-MM-DD/<uuid4>.jpg`` name, carrying nothing about the visitor.
    """
    stamp = (now if now is not None else datetime.now(UTC)).astimezone(UTC)
    return f"{stamp:%Y-%m-%d}/{uuid.uuid4()}.jpg"


class BlobStore(Protocol):
    """The one write the upload path performs."""

    @property
    def container(self) -> str:
        """The container written to, so a :class:`BlobRef` can name it."""
        ...

    def put(self, name: str, data: bytes, *, content_type: str) -> None:
        """Write one blob.

        Args:
            name: The blob name, from :func:`blob_name`.
            data: The normalized image bytes.
            content_type: What to record as the blob's content type.

        Raises:
            FileExistsError: If ``name`` is already taken. Writes never
                overwrite: the name is a uuid, so a collision is either
                something very strange or a replay, and neither should quietly
                replace a photograph already in the container.
        """
        ...


class AzureBlobStore:
    """Writes to the real uploads container with the app's managed identity.

    Constructed from the environment the Container App is given. The account has
    shared keys disabled, so there is no code path here that could authenticate
    with a secret -- :class:`~azure.identity.DefaultAzureCredential` is not a
    preference, it is the only thing that works.
    """

    __slots__ = ("_client",)

    def __init__(self, client: "ContainerClient") -> None:
        """Wrap a container client.

        Args:
            client: A client already pointed at the uploads container.
        """
        self._client = client

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AzureBlobStore":
        """Build a store from ``AZURE_STORAGE_ACCOUNT`` and ``AZURE_UPLOADS_CONTAINER``.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            A store pointed at the uploads container.

        Raises:
            RuntimeError: If either variable is missing. Failing at startup is
                the point: the alternative is discovering it on the first
                visitor who attaches a photo.
        """
        # Imported here rather than at module scope so that validate/normalize
        # stay importable -- and unit-testable -- without the Azure SDK's own
        # import cost or an identity chain to resolve.
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient

        source = os.environ if env is None else env
        account = source.get(ACCOUNT_VARIABLE, "").strip()
        container = source.get(CONTAINER_VARIABLE, "").strip()
        missing = [
            name
            for name, value in (
                (ACCOUNT_VARIABLE, account),
                (CONTAINER_VARIABLE, container),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"upload storage is not configured: {', '.join(missing)}")

        return cls(
            ContainerClient(
                account_url=f"https://{account}.blob.core.windows.net",
                container_name=container,
                credential=DefaultAzureCredential(),
            )
        )

    @property
    def container(self) -> str:
        return self._client.container_name

    def put(self, name: str, data: bytes, *, content_type: str) -> None:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        try:
            self._client.upload_blob(
                name=name,
                data=data,
                overwrite=False,
                content_settings=ContentSettings(content_type=content_type),
            )
        except ResourceExistsError as error:
            raise FileExistsError(name) from error
