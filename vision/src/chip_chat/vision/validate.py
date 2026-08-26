"""Stage 1. Decide what the bytes are, and reject before anything is written.

RFC-001 section 07 puts validation at the front of the vision pipeline, and the
single sentence that matters is this one: **the declared content type is
attacker-controlled and is never consulted for the decision.** A request says
``Content-Type: image/jpeg`` because the client typed it. Anyone can type it.
What the file *is* is decided here, from its first bytes and its header, and
nothing else.

The declared type is still recorded -- :attr:`ValidImage.declared_media_type`
and :attr:`ValidImage.declared_matches_bytes` -- because a mismatch is a useful
signal for the abuse work in issue #80. It is a signal, not a verdict: mobile
browsers mislabel uploads routinely (``application/octet-stream`` for a photo
out of an iOS camera roll is ordinary), so rejecting on mismatch would fail real
visitors while stopping nothing that the byte check does not already stop.

Four gates, in this order, each one cheaper than the next and each one refusing
before the next has to run:

1. **Size.** Compared against the ceiling before anything looks at the content.
2. **Magic bytes.** :func:`sniff` reads at most 16 bytes and answers "what is
   this". A non-image -- a ZIP, an ELF binary, a shell script, an SVG, an HTML
   page -- has no answer and is refused here, whatever it called itself.
3. **Allowlist.** The sniffed type must be one we can decode.
4. **Declared pixel dimensions.** Parsed from the header, never by decoding. A
   4 KB PNG may honestly declare itself 40000 x 40000 and cost 6 GB of RAM to
   open; the byte ceiling has nothing to say about that and this gate does.

Only then does :mod:`chip_chat.vision.normalize` decode anything.

.. code-block:: python

    try:
        image = validate(payload, declared_media_type=request_content_type)
    except UploadRejectedError as refusal:
        return upload_error(refusal.message)   # nothing was written
"""

import enum
from dataclasses import dataclass
from io import BytesIO
from typing import Final

from PIL import Image, UnidentifiedImageError

from chip_chat.vision import decoders  # noqa: F401  -- registers the HEIC opener
from chip_chat.vision.limits import SUPPORTED_MEDIA_TYPES, UploadLimits

__all__ = [
    "RejectionReason",
    "UploadRejectedError",
    "ValidImage",
    "rejection",
    "sniff",
    "validate",
]

_SNIFF_BYTES: Final = 16
"""Every signature below is decided within the first sixteen bytes."""

_HEIF_BRANDS: Final = frozenset(
    {
        b"heic",
        b"heix",
        b"heim",
        b"heis",
        b"hevc",
        b"hevx",
        b"hevm",
        b"hevs",
        b"mif1",
        b"msf1",
    }
)
"""ISO-BMFF brands that mean "this is a still image", from ISO/IEC 23008-12.

An MP4 video shares the ``ftyp`` box and is excluded by not being in this set,
which is the point: ``ftyp`` alone would let a video in.
"""


class RejectionReason(enum.Enum):
    """Why an upload was refused. Distinct values so abuse telemetry can count them."""

    EMPTY = "empty"
    """Nothing was uploaded."""

    TOO_LARGE = "too_large"
    """Over the byte ceiling."""

    NOT_AN_IMAGE = "not_an_image"
    """The bytes match no image signature at all, whatever the request claimed."""

    UNSUPPORTED_FORMAT = "unsupported_format"
    """A real image, in a format the pipeline does not decode."""

    TOO_MANY_PIXELS = "too_many_pixels"
    """The header declares more pixels than we will allocate memory for."""

    CORRUPT = "corrupt"
    """The signature matched but the header does not parse. A truncated or
    hand-edited file, or a signature glued onto something else."""


_MESSAGES: Final[dict[RejectionReason, str]] = {
    RejectionReason.EMPTY: "That upload arrived empty. Try attaching the photo again.",
    RejectionReason.TOO_LARGE: (
        "That photo is larger than {max_mb:.0f} MB. Most phones can send a smaller copy."
    ),
    RejectionReason.NOT_AN_IMAGE: (
        "That file is not a photo, so there is nothing to read."
    ),
    RejectionReason.UNSUPPORTED_FORMAT: (
        "That image format is not one I can read. JPEG, PNG, WebP and HEIC all work."
    ),
    RejectionReason.TOO_MANY_PIXELS: (
        "That image is too large to open. A photo straight from a phone is fine."
    ),
    RejectionReason.CORRUPT: (
        "That photo did not arrive intact. Try attaching it again."
    ),
}
"""Visitor-facing copy, one line per reason.

Deliberately helpful rather than neutral: none of these outcomes says anything
about *content*, so there is nothing to be coy about, and "something went wrong"
in front of a size limit just makes a visitor try the same photo four times.
Stage 3 (Content Safety, issue #52) is the one whose refusal must stay neutral.

Nothing attacker-controlled is interpolated -- no filename, no declared type, no
byte excerpt -- so no message can be used to reflect content back at a visitor.
"""


