"""Where a normalized photo is written, and the reference that is all anyone gets.

RFC-001 section 07 is precise about the boundary: the upload returns a
``blob_ref``, and **the ref is the only thing that ever crosses into a tool
argument.** The image does not. That is not a size optimisation -- it is what
keeps a photograph out of the model's context, out of a tool call's recorded
arguments, and out of every span, log line and trace those produce.

:class:`BlobRef` is therefore deliberately dull: a container, a name, and no
method that returns image bytes. Reading the image back is a separate capability
with a separate name -- :class:`BlobReader` -- and stage 4 is given one of those
explicitly. A ref that could fetch its own bytes would put an image one attribute
access away from every place a ref is legitimately passed, which is every span
and every tool argument in the photo lane.

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
    "PHOTO_REF_ARGUMENT",
    "AzureBlobStore",
    "BlobReader",
    "BlobRef",
    "BlobStore",
    "blob_name",
]

ACCOUNT_VARIABLE = "AZURE_STORAGE_ACCOUNT"
"""The storage account name. Set on the Container App by ``infra/terraform``."""

CONTAINER_VARIABLE = "AZURE_UPLOADS_CONTAINER"
"""The uploads container name, set by the same file. ``uploads`` in practice."""

PHOTO_REF_ARGUMENT = "blob_ref"
"""What ``match_meal_from_photo`` calls its one argument.

The name has to match the parameter
:mod:`chip_chat.agent.surface` declares, because tool arguments are recorded on
the tool span and Phase 9's tool-selection evals read them: two names for one
argument is two vocabularies for one tool, which is the whole class of problem
the span schema exists to prevent. It lives here rather than in ``agent/``
because ``vision/`` cannot import back into ``agent/`` -- see
:mod:`chip_chat.vision.describe` on why that direction is one-way --  and
``agent/tests/test_photo_tool.py`` is where the two are asserted equal.
"""


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


class BlobReader(Protocol):
    """The one read the describe stage performs.

    Deliberately a different protocol from :class:`BlobStore` rather than two
    more methods on it. Stage 4 needs to read and must never write; the upload
    path needs to write and has no business reading anything back. Splitting
    them means each stage is handed the capability it uses and not the other
    one -- and it means a test double for stage 4 is a dict with a ``read``, not
    a whole store.
    """

    def read(self, ref: BlobRef) -> bytes:
        """Return the bytes stored under ``ref``.

        Args:
            ref: A reference the upload path produced. Its container is checked
                against the reader's own, because a ref is the one part of the
                photo lane that can arrive from outside this process.

        Returns:
            The stored image bytes, exactly as they were written -- normalized,
            re-encoded and already screened by stage 3.

        Raises:
            KeyError: If nothing is stored under that name.
            ValueError: If the ref names a different container.
            OSError: If the read failed for any other reason. Stage 4 declines
                on all three -- a photograph it cannot read is a photograph it
                cannot describe -- so an implementation must raise one of these
                rather than return empty bytes.
        """
        ...


class AzureBlobStore:
    """Reads and writes the real uploads container with the app's managed identity.

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

    def read(self, ref: BlobRef) -> bytes:
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        if ref.container != self.container:
            raise ValueError(f"{ref} is not in this store's container ({self.container})")
        try:
            return bytes(self._client.download_blob(ref.name).readall())
        except ResourceNotFoundError as error:
            raise KeyError(ref.name) from error
        except AzureError as error:
            # Translated at the vendor boundary, the way
            # :class:`~chip_chat.vision.moderation.AzureImageAnalyzer` turns one
            # into a moderation failure. A stage that had to catch ``AzureError``
            # to decline gracefully would be a stage that imports the Azure SDK
            # in order to describe a photograph.
            raise OSError(f"could not read {ref}: {error}") from error

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
