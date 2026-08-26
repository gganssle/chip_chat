"""The numbers stage 1 enforces and stage 2 works to, and where they come from.

Every ceiling here is deliberately small. The upload endpoint is public and
unauthenticated, so each of these is the difference between a hostile request
costing nothing and it costing CPU, memory or an invoice:

=========================== ===================================================
Ceiling                     Why it exists
=========================== ===================================================
:attr:`~UploadLimits.max_bytes`      Bounds the read before anything is decoded.
:attr:`~UploadLimits.max_pixels`     Bounds the *decode*, which the byte ceiling
                                     does not -- a 4 KB PNG can declare 40000 x
                                     40000 and ask for 6 GB of RAM.
:attr:`~UploadLimits.max_edge`       The model's working resolution. Anything
                                     larger is paid for and thrown away.
=========================== ===================================================

The media-type allowlist is the third: :data:`SUPPORTED_MEDIA_TYPES` is what the
*bytes* are allowed to be, never what the request claimed they were. See
:mod:`chip_chat.vision.validate` for why that distinction is the whole of stage 1.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "SUPPORTED_MEDIA_TYPES",
    "UploadLimits",
]

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
"""Eight mebibytes. A 12-megapixel phone JPEG is 3-5 MB; HEIC is smaller still."""

DEFAULT_MAX_PIXELS = 50_000_000
"""Fifty megapixels, checked from the header before a single row is decoded.

Comfortably above any phone camera and far below the point where a decode
becomes a denial of service. This is the ceiling that a byte ceiling cannot
express, because compression ratio is chosen by whoever uploads the file.
"""

DEFAULT_MAX_EDGE = 1024
"""Longest edge, in pixels, of the image the model actually sees.

Vision models bill by tile, not by megapixel: an image is scaled to fit the
model's window and then counted in 512-pixel tiles. At 1024 the longest edge the
short side lands at 768 or below for every ordinary phone aspect ratio, which is
the cheapest framing that still gets the full-detail path. Sending 4032 x 3024
instead costs more and is downscaled by the provider anyway -- a photograph of a
burrito bowl does not become more legible above a thousand pixels.
"""

DEFAULT_JPEG_QUALITY = 85
"""High enough that re-encoding is invisible, low enough that the blob is small."""

_ENV_PREFIX = "CHIP_CHAT_"

SUPPORTED_MEDIA_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/webp": "WEBP",
        "image/heic": "HEIF",
        "image/heif": "HEIF",
    }
)
"""Media types the decoder will accept, mapped to the Pillow format that reads them.

HEIC is here because it is what an iPhone camera roll holds. Safari usually
transcodes to JPEG on upload and sometimes does not, and "sometimes the photo
you picked just fails" is not a thing a visitor can debug. Reading it costs one
dependency; not reading it costs a share of every iOS upload.
"""


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(_ENV_PREFIX + key, "").strip()
    if not raw:
        return default
    return int(raw)


@dataclass(frozen=True, slots=True)
class UploadLimits:
    """Every ceiling the upload path enforces, validated on construction."""

    max_bytes: int = DEFAULT_MAX_BYTES
    max_pixels: int = DEFAULT_MAX_PIXELS
    max_edge: int = DEFAULT_MAX_EDGE
    jpeg_quality: int = DEFAULT_JPEG_QUALITY

    def __post_init__(self) -> None:
        """Refuse a configuration that would not actually bound anything.

        Raises:
            ValueError: If any ceiling is not positive, or the JPEG quality is
                outside the 1-95 range Pillow treats as meaningful.
        """
        positive = {
            "max_bytes": self.max_bytes,
            "max_pixels": self.max_pixels,
            "max_edge": self.max_edge,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        # Above 95 Pillow's own documentation calls the result "almost no
        # increase in quality" for a large increase in size, so treating it as a
        # typo is kinder than honouring it.
        if not 1 <= self.jpeg_quality <= 95:
            raise ValueError(f"jpeg_quality must be in 1..95, got {self.jpeg_quality}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "UploadLimits":
        """Build limits from the environment.

        Reads ``CHIP_CHAT_UPLOAD_MAX_BYTES``, ``CHIP_CHAT_UPLOAD_MAX_PIXELS``,
        ``CHIP_CHAT_UPLOAD_MAX_EDGE`` and ``CHIP_CHAT_UPLOAD_JPEG_QUALITY``.
        Every one is optional.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            The configured limits.

        Raises:
            ValueError: If a value is unparseable or would not bound anything.
        """
        source = os.environ if env is None else env
        return cls(
            max_bytes=_positive_int(source, "UPLOAD_MAX_BYTES", DEFAULT_MAX_BYTES),
            max_pixels=_positive_int(source, "UPLOAD_MAX_PIXELS", DEFAULT_MAX_PIXELS),
            max_edge=_positive_int(source, "UPLOAD_MAX_EDGE", DEFAULT_MAX_EDGE),
            jpeg_quality=_positive_int(
                source, "UPLOAD_JPEG_QUALITY", DEFAULT_JPEG_QUALITY
            ),
        )