class UploadRejectedError(Exception):
    """An upload that never became an image. Raised before anything is written.

    Attributes:
        reason: Which gate refused it.
        message: The line to show the visitor.
    """

    __slots__ = ("message", "reason")

    def __init__(self, reason: RejectionReason, message: str) -> None:
        super().__init__(f"{reason.value}: {message}")
        self.reason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class ValidImage:
    """Bytes that have passed every gate in stage 1, and what they turned out to be.

    :attr:`media_type` is what the *bytes* say. It is the only one of the two
    types here that anything downstream may act on.
    """

    data: bytes
    media_type: str
    width: int
    height: int
    declared_media_type: str | None = None

    @property
    def byte_size(self) -> int:
        """How many bytes arrived."""
        return len(self.data)

    @property
    def pixels(self) -> int:
        """How many pixels the header declares."""
        return self.width * self.height

    @property
    def declared_matches_bytes(self) -> bool:
        """Whether the request's content type agreed with the file.

        False is not an error -- see the module docstring -- but it is worth
        counting. A visitor's browser mislabelling a photo and an attacker
        dressing a payload as one look identical here; the difference is that
        only one of them also survived :func:`sniff`.
        """
        declared = _normalise_media_type(self.declared_media_type)
        if declared is None:
            return False
        return SUPPORTED_MEDIA_TYPES.get(declared) == SUPPORTED_MEDIA_TYPES.get(
            self.media_type
        )


def _normalise_media_type(raw: str | None) -> str | None:
    """Reduce ``image/JPEG; charset=binary`` to ``image/jpeg``.

    Args:
        raw: A media type as it arrived, or ``None``.

    Returns:
        The lowercased type without parameters, or ``None`` if there was none.
    """
    if raw is None:
        return None
    head = raw.split(";", 1)[0].strip().lower()
    return head or None


def sniff(data: bytes) -> str | None:
    """Identify an image from its leading bytes.

    Reads signatures only -- it never decodes, allocates per-pixel memory, or
    consults anything the request said about the file.

    Args:
        data: The uploaded bytes. Only the first :data:`_SNIFF_BYTES` are read.

    Returns:
        The media type the bytes are, or ``None`` if they are not a recognised
        image. ``None`` covers every disguised payload the endpoint will see:
        archives, executables, scripts, HTML, and SVG -- which is XML, has no
        signature, and would be the one image format that can carry script.
    """
    head = data[:_SNIFF_BYTES]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    # RIFF....WEBP -- the four bytes between are the chunk length.
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    # ISO base media: a length, then 'ftyp', then the brand that says what it is.
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "image/heic"
    return None


def validate(
    data: bytes,
    *,
    declared_media_type: str | None = None,
    limits: UploadLimits | None = None,
) -> ValidImage:
    """Run stage 1 over one upload.

    Args:
        data: The uploaded bytes, already read into memory -- which is safe
            because the caller enforced the same byte ceiling while reading.
        declared_media_type: What the request claimed the file was. Recorded,
            never trusted; pass it or do not, the verdict is identical.
        limits: The ceilings to enforce. Defaults to :class:`UploadLimits`.

    Returns:
        The :class:`ValidImage` stage 2 will normalize.

    Raises:
        UploadRejectedError: On any gate. Nothing has been written or decoded.
    """
    ceilings = limits if limits is not None else UploadLimits()

    if not data:
        raise rejection(RejectionReason.EMPTY, ceilings)
    if len(data) > ceilings.max_bytes:
        raise rejection(RejectionReason.TOO_LARGE, ceilings)

    media_type = sniff(data)
    if media_type is None:
        raise rejection(RejectionReason.NOT_AN_IMAGE, ceilings)
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise rejection(RejectionReason.UNSUPPORTED_FORMAT, ceilings)

    width, height = _header_size(data, media_type, ceilings)

    return ValidImage(
        data=data,
        media_type=media_type,
        width=width,
        height=height,
        declared_media_type=_normalise_media_type(declared_media_type),
    )


def _header_size(data: bytes, media_type: str, limits: UploadLimits) -> tuple[int, int]:
    """Read the declared dimensions, and refuse to open an image we would not allocate.

    ``Image.open`` parses the header and stops; the pixels are not read until
    something asks for them. That laziness is what makes this gate cheap, and it
    is why the pixel ceiling is checked here rather than in stage 2 -- by stage 2
    the allocation has already happened.

    Args:
        data: The uploaded bytes.
        media_type: What :func:`sniff` decided they are.
        limits: The ceilings to enforce.

    Returns:
        The declared ``(width, height)``.

    Raises:
        UploadRejectedError: If the header does not parse, disagrees with the
            signature, or declares more pixels than the ceiling allows.
    """
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            decoded_format = image.format
    except Image.DecompressionBombError as error:
        # Pillow's own backstop, which fires before ours can when a header
        # claims something absurd. Same verdict, reached earlier.
        raise rejection(RejectionReason.TOO_MANY_PIXELS, limits) from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        # A signature glued onto something else lands here, as does a photo
        # whose upload was cut short.
        raise rejection(RejectionReason.CORRUPT, limits) from error

    # The signature and the decoder must agree. They can disagree when a file
    # carries a valid signature for one format and a body that Pillow resolves
    # as another, which is the polyglot case -- and a file that is two formats
    # at once is not a photograph anybody took.
    if decoded_format != SUPPORTED_MEDIA_TYPES[media_type]:
        raise rejection(RejectionReason.CORRUPT, limits)

    if width <= 0 or height <= 0:
        raise rejection(RejectionReason.CORRUPT, limits)
    if width * height > limits.max_pixels:
        raise rejection(RejectionReason.TOO_MANY_PIXELS, limits)
    return width, height


def rejection(
    reason: RejectionReason, limits: UploadLimits | None = None
) -> UploadRejectedError:
    """Build the refusal for ``reason``, with the ceiling filled into the copy.

    Stage 2 raises through this too, so that every refusal a visitor can see is
    worded in one place.

    Args:
        reason: Which gate refused the upload.
        limits: The ceilings in force, for the copy that quotes one.

    Returns:
        The exception to raise.
    """
    ceilings = limits if limits is not None else UploadLimits()
    template = _MESSAGES[reason]
    return UploadRejectedError(
        reason, template.format(max_mb=ceilings.max_bytes / (1024 * 1024))
    )
