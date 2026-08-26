"""Photo pipeline: validate, moderate, describe, match, answer.

Six stages, and RFC-001 section 07 is explicit that their ordering is the
design. Moderation happens before inference so nothing unmoderated reaches a
model; SKU resolution happens after inference so no model output is trusted as
a product identifier. What lives here today is stages 0 to 5 plus the sentence
stage 5 hands to the visitor -- the whole path from a hostile upload to a draft
of real catalogue rows, or to the question or the offer that replaces one:

=========== ================================================= ==================
Stage       What it does                                      Where
=========== ================================================= ==================
0 Read      Byte ceiling and deadline, while it arrives       ``reader``
1 Validate  Size, magic bytes, allowlist, pixel ceiling       ``validate``
2 Normalize Strip metadata, re-encode, downscale              ``normalize``
3 Moderate  Content Safety, then the write                    ``moderation``
4 Describe  Structured slots, no free text                    ``describe``
5 Resolve   Deterministic catalogue match                     ``matcher``
5 Answer    The sentence the visitor reads                     ``reply``
6 Propose   Priced, confirmable draft                         issue #62
=========== ================================================= ==================

``reply`` shares stage 5's number because it is not a stage: it names no item
the matcher did not resolve, and it is what issue #55 adds on top of a
:class:`~chip_chat.vision.matcher.Resolution`.

The first four decide whether a hostile upload ever reaches a model, so they are
written to be the boring part: no inference, one entry point, and an order it
cannot be run out of. The fifth is the one that names a product, and it is the
one with no model in it -- see :mod:`chip_chat.vision.matcher`.

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

**Nothing is named that the catalogue does not publish.** Stage 5 is ordinary
deterministic code holding a :class:`~chip_chat.catalog.records.MenuCatalog`,
and the only path from a described meal to a product identifier is a lookup in
it. A required slot below its floor becomes a question rather than a guess. See
:mod:`chip_chat.vision.matcher`.

.. code-block:: python

    matcher = MealMatcher(load_catalog(blobs), rules=SlotRules.from_env())
    resolution = matcher.resolve(          # the meal. There are no notes on it.
        description.meal, content_version=description.content_version
    )

**The three photographs that are not the happy path each have a behaviour.**
Food this restaurant does not serve is told so *and* offered the closest thing
it does; a component the model was unsure of becomes a question naming that
slot; several meals in one frame become a count and a question, never a draft.
:func:`~chip_chat.vision.reply.reply_for` is total over the four outcomes, and
holds no catalogue -- every food word in what it says came off a row the matcher
resolved. See :mod:`chip_chat.vision.reply`.

.. code-block:: python

    answer = reply_for(resolution)
    say(answer.text)                       # every menu word in it is a real row
    if resolution.resolved:
        card(answer.items, resolution.total())   # issue #62
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
from chip_chat.vision.matcher import (
    NOTHING_SEEN,
    REQUIRED_SLOTS,
    CatalogueDriftError,
    Clarification,
    ClarificationReason,
    DiscardedSlot,
    MealMatcher,
    Outcome,
    Resolution,
    ResolvedItem,
    SeenSlot,
    SlotRule,
    SlotRules,
)
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
from chip_chat.vision.reply import (
    SLOT_NOUNS,
    Reply,
    ReplyKind,
    reply_for,
    slot_noun,
)
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
    "NOTHING_SEEN",
    "REQUIRED_SLOTS",
    "RETENTION_CEILING_HOURS",
    "RETENTION_NOTICE",
    "RETENTION_NOTICE_LONG",
    "SERVICE_NAME",
    "SEVERITY_LEVELS",
    "SLOT_NOUNS",
    "SUPPORTED_MEDIA_TYPES",
    "SYSTEM_PROMPT",
    "AsyncByteStream",
    "AzureBlobStore",
    "AzureImageAnalyzer",
    "AzureVisionModel",
    "BlobReader",
    "BlobRef",
    "BlobStore",
    "ByteStream",
    "CatalogueDriftError",
    "Clarification",
    "ClarificationReason",
    "ConfidenceProfile",
    "DescribeError",
    "DescribeUnavailableError",
    "DescribedMeal",
    "Description",
    "DescriptionRejectedError",
    "DiscardedSlot",
    "ImageAnalyzer",
    "ImageModerator",
    "MealDescriber",
    "MealMatcher",
    "ModerationThresholds",
    "ModerationUnavailableError",
    "ModerationVerdict",
    "NormalizedImage",
    "Outcome",
    "PhotoIntake",
    "RejectionReason",
    "Reply",
    "ReplyKind",
    "Resolution",
    "ResolvedItem",
    "SafetyCategory",
    "SchemaViolationError",
    "SeenSlot",
    "SlotRule",
    "SlotRules",
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
    "content_length",
    "normalize",
    "read_upload",
    "read_upload_async",
    "reply_for",
    "service_name",
    "slot_noun",
    "sniff",
    "validate",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("vision")
"""OpenTelemetry ``service.name`` for this component."""
