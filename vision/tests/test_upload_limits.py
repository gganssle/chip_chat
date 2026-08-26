"""The ceilings themselves: what they default to, and what they refuse to be.

A limit configured to zero is not a limit, and a limit whose environment
variable is a typo is a limit that silently is not there. Both fail here rather
than on the first hostile upload.
"""

import pytest

from chip_chat.vision import SUPPORTED_MEDIA_TYPES, UploadLimits
from chip_chat.vision.decoders import HEIF_AVAILABLE
from chip_chat.vision.store import (
    ACCOUNT_VARIABLE,
    CONTAINER_VARIABLE,
    AzureBlobStore,
)


def test_the_defaults_bound_the_read_the_decode_and_the_send() -> None:
    limits = UploadLimits()
    assert limits.max_bytes == 8 * 1024 * 1024
    # The one that a byte ceiling cannot express, and the reason there are two.
    assert limits.max_pixels > limits.max_bytes
    assert limits.max_edge == 1024


@pytest.mark.parametrize("field", ["max_bytes", "max_pixels", "max_edge"])
@pytest.mark.parametrize("value", [0, -1])
def test_a_ceiling_that_would_not_bound_anything_is_refused(
    field: str, value: int
) -> None:
    with pytest.raises(ValueError, match=field):
        UploadLimits(**{field: value})


@pytest.mark.parametrize("quality", [0, 96, 100, -5])
def test_a_meaningless_jpeg_quality_is_refused(quality: int) -> None:
    with pytest.raises(ValueError, match="jpeg_quality"):
        UploadLimits(jpeg_quality=quality)


def test_the_environment_overrides_every_ceiling() -> None:
    limits = UploadLimits.from_env(
        {
            "CHIP_CHAT_UPLOAD_MAX_BYTES": "1024",
            "CHIP_CHAT_UPLOAD_MAX_PIXELS": "2048",
            "CHIP_CHAT_UPLOAD_MAX_EDGE": "512",
            "CHIP_CHAT_UPLOAD_JPEG_QUALITY": "70",
        }
    )
    assert (limits.max_bytes, limits.max_pixels) == (1024, 2048)
    assert (limits.max_edge, limits.jpeg_quality) == (512, 70)


def test_an_empty_variable_means_absent_rather_than_zero() -> None:
    # A blank line in .env.example must not disarm a ceiling.
    assert UploadLimits.from_env({"CHIP_CHAT_UPLOAD_MAX_BYTES": "  "}) == UploadLimits()


def test_an_unparseable_ceiling_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="invalid literal"):
        UploadLimits.from_env({"CHIP_CHAT_UPLOAD_MAX_EDGE": "1024px"})


def test_an_environment_ceiling_of_zero_is_still_refused() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        UploadLimits.from_env({"CHIP_CHAT_UPLOAD_MAX_BYTES": "0"})


# --- the allowlist ---------------------------------------------------------


def test_the_heic_decoder_actually_registered() -> None:
    # A registration that silently did not run costs a share of every iOS
    # upload, and looks exactly like an unsupported format from outside.
    assert HEIF_AVAILABLE


def test_every_allowed_type_names_a_format_pillow_can_open() -> None:
    from PIL import Image

    for media_type, fmt in SUPPORTED_MEDIA_TYPES.items():
        assert fmt in Image.OPEN, f"{media_type} maps to an unregistered decoder"


def test_svg_is_not_in_the_allowlist() -> None:
    # Enumerated rather than assumed: it is the one image format that can carry
    # script, and the one that would arrive if the allowlist were ever widened
    # to "whatever the browser calls an image".
    assert "image/svg+xml" not in SUPPORTED_MEDIA_TYPES


# --- the store's configuration ---------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {ACCOUNT_VARIABLE: "stchipchat"},
        {CONTAINER_VARIABLE: "uploads"},
        {ACCOUNT_VARIABLE: "  ", CONTAINER_VARIABLE: "uploads"},
    ],
)
def test_an_unconfigured_upload_store_fails_at_startup(env: dict[str, str]) -> None:
    # The alternative is discovering it on the first visitor who attaches a photo.
    with pytest.raises(RuntimeError, match="not configured"):
        AzureBlobStore.from_env(env)
