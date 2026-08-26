"""Photo pipeline: validate, moderate, describe, match.

Six stages, and RFC-001 section 07 is explicit that their ordering is the
design. Moderation happens before inference so nothing unmoderated reaches a
model; SKU resolution happens after inference so no model output is trusted as
a product identifier. What lives here today is stages 1 and 2 -- the front of
it, and the only part that involves no model at all:

=========== ================================================= ==================
Stage       What it does                                      Where
=========== ================================================= ==================
1 Validate  Size, magic bytes, allowlist, pixel ceiling       ``validate``
2 Normalize Strip metadata, re-encode, downscale, store       ``normalize``
3 Moderate  Content Safety                                    issue #52
4 Describe  Structured slots, no free text                    issue #53
5 Resolve   Deterministic catalogue match                     issue #54
6 Propose   Priced, confirmable draft                         issue #62
=========== ================================================= ==================

Stages 1 and 2 are what decides whether a hostile upload ever reaches a model,
so they are written to be the boring part: no inference, no network call, and
one entry point that cannot be run out of order.

.. code-block:: python

    from chip_chat.vision import AzureBlobStore, PhotoIntake, UploadRejectedError

    intake = PhotoIntake(store=AzureBlobStore.from_env())
    try:
        photo = intake.accept(payload, declared_media_type=content_type)
    except UploadRejectedError as refusal:
        return upload_error(refusal.message)
    return uploaded(str(photo.blob_ref), photo.retention_notice)

Two properties hold the rest of the design up, and both are easy to undo by
accident:

**The declared content type is never trusted.** It is attacker-controlled. What
a file is, is decided from its bytes. See :mod:`chip_chat.vision.validate`.

**The image never crosses a tool boundary.** A
:attr:`~chip_chat.vision.intake.StoredPhoto.blob_ref` does. See
:mod:`chip_chat.vision.store`.
"""

from chip_chat.otel import service_name
from chip_chat.vision.intake import PhotoIntake, StoredPhoto
from chip_chat.vision.limits import SUPPORTED_MEDIA_TYPES, UploadLimits
from chip_chat.vision.normalize import NORMALIZED_MEDIA_TYPE, NormalizedImage, normalize
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
    "SUPPORTED_MEDIA_TYPES",
    "AzureBlobStore",
    "BlobRef",
    "BlobStore",
    "NormalizedImage",
    "PhotoIntake",
    "RejectionReason",
    "StoredPhoto",
    "UploadLimits",
    "UploadRejectedError",
    "ValidImage",
    "__version__",
    "blob_name",
    "normalize",
    "service_name",
    "sniff",
    "validate",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("vision")
"""OpenTelemetry ``service.name`` for this component."""
