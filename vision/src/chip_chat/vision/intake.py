"""The upload path, as one call the request handler makes.

Stages 1 to 3 of RFC-001 section 07, in order, with the bounded read in front
and the write at the end::

    read  ──▶  validate  ──▶  normalize  ──▶  screen  ──▶  put  ──▶  blob_ref
   bytes        bytes          pixels     Content Safety   Blob   what leaves

This is a library rather than a route for the same reason the spend cap is
(:mod:`chip_chat.api.guard`): the FastAPI app is issue #66 and does not exist
yet, and the shape of its request path is not this module's business. What is
this module's business is that the ordering above cannot be got wrong by
whoever writes that route -- there is one entry point, and it does every step or
raises.

**The write is last, and moderation is in front of it.** That is the ordering
requirement of RFC-001 section 07 made structural rather than documented: an
image Content Safety refused is not merely kept away from the vision model, it
is never stored at all, so there is no blob for a later stage to be handed. And
:class:`PhotoIntake` cannot be constructed without a moderator, so there is no
configuration of this class that quietly skips stage 3.

.. code-block:: python

    intake = PhotoIntake(
        store=AzureBlobStore.from_env(),
        moderator=ImageModerator(analyzer=AzureImageAnalyzer.from_env()),
    )

    @app.post("/upload")
    async def upload(file: UploadFile, request: Request) -> UploadResponse:
        try:
            photo = await intake.accept_stream(
                file,
                declared_media_type=file.content_type,
                declared_length=content_length(request.headers.get("content-length")),
            )
        except UploadRejectedError as refusal:
            return UploadResponse(error=refusal.message)
        return UploadResponse(blob_ref=str(photo.blob_ref), notice=photo.retention_notice)

:meth:`PhotoIntake.accept_stream` rather than ``await file.read()`` then
:meth:`PhotoIntake.accept`, and the difference is the point of
:mod:`chip_chat.vision.reader`: ``file.read()`` with no argument buys whatever
the sender chose to send before any ceiling gets a vote. The stream form does
the same four stages with the read bounded, so a route that reaches for the
obvious method gets the bounded one.

What comes back is a :class:`StoredPhoto`, and the only field of it that may be
handed to a tool is :attr:`~StoredPhoto.blob_ref`. Stage 4 (the vision model,
issue #53) reads the blob next, and nothing between here and there has held the
image in a place a model can see.

:meth:`PhotoIntake.accept` emits ``guard.content_safety``, which RFC-001 section
09 places under ``chat.turn`` -- so it is called inside one. The guard belongs
to the turn it protects, next to the spend cap and in front of ``agent.step``.
"""

from collections.abc import Callable
from dataclasses import dataclass

from chip_chat.vision.limits import UploadLimits
from chip_chat.vision.moderation import ImageModerator, ModerationVerdict
from chip_chat.vision.normalize import NORMALIZED_MEDIA_TYPE, normalize
from chip_chat.vision.reader import AsyncByteStream, read_upload_async
from chip_chat.vision.retention import RETENTION_NOTICE
from chip_chat.vision.store import BlobRef, BlobStore, blob_name
from chip_chat.vision.validate import validate

__all__ = ["PhotoIntake", "StoredPhoto"]


@dataclass(frozen=True, slots=True)
class StoredPhoto:
    """One accepted upload: where it went, what it became, and what we promised."""

    blob_ref: BlobRef
    """The reference. The only field that may become a tool argument."""

    width: int
    height: int
    byte_size: int
    """Size of the *stored* object, after re-encoding -- not what was uploaded."""

    source_media_type: str
    """What the bytes turned out to be, per the magic-byte check. Telemetry only."""

    declared_media_type: str | None
    """What the request claimed. Recorded for abuse counting; never acted on."""

    declared_matches_bytes: bool
    """Whether those two agreed. See :mod:`chip_chat.vision.validate`."""

    moderation: ModerationVerdict
    """What Content Safety said. A :class:`StoredPhoto` exists only where this
    verdict allowed one, so its presence here is the record, not a re-check."""

    retention_notice: str = RETENTION_NOTICE
    """The promise to show the visitor, alongside their photo. 48 hours."""


