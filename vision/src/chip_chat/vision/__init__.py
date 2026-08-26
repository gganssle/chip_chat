"""Photo pipeline: validate, moderate, describe, match.

Six stages, and RFC-001 section 07 is explicit that their ordering is the
design. Moderation happens before inference so nothing unmoderated reaches a
model; SKU resolution happens after inference so no model output is trusted as
a product identifier. What lives here today is everything that happens
before a model is involved at all:

=========== ================================================= ==================
Stage       What it does                                      Where
=========== ================================================= ==================
0 Read      Byte ceiling and deadline, while it arrives       ``reader``
1 Validate  Size, magic bytes, allowlist, pixel ceiling       ``validate``
2 Normalize Strip metadata, re-encode, downscale              ``normalize``
3 Moderate  Content Safety, then the write                    ``moderation``
4 Describe  Structured slots, no free text                    issue #53
5 Resolve   Deterministic catalogue match                     issue #54
6 Propose   Priced, confirmable draft                         issue #62
=========== ================================================= ==================

Those first four are what decides whether a hostile upload ever reaches a model,
so they are written to be the boring part: no inference, one entry point, and an
order it cannot be run out of.

.. code-block:: python

    from chip_chat.vision import (
        AzureBlobStore,
        AzureImageAnalyzer,
        ImageModerator,
        PhotoIntake,
        UploadRejectedError,
        read_upload_async,
    )

    intake = PhotoIntake(
        store=AzureBlobStore.from_env(),
        moderator=ImageModerator(analyzer=AzureImageAnalyzer.from_env()),
    )
    try:
        payload = await read_upload_async(body, declared_length=declared)
        photo = intake.accept(payload, declared_media_type=content_type)
    except UploadRejectedError as refusal:
        return upload_error(refusal.message)
    return uploaded(str(photo.blob_ref), photo.retention_notice)

Four properties hold the rest of the design up, and each is easy to undo by
accident:

**Nothing is read unbounded.** The ceiling is enforced while the body arrives,
not after it is in memory, and the read carries a deadline as well as a size.
See :mod:`chip_chat.vision.reader`.

**The declared content type is never trusted.** It is attacker-controlled. What
a file is, is decided from its bytes. See :mod:`chip_chat.vision.validate`.

**Nothing unmoderated is ever stored.** The write is the last statement of
:meth:`~chip_chat.vision.intake.PhotoIntake.accept` and Content Safety is the
statement before it, so there is no blob for stage 4 to be handed that stage 3
did not pass -- and no way to build a
:class:`~chip_chat.vision.intake.PhotoIntake` without a moderator. See
:mod:`chip_chat.vision.moderation`.

**The image never crosses a tool boundary.** A
:attr:`~chip_chat.vision.intake.StoredPhoto.blob_ref` does. See
:mod:`chip_chat.vision.store`.
"""

from chip_chat.otel import service_name
from chip_chat.vision.intake import PhotoIntake, StoredPhoto
from chip_chat.vision.limits import SUPPORTED_MEDIA_TYPES, UploadLimits
from chip_chat.vision.moderation import (
    SEVERITY_LEVELS,
    AzureImageAnalyzer,
    ImageAnalyzer,
    ImageModerator,
    ModerationThresholds,
    ModerationUnavailableError,
    ModerationVerdict,
    SafetyCategory,
)
from chip_chat.vision.normalize import NORMALIZED_MEDIA_TYPE, NormalizedImage, normalize
from chip_chat.vision.reader import (
    AsyncByteStream,
    ByteStream,
    content_length,
    read_upload,
    read_upload_async,
)
from chip_chat.vision.retention import (
    RETENTION_CEILING_HOURS,
    RETENTION_NOTICE,
    RETENTION_NOTICE_LONG,
)
from chip_chat.vision.store import AzureBlobStore, BlobRef, BlobStore, blob_name
from chip_chat.vision.validate import (
    RejectionReason,
    UploadRejectedError,
    ValidImage,
    sniff,
    validate,
)

__all__ = [
    "NORMALIZED_MEDIA_TYPE",
    "RETENTION_CEILING_HOURS",
    "RETENTION_NOTICE",
    "RETENTION_NOTICE_LONG",
    "SERVICE_NAME",
    "SEVERITY_LEVELS",
    "SUPPORTED_MEDIA_TYPES",
    "AsyncByteStream",
    "AzureBlobStore",
    "AzureImageAnalyzer",
    "BlobRef",
    "BlobStore",
    "ByteStream",
    "ImageAnalyzer",
    "ImageModerator",
    "ModerationThresholds",
    "ModerationUnavailableError",
    "ModerationVerdict",
    "NormalizedImage",
    "PhotoIntake",
    "RejectionReason",
    "SafetyCategory",
    "StoredPhoto",
    "UploadLimits",
    "UploadRejectedError",
    "ValidImage",
    "__version__",
    "blob_name",
    "content_length",
    "normalize",
    "read_upload",
    "read_upload_async",
    "service_name",
    "sniff",
    "validate",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("vision")
"""OpenTelemetry ``service.name`` for this component."""
