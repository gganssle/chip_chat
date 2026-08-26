"""Stage 2: metadata gone, pixels re-encoded, size brought down to what a model reads.

The EXIF assertions here deliberately do not stop at
:meth:`~PIL.Image.Image.getexif`. A stored object can report no EXIF and still
carry the same GPS coordinates in an XMP packet, which is a place phones write
them and a place "strip EXIF" implemented literally does not look.
"""

from io import BytesIO

import pytest
from PIL import Image

from chip_chat.vision import UploadLimits, normalize, validate
from chip_chat.vision.testing import (
    GPS_LATITUDE_DEGREES,
    JPEG_COMMENT,
    XMP_LOCATION_MARKER,
    ZIP_ARCHIVE,
    photo_with_location,
    solid_image,
)
from chip_chat.vision.validate import RejectionReason, UploadRejectedError


def _run(payload: bytes, limits: UploadLimits | None = None) -> bytes:
    """Validate and normalize ``payload``, returning the stored bytes."""
    return normalize(validate(payload, limits=limits), limits=limits).data


# --- metadata --------------------------------------------------------------


def test_the_fixture_really_does_carry_location_data_to_begin_with() -> None:
    # Otherwise every assertion below passes for the wrong reason.
    original = photo_with_location()
    assert Image.open(BytesIO(original)).getexif()
    assert XMP_LOCATION_MARKER in original
    assert JPEG_COMMENT in original


def test_the_stored_object_reports_no_exif() -> None:
    assert not Image.open(BytesIO(_run(photo_with_location()))).getexif()


def test_the_stored_bytes_contain_no_exif_segment_at_all() -> None:
    stored = _run(photo_with_location())
    assert b"Exif\x00\x00" not in stored
    assert b"ChipChatFixtures" not in stored


def test_location_does_not_survive_in_xmp_either() -> None:
    # The reason stage 2 rebuilds the image from raw pixels rather than deleting
    # the EXIF block: XMP carries GPS too, and deleting EXIF leaves it behind.
    stored = _run(photo_with_location())
    assert XMP_LOCATION_MARKER not in stored
    assert b"xmpmeta" not in stored
    assert str(GPS_LATITUDE_DEGREES).encode() not in stored[:512]


def test_the_jpeg_comment_segment_does_not_survive() -> None:
    assert JPEG_COMMENT not in _run(photo_with_location())


def test_pillow_finds_no_metadata_of_any_kind_on_the_stored_object() -> None:
    stored = Image.open(BytesIO(_run(photo_with_location())))
    assert "exif" not in stored.info
    assert "xmp" not in stored.info
    assert "icc_profile" not in stored.info
    assert "comment" not in stored.info


# --- orientation -----------------------------------------------------------


def test_a_phone_photo_is_turned_upright_before_the_tag_is_dropped() -> None:
    # Orientation 6 means "rotate 90 clockwise to display". Dropping the tag
    # without applying it is how a photograph reaches the model on its side --
    # and the model then describes a photograph on its side.
    upright = Image.open(BytesIO(_run(photo_with_location((80, 40), orientation=6))))
    assert upright.size == (40, 80)


def test_an_unrotated_photo_is_left_alone() -> None:
    stored = Image.open(BytesIO(_run(photo_with_location((80, 40), orientation=1))))
    assert stored.size == (80, 40)


def test_the_pixels_move_with_the_orientation_not_just_the_dimensions() -> None:
    # The fixture paints its top-left corner blue. Under orientation 6 that
    # corner belongs at the top right once the photo is upright.
    stored = Image.open(BytesIO(_run(photo_with_location((80, 80), orientation=6))))
    top_right = stored.convert("RGB").getpixel((stored.width - 4, 4))
    assert isinstance(top_right, tuple)
    assert top_right[2] > top_right[0]  # more blue than red


# --- re-encoding -----------------------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "HEIF"])
def test_everything_leaves_as_jpeg_whatever_it_arrived_as(fmt: str) -> None:
    result = normalize(validate(solid_image((60, 40), fmt=fmt)))
    assert result.media_type == "image/jpeg"
    assert Image.open(BytesIO(result.data)).format == "JPEG"


def test_the_source_format_is_recorded_even_though_the_output_is_not_it() -> None:
    result = normalize(validate(solid_image(fmt="PNG")))
    assert result.source_media_type == "image/png"


def test_a_payload_appended_to_a_real_photo_does_not_survive_re_encoding() -> None:
    # A polyglot: a valid JPEG that a decoder reads happily, with an archive
    # glued to its tail for whatever reads it next. Only pixels are copied
    # forward, so the tail is simply not there afterwards.
    polyglot = solid_image() + ZIP_ARCHIVE
    assert ZIP_ARCHIVE in polyglot
    assert ZIP_ARCHIVE not in _run(polyglot)


def test_transparency_is_flattened_onto_white_rather_than_black() -> None:
    # JPEG has no alpha. Compositing onto black turns a screenshot of a menu
    # into an unreadable one.
    transparent = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    buffer = BytesIO()
    transparent.save(buffer, format="PNG")
    stored = Image.open(BytesIO(_run(buffer.getvalue()))).convert("RGB")
    assert stored.getpixel((10, 10)) == (255, 255, 255)


def test_a_photo_whose_body_is_corrupt_is_refused_rather_than_stored() -> None:
    # The header parses -- stage 1 passed it -- and the pixels do not. Stage 2
    # is the first thing to read the body, so this is where it shows up.
    payload = bytearray(solid_image((200, 200)))
    del payload[len(payload) // 2 :]
    image = validate(bytes(payload))
    with pytest.raises(UploadRejectedError) as caught:
        normalize(image)
    assert caught.value.reason is RejectionReason.CORRUPT


# --- downscaling -----------------------------------------------------------


def test_a_phone_sized_photo_comes_down_to_the_models_working_resolution() -> None:
    result = normalize(validate(solid_image((4032, 3024))))
    assert (result.width, result.height) == (1024, 768)


def test_a_portrait_photo_is_bounded_by_its_long_edge_too() -> None:
    result = normalize(validate(solid_image((3024, 4032))))
    assert max(result.width, result.height) == 1024


def test_a_small_photo_is_never_enlarged() -> None:
    # Upscaling would pay tokens to send interpolated pixels carrying nothing.
    result = normalize(validate(solid_image((200, 150))))
    assert (result.width, result.height) == (200, 150)


def test_an_extreme_aspect_ratio_keeps_at_least_one_pixel_on_the_short_side() -> None:
    limits = UploadLimits(max_edge=64)
    result = normalize(validate(solid_image((4000, 3)), limits=limits), limits=limits)
    assert (result.width, result.height) == (64, 1)


def test_downscaling_makes_the_stored_object_smaller_than_what_arrived() -> None:
    payload = photo_with_location((4032, 3024))
    result = normalize(validate(payload))
    assert result.byte_size < len(payload)
