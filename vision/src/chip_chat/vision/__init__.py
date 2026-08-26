"""Photo pipeline: validate, moderate, describe, match.

Six stages, and RFC-001 section 07 is explicit that their ordering is the
design. Moderation happens before inference so nothing unmoderated reaches a
model; SKU resolution happens after inference so no model output is trusted as
a product identifier. What lives here today is stages 1 to 4 -- the whole path
from a hostile upload to a structured description nothing downstream has to
trust as a product identifier:

=========== ================================================= ==================
Stage       What it does                                      Where
=========== ================================================= ==================
1 Validate  Size, magic bytes, allowlist, pixel ceiling       ``validate``
2 Normalize Strip metadata, re-encode, downscale              ``normalize``
3 Moderate  Content Safety, then the write                    ``moderation``
4 Describe  Structured slots, no free text                    ``describe``
5 Resolve   Deterministic catalogue match                     issue #54
6 Propose   Priced, confirmable draft                         issue #62
=========== ================================================= ==================

The first three decide whether a hostile upload ever reaches a model, so they
are written to be the boring part: no inference, one entry point, and an order
it cannot be run out of. The fourth is the one that involves a model, and it is
arranged so that the model's answer cannot become a product name -- see
:mod:`chip_chat.vision.describe`.

.. code-block:: python

    from chip_chat.vision import (
        AzureBlobStore,
        AzureImageAnalyzer,
        ImageModerator,
        PhotoIntake,
        UploadRejectedError,
    )

    intake = PhotoIntake(
        store=AzureBlobStore.from_env(),
        moderator=ImageModerator(analyzer=AzureImageAnalyzer.from_env()),
    )
    try:
        photo = intake.accept(payload, declared_media_type=content_type)
    except UploadRejectedError as refusal:
        return upload_error(refusal.message)
    return uploaded(str(photo.blob_ref), photo.retention_notice)

Three properties hold the rest of the design up, and each is easy to undo by
accident:

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

**The model describes; it never names a SKU.** Its vocabulary is generated from
the live catalogue at build time and enforced by the API, and the one free-text
field it may return is not on the object the matcher receives. See
:mod:`chip_chat.vision.describe`.

.. code-block:: python

    describer = MealDescriber(
        AzureVisionModel.from_env(),
        images=AzureBlobStore.from_env(),
        vocabulary=Vocabulary.from_env(),
    )
    description = describer.describe(photo.blob_ref)
    show(description.notes)          # display-only, and the only reader
    resolve(description.meal)        # issue #54. There are no notes on it.
"""

from chip_chat.otel import service_name
from chip_chat.vision.describe import (
    DESCRIBE_UNAVAILABLE_MESSAGE,
    SYSTEM_PROMPT,
    AzureVisionModel,
    ConfidenceProfile,
    DescribedMeal,
    DescribeError,
    DescribeUnavailableError,
    Description,
    DescriptionRejectedError,
    MealDescriber,
    SlotValue,
    VisionModel,
    confidence_profile,
)
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
from chip_chat.vision.retention import (
    RETENTION_CEILING_HOURS,
    RETENTION_NOTICE,
    RETENTION_NOTICE_LONG,
)
from chip_chat.vision.store import (
    AzureBlobStore,
    BlobReader,
    BlobRef,
    BlobStore,
    blob_name,
)
from chip_chat.vision.validate import (
    RejectionReason,
    UploadRejectedError,
    ValidImage,
    sniff,
    validate,
)
from chip_chat.vision.vocabulary import SchemaViolationError, Vocabulary, VocabularyError

__all__ = [
    "DESCRIBE_UNAVAILABLE_MESSAGE",
    "NORMALIZED_MEDIA_TYPE",
    "RETENTION_CEILING_HOURS",
    "RETENTION_NOTICE",
    "RETENTION_NOTICE_LONG",
    "SERVICE_NAME",
    "SEVERITY_LEVELS",
    "SUPPORTED_MEDIA_TYPES",
    "SYSTEM_PROMPT",
    "AzureBlobStore",
    "AzureImageAnalyzer",
    "AzureVisionModel",
    "BlobReader",
    "BlobRef",
    "BlobStore",
    "ConfidenceProfile",
    "DescribeError",
    "DescribeUnavailableError",
    "DescribedMeal",
    "Description",
    "DescriptionRejectedError",
    "ImageAnalyzer",
    "ImageModerator",
    "MealDescriber",
    "ModerationThresholds",
    "ModerationUnavailableError",
    "ModerationVerdict",
    "NormalizedImage",
    "PhotoIntake",
    "RejectionReason",
    "SafetyCategory",
    "SchemaViolationError",
    "SlotValue",
    "StoredPhoto",
    "UploadLimits",
    "UploadRejectedError",
    "ValidImage",
    "VisionModel",
    "Vocabulary",
    "VocabularyError",
    "__version__",
    "blob_name",
    "confidence_profile",
    "normalize",
    "service_name",
    "sniff",
    "validate",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("vision")
"""OpenTelemetry ``service.name`` for this component."""
