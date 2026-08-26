"""Stage 2. Strip the metadata, re-encode the pixels, downscale to what the model reads.

Three jobs, and only one of them is about cost.

**Strip metadata.** Photographs from strangers carry location data we have no
business holding, and "strip EXIF" is the wrong specification of the job.
Removing the EXIF block leaves XMP, which carries GPS too; leaves the JPEG
comment segment; leaves the MakerNote; leaves whatever the next phone vendor
invents. So this module does not remove metadata -- it never carries any across.
:func:`normalize` reads pixels out of the decoded image and builds a *new* image
from them, and a new image has no metadata to leak because it has never had any.
The orientation tag is the one thing read before it is dropped, because dropping
it without applying it is how phone photographs arrive at the model sideways.

**Re-encode.** The output is a JPEG this process wrote, byte for byte, from
pixel data. That neutralises a whole class of malformed-file payload: a crafted
chunk that trips a downstream decoder, a polyglot with an archive glued to its
tail, an oversized comment segment. None of it survives, because none of it is
copied -- only the pixels are, and pixels cannot encode a parser bug.

**Downscale.** To :attr:`~chip_chat.vision.limits.UploadLimits.max_edge`, and
never upward. A vision model scales the image into its own window before it
looks at it; sending 4032 x 3024 pays to transmit detail the model discards.

.. code-block:: python

    image = validate(payload, declared_media_type=content_type)
    photo = normalize(image)          # JPEG bytes, no metadata, <= max_edge
"""

from dataclasses import dataclass
from io import BytesIO
from typing import Final

from PIL import Image, ImageOps

from chip_chat.vision import decoders  # noqa: F401  -- registers the HEIC opener
from chip_chat.vision.limits import UploadLimits
from chip_chat.vision.validate import RejectionReason, ValidImage, rejection

__all__ = ["NormalizedImage", "normalize"]

NORMALIZED_MEDIA_TYPE: Final = "image/jpeg"
"""Everything leaves stage 2 as JPEG, whatever it arrived as."""

NORMALIZED_EXTENSION: Final = ".jpg"

_FLATTEN_ONTO: Final = (255, 255, 255)
"""White, for the transparent parts of a PNG or WebP.

JPEG has no alpha channel, and the alternative default -- black -- turns a
screenshot of a menu into an unreadable one.
"""


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    """What stage 2 produces: JPEG bytes with nothing attached to them."""

    data: bytes
    width: int
    height: int
    source_media_type: str
    """What the bytes were before this stage. Recorded for telemetry only."""

    media_type: str = NORMALIZED_MEDIA_TYPE

    @property
    def byte_size(self) -> int:
        """How many bytes will be written to Blob."""
        return len(self.data)


def normalize(
    image: ValidImage, *, limits: UploadLimits | None = None
) -> NormalizedImage:
    """Run stage 2 over a validated upload.

    Args:
        image: The output of :func:`~chip_chat.vision.validate.validate`. Only a
            validated image may be passed: the pixel ceiling that makes this
            decode safe to attempt is enforced there, from the header, before
            any memory is allocated for pixels.
        limits: The ceilings to work to. Defaults to :class:`UploadLimits`.

    Returns:
        The :class:`NormalizedImage` to write to Blob.

    Raises:
        UploadRejectedError: With
            :attr:`~chip_chat.vision.validate.RejectionReason.CORRUPT` if the
            pixels do not decode. Stage 1 reads the header and this is the first
            thing to read the body, so a file that is well-formed for exactly as
            long as a header parser looks at it is caught here.
    """
    ceilings = limits if limits is not None else UploadLimits()

    try:
        with Image.open(BytesIO(image.data)) as opened:
            # Orientation is read and applied here, then never written out.
            # Phone cameras record "the sensor was rotated" rather than rotating
            # the pixels, so a stripped-but-untransposed photo is a photo lying
            # on its side -- and the model will describe it that way.
            upright = ImageOps.exif_transpose(opened) or opened
            working = _flatten(upright)
            working = _downscale(working, ceilings.max_edge)
            pixels = working.tobytes()
            size = working.size
    except (OSError, ValueError, SyntaxError) as error:
        raise rejection(RejectionReason.CORRUPT, ceilings) from error

    # The metadata strip, and the reason it is a strip rather than a deletion:
    # this image is constructed from raw pixel data and has no `info` dictionary
    # to have missed something in. EXIF, XMP, MakerNote, ICC profile and JPEG
    # comments are all absent because none of them was ever attached.
    stripped = Image.frombytes("RGB", size, pixels)

    buffer = BytesIO()
    # `exif=b""` is redundant against a freshly constructed image and is passed
    # anyway: it is the line someone greps for, and it costs nothing to be true
    # twice.
    stripped.save(
        buffer,
        format="JPEG",
        quality=ceilings.jpeg_quality,
        optimize=True,
        progressive=True,
        exif=b"",
    )

    return NormalizedImage(
        data=buffer.getvalue(),
        width=size[0],
        height=size[1],
        source_media_type=image.media_type,
    )


def _flatten(image: Image.Image) -> Image.Image:
    """Return ``image`` in RGB, compositing any transparency onto white.

    Args:
        image: The decoded image, in whatever mode it arrived.

    Returns:
        An RGB image. Palette images go through RGBA first, because a palette
        can carry a transparency index that a direct ``convert("RGB")`` drops
        into an arbitrary colour.
    """
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, _FLATTEN_ONTO)
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def _downscale(image: Image.Image, max_edge: int) -> Image.Image:
    """Shrink ``image`` so its longest edge is at most ``max_edge``.

    Never enlarges. A small photograph is a small photograph; upscaling it would
    cost tokens to send interpolated pixels that carry no information.

    Args:
        image: The image to fit.
        max_edge: The longest edge to allow, in pixels.

    Returns:
        The image, resized only if it was too big.
    """
    longest = max(image.size)
    if longest <= max_edge:
        return image
    scale = max_edge / longest
    # At least one pixel on each side: a 4000 x 3 panorama must not round to zero.
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    return image.resize((width, height), Image.Resampling.LANCZOS)