class PhotoIntake:
    """Stages 1 to 3, plus the write. One instance per process is enough."""

    __slots__ = ("_limits", "_moderator", "_name", "_store")

    def __init__(
        self,
        store: BlobStore,
        *,
        moderator: ImageModerator,
        limits: UploadLimits | None = None,
        name_factory: Callable[[], str] | None = None,
    ) -> None:
        """Assemble the intake.

        Args:
            store: Where accepted photos are written.
            moderator: Stage 3. Required, and deliberately without a default:
                an intake that could be built without one would be an intake
                that could be built without moderation, which is the failure
                RFC-001 section 07 is written to prevent.
            limits: The ceilings to enforce. Defaults to :class:`UploadLimits`.
            name_factory: Mints blob names. Defaults to
                :func:`~chip_chat.vision.store.blob_name`; a test passes its own
                to get a name it can predict.
        """
        self._store = store
        self._moderator = moderator
        self._limits = limits if limits is not None else UploadLimits()
        self._name: Callable[[], str] = (
            name_factory if name_factory is not None else blob_name
        )

    @property
    def limits(self) -> UploadLimits:
        """The ceilings this intake enforces, for a UI that wants to quote one."""
        return self._limits

    @property
    def moderator(self) -> ImageModerator:
        """Stage 3, for an ops surface that wants to report its thresholds."""
        return self._moderator

    async def accept_stream(
        self,
        stream: AsyncByteStream,
        *,
        declared_media_type: str | None = None,
        declared_length: int | None = None,
    ) -> StoredPhoto:
        """Read one upload under the ceiling and the deadline, then :meth:`accept` it.

        The entry point a request handler should reach for. :meth:`accept`
        takes bytes, and bytes only exist once something has already read the
        body -- so a handler that calls it directly has bought whatever the
        sender chose to send before the first gate ran. This method is the same
        four stages with that hole closed, and it enforces *this intake's*
        ceilings rather than whatever the route remembered to pass.

        Args:
            stream: The request body, with an awaitable ``read``.
            declared_media_type: The request's content type, if it sent one.
                Recorded, never trusted.
            declared_length: The request's ``Content-Length``, if it sent one.
                Used only to refuse early; see :mod:`chip_chat.vision.reader`.

        Returns:
            The :class:`StoredPhoto`.

        Raises:
            UploadRejectedError: If any gate refuses, the read included.
                Nothing has been written.
        """
        payload = await read_upload_async(
            stream, declared_length=declared_length, limits=self._limits
        )
        return self.accept(payload, declared_media_type=declared_media_type)

    def accept(
        self, data: bytes, *, declared_media_type: str | None = None
    ) -> StoredPhoto:
        """Validate, normalize, moderate and store one upload.

        Args:
            data: The uploaded bytes, already read under a ceiling --
                :meth:`accept_stream` is the method that guarantees that, and
                the one a route should call.
            declared_media_type: The request's content type, if it sent one.
                Recorded, never trusted.

        Returns:
            The :class:`StoredPhoto`, whose ``blob_ref`` is what the rest of the
            pipeline is given.

        Raises:
            UploadRejectedError: If any gate refuses -- including Content
                Safety, and including Content Safety being unreachable. Nothing
                has been written: the write is the last statement in this
                method, and it is reached only by an upload that passed all
                three stages.
        """
        image = validate(
            data, declared_media_type=declared_media_type, limits=self._limits
        )
        photo = normalize(image, limits=self._limits)
        # Stage 3, and its position in this method is the requirement. Moving
        # the write above this line would put an unscreened photograph in the
        # container, where stage 4 could be handed a reference to it.
        verdict = self._moderator.screen(photo)

        name = self._name()
        self._store.put(name, photo.data, content_type=NORMALIZED_MEDIA_TYPE)

        return StoredPhoto(
            blob_ref=BlobRef(container=self._store.container, name=name),
            width=photo.width,
            height=photo.height,
            byte_size=photo.byte_size,
            source_media_type=image.media_type,
            declared_media_type=image.declared_media_type,
            declared_matches_bytes=image.declared_matches_bytes,
            moderation=verdict,
        )
