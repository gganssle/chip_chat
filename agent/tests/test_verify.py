"""The verification image has to be correct, or the check it feeds proves nothing.

The vision check fails a deployment that names the wrong colours. That inference
is only sound if the image really does hold the colours the check expects, so the
generator is tested against a decoded PNG rather than trusted.
"""

import struct
import zlib

from chip_chat.agent.verify import _QUADRANTS, _png, _quadrant_image


def _decode(png: bytes) -> tuple[int, int, bytes]:
    """Decode a non-interlaced 8-bit RGB PNG with no row filtering.

    Deliberately not Pillow: this is the independent reader that keeps the
    encoder honest, and reusing the encoder's own assumptions would defeat it.
    """
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    position = 8
    width = height = 0
    compressed = b""
    while position < len(png):
        (length,) = struct.unpack(">I", png[position : position + 4])
        kind = png[position + 4 : position + 8]
        payload = png[position + 8 : position + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour_type = struct.unpack(">2I2B", payload[:10])
            assert (depth, colour_type) == (8, 2)
        elif kind == b"IDAT":
            compressed += payload
        position += 12 + length

    raw = zlib.decompress(compressed)
    stride = width * 3 + 1
    rows = []
    for y in range(height):
        assert raw[y * stride] == 0, "filter byte must be None"
        rows.append(raw[y * stride + 1 : (y + 1) * stride])
    return width, height, b"".join(rows)


def test_png_round_trips_through_an_independent_reader() -> None:
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])

    width, height, decoded = _decode(_png(2, 2, pixels))

    assert (width, height) == (2, 2)
    assert decoded == pixels


def test_the_quadrant_image_holds_the_colours_the_check_asserts_on() -> None:
    size = 64
    width, height, pixels = _decode(_quadrant_image(size))
    assert (width, height) == (size, size)

    def pixel(x: int, y: int) -> tuple[int, ...]:
        offset = (y * size + x) * 3
        return tuple(pixels[offset : offset + 3])

    quarter, three_quarters = size // 4, size * 3 // 4
    corners = [
        pixel(quarter, quarter),  # top-left
        pixel(three_quarters, quarter),  # top-right
        pixel(quarter, three_quarters),  # bottom-left
        pixel(three_quarters, three_quarters),  # bottom-right
    ]

    assert corners == [rgb for _, rgb in _QUADRANTS]


def test_the_four_colours_are_distinct() -> None:
    """A repeated colour would make a wrong answer scoreable as right."""
    assert len({rgb for _, rgb in _QUADRANTS}) == len(_QUADRANTS)
    assert len({name for name, _ in _QUADRANTS}) == len(_QUADRANTS)


def test_the_default_size_is_the_one_the_model_can_actually_read() -> None:
    """768, not 256. See _quadrant_image's docstring for the sweep behind it."""
    width, height, _ = _decode(_quadrant_image())

    assert (width, height) == (768, 768)
