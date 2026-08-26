"""Stage 1: what gets in, what does not, and what decides.

The property under test throughout is that the *bytes* decide. Several of these
tests exist only to prove that the declared content type changes nothing --
which is a strange thing to test until you remember that the declared content
type is the thing an attacker sets.
"""

import pytest

from chip_chat.vision import UploadLimits, sniff, validate
from chip_chat.vision.testing import (
    SVG_WITH_SCRIPT,
    ZIP_ARCHIVE,
    png_declaring,
    solid_image,
)
from chip_chat.vision.validate import RejectionReason, UploadRejectedError


def _reason(payload: bytes, **kwargs: object) -> RejectionReason:
    """Validate ``payload``, expecting a refusal, and return why."""
    with pytest.raises(UploadRejectedError) as caught:
        validate(payload, **kwargs)  # type: ignore[arg-type]
    return caught.value.reason


# --- sniff -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
)
def test_sniff_names_the_format_from_the_first_bytes(fmt: str, expected: str) -> None:
    assert sniff(solid_image(fmt=fmt)) == expected


def test_sniff_recognises_heic_by_its_brand_not_merely_by_ftyp() -> None:
    heic = solid_image(fmt="HEIF")
    assert sniff(heic) == "image/heic"
    # An MP4 shares the ftyp box; only the brand separates a still from a video.
    video = bytearray(heic)
    video[8:12] = b"mp42"
    assert sniff(bytes(video)) is None


def test_sniff_declines_anything_without_an_image_signature(
    disguised_payload: bytes,
) -> None:
    assert sniff(disguised_payload) is None


def test_sniff_declines_an_empty_upload() -> None:
    assert sniff(b"") is None


# --- the four gates --------------------------------------------------------


def test_an_empty_upload_is_refused() -> None:
    assert _reason(b"") is RejectionReason.EMPTY


def test_an_oversized_upload_is_refused() -> None:
    limits = UploadLimits(max_bytes=1024)
    assert _reason(b"\xff\xd8\xff" + b"\x00" * 4096, limits=limits) is (
        RejectionReason.TOO_LARGE
    )


def test_the_size_gate_runs_before_anything_looks_at_the_content() -> None:
    # A twenty-megabyte payload must cost one length check, not a parse.
    limits = UploadLimits(max_bytes=1024)
    assert _reason(b"\x00" * 20_000_000, limits=limits) is RejectionReason.TOO_LARGE


def test_the_size_message_quotes_the_ceiling_a_visitor_has_to_get_under() -> None:
    with pytest.raises(UploadRejectedError) as caught:
        validate(b"\x00" * 3_000_000, limits=UploadLimits(max_bytes=2 * 1024 * 1024))
    assert "2 MB" in caught.value.message


def test_a_disguised_payload_is_refused_on_its_bytes(
    disguised_payload: bytes,
) -> None:
    # Every one of these claims to be a photograph. None of them is.
    assert _reason(disguised_payload, declared_media_type="image/jpeg") is (
        RejectionReason.NOT_AN_IMAGE
    )


def test_an_svg_is_refused_even_though_it_is_genuinely_an_image_format() -> None:
    # It is XML, it has no signature, and it can carry script. The allowlist is
    # a list of signatures for exactly this reason.
    assert _reason(SVG_WITH_SCRIPT, declared_media_type="image/svg+xml") is (
        RejectionReason.NOT_AN_IMAGE
    )


def test_a_signature_glued_onto_something_else_is_refused() -> None:
    # The next thing you try after a bare zip is refused: prepend the magic
    # bytes. It gets past the sniff and dies on the header.
    assert _reason(b"\xff\xd8\xff" + ZIP_ARCHIVE) is RejectionReason.CORRUPT


def test_a_truncated_photo_is_refused() -> None:
    assert _reason(solid_image()[:20]) is RejectionReason.CORRUPT


def test_a_header_declaring_more_pixels_than_we_will_allocate_is_refused() -> None:
    bomb = png_declaring(8000, 8000)
    # Sixty-four megapixels in sixty-seven bytes. No byte ceiling catches this.
    assert len(bomb) < 1024
    assert _reason(bomb) is RejectionReason.TOO_MANY_PIXELS


def test_an_absurd_header_hits_pillows_own_backstop_as_the_same_refusal() -> None:
    # Above twice Pillow's ceiling the decoder raises before ours can look.
    # The visitor should still see a refusal rather than a 500.
    assert _reason(png_declaring(20000, 20000)) is RejectionReason.TOO_MANY_PIXELS


# --- the declared type decides nothing -------------------------------------


@pytest.mark.parametrize(
    "declared",
    [None, "image/png", "image/jpeg", "application/octet-stream", "text/html", ""],
)
def test_the_verdict_does_not_depend_on_what_the_request_claimed(
    declared: str | None,
) -> None:
    image = validate(solid_image(fmt="JPEG"), declared_media_type=declared)
    assert image.media_type == "image/jpeg"


@pytest.mark.parametrize(
    "declared",
    [None, "image/png", "application/octet-stream", "text/html"],
)
def test_a_lie_about_the_type_does_not_get_a_payload_in(declared: str | None) -> None:
    assert _reason(ZIP_ARCHIVE, declared_media_type=declared) is (
        RejectionReason.NOT_AN_IMAGE
    )


def test_a_mismatch_is_recorded_rather_than_refused() -> None:
    # iOS sends application/octet-stream for camera-roll photos often enough
    # that refusing on mismatch would refuse real visitors. It is counted, for
    # the abuse work in issue #80, and it is not a verdict.
    image = validate(solid_image(fmt="JPEG"), declared_media_type="image/png")
    assert image.media_type == "image/jpeg"
    assert image.declared_media_type == "image/png"
    assert not image.declared_matches_bytes


def test_agreement_is_recorded_too_and_survives_a_charset_parameter() -> None:
    image = validate(solid_image(fmt="JPEG"), declared_media_type="IMAGE/JPEG; q=1")
    assert image.declared_media_type == "image/jpeg"
    assert image.declared_matches_bytes


def test_heic_and_heif_are_the_same_file_by_two_names() -> None:
    image = validate(solid_image(fmt="HEIF"), declared_media_type="image/heif")
    assert image.media_type == "image/heic"
    assert image.declared_matches_bytes


# --- what a valid image reports --------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "HEIF"])
def test_every_supported_format_is_read_without_decoding_it(fmt: str) -> None:
    payload = solid_image((96, 64), fmt=fmt)
    image = validate(payload)
    assert (image.width, image.height) == (96, 64)
    assert image.pixels == 96 * 64
    assert image.byte_size == len(payload)
    assert image.data == payload


def test_no_refusal_message_repeats_anything_the_uploader_controlled() -> None:
    # A message that echoes a filename or a declared type is a reflection bug
    # waiting for a renderer that trusts it.
    hostile = "<script>alert(1)</script>/../../etc/passwd"
    with pytest.raises(UploadRejectedError) as caught:
        validate(ZIP_ARCHIVE, declared_media_type=hostile)
    assert "script" not in caught.value.message
    assert "passwd" not in caught.value.message
