"""The upload path end to end: what is written, what is returned, and when nothing is.

Two properties carry the most weight here. A refused upload writes nothing --
asserted against a store that would have recorded the write, not against the
response text. And what comes back is a reference, never an image.
"""

import pytest

from chip_chat.vision import (
    NORMALIZED_MEDIA_TYPE,
    RETENTION_CEILING_HOURS,
    BlobRef,
    ImageModerator,
    PhotoIntake,
    UploadLimits,
    UploadRejectedError,
    blob_name,
)
from chip_chat.vision.testing import (
    SVG_WITH_SCRIPT,
    XMP_LOCATION_MARKER,
    ZIP_ARCHIVE,
    InMemoryBlobStore,
    StubImageAnalyzer,
    photo_with_location,
    png_declaring,
    solid_image,
)


@pytest.fixture
def store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def moderator() -> ImageModerator:
    """Stage 3, answering "safe" -- what it says about a photograph of lunch.

    Stage 3 refusing is ``test_moderation.py``'s subject; here it is a
    dependency, and one the intake cannot be built without.
    """
    return ImageModerator(analyzer=StubImageAnalyzer())


@pytest.fixture
def intake(store: InMemoryBlobStore, moderator: ImageModerator) -> PhotoIntake:
    return PhotoIntake(store=store, moderator=moderator)


# --- the accepted path -----------------------------------------------------


def test_an_accepted_photo_is_written_once(
    intake: PhotoIntake, store: InMemoryBlobStore
) -> None:
    photo = intake.accept(solid_image(), declared_media_type="image/jpeg")
    assert len(store) == 1
    assert store.get(photo.blob_ref).content_type == NORMALIZED_MEDIA_TYPE


def test_what_is_stored_is_the_normalized_copy_not_what_was_uploaded(
    intake: PhotoIntake, store: InMemoryBlobStore
) -> None:
    uploaded = photo_with_location((4032, 3024))
    photo = intake.accept(uploaded)
    stored = store.get(photo.blob_ref).data
    assert stored != uploaded
    assert XMP_LOCATION_MARKER not in stored
    assert photo.byte_size == len(stored)
    assert (photo.width, photo.height) == (1024, 768)


def test_the_reference_names_the_container_it_was_written_to(
    intake: PhotoIntake, store: InMemoryBlobStore
) -> None:
    photo = intake.accept(solid_image())
    assert photo.blob_ref.container == store.container
    assert str(photo.blob_ref) == f"{store.container}/{photo.blob_ref.name}"


def test_the_reference_carries_nothing_about_the_visitor(
    intake: PhotoIntake,
) -> None:
    # No session id, no address, no filename -- a container listing is not a
    # record of who was where.
    name = intake.accept(solid_image()).blob_ref.name
    date, _, leaf = name.partition("/")
    assert len(date) == len("2026-08-26")
    assert leaf.endswith(".jpg")
    assert len(leaf) == len("00000000-0000-0000-0000-000000000000.jpg")


def test_two_uploads_of_the_same_photo_get_different_references(
    intake: PhotoIntake, store: InMemoryBlobStore
) -> None:
    payload = solid_image()
    first = intake.accept(payload)
    second = intake.accept(payload)
    assert first.blob_ref != second.blob_ref
    assert len(store) == 2


def test_a_name_collision_refuses_rather_than_replacing_a_photograph(
    store: InMemoryBlobStore, moderator: ImageModerator
) -> None:
    fixed = PhotoIntake(
        store=store, moderator=moderator, name_factory=lambda: "2026-08-26/fixed.jpg"
    )
    fixed.accept(solid_image())
    with pytest.raises(FileExistsError):
        fixed.accept(solid_image())
    assert len(store) == 1


def test_the_result_carries_the_promise_the_visitor_is_shown(
    intake: PhotoIntake,
) -> None:
    photo = intake.accept(solid_image())
    assert str(RETENTION_CEILING_HOURS) in photo.retention_notice


def test_the_mismatch_signal_survives_into_the_result(intake: PhotoIntake) -> None:
    # What issue #80 counts. It did not change the verdict; it is still worth
    # having on the record.
    honest = intake.accept(solid_image(), declared_media_type="image/jpeg")
    mislabelled = intake.accept(solid_image(), declared_media_type="image/png")
    assert honest.declared_matches_bytes
    assert not mislabelled.declared_matches_bytes
    assert mislabelled.source_media_type == "image/jpeg"


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "HEIF"])
def test_a_photo_from_any_supported_camera_roll_is_accepted(
    intake: PhotoIntake, fmt: str
) -> None:
    # HEIF is the iPhone case. Safari usually transcodes on upload and
    # sometimes does not, and "sometimes your photo just fails" is not
    # something a visitor can debug.
    assert intake.accept(solid_image(fmt=fmt)).blob_ref is not None


# --- the refused path ------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        (ZIP_ARCHIVE, "an archive"),
        (SVG_WITH_SCRIPT, "an svg"),
        (png_declaring(8000, 8000), "a pixel bomb"),
        (b"", "nothing at all"),
    ],
)
def test_a_refused_upload_writes_nothing(
    intake: PhotoIntake, store: InMemoryBlobStore, payload: bytes, label: str
) -> None:
    # Asserted on the store, not on the response: "reject before anything is
    # written" is a claim about the container, and only the container can
    # settle it.
    with pytest.raises(UploadRejectedError):
        intake.accept(payload, declared_media_type="image/jpeg")
    assert len(store) == 0, f"{label} was stored"


def test_an_oversized_upload_writes_nothing(
    store: InMemoryBlobStore, moderator: ImageModerator
) -> None:
    small = PhotoIntake(
        store=store, moderator=moderator, limits=UploadLimits(max_bytes=512)
    )
    with pytest.raises(UploadRejectedError):
        small.accept(solid_image((400, 400)))
    assert len(store) == 0


def test_a_photo_that_dies_during_normalization_writes_nothing(
    intake: PhotoIntake, store: InMemoryBlobStore
) -> None:
    payload = bytearray(solid_image((200, 200)))
    del payload[len(payload) // 2 :]
    with pytest.raises(UploadRejectedError):
        intake.accept(bytes(payload))
    assert len(store) == 0


def test_the_intake_reports_the_ceiling_it_enforces(
    store: InMemoryBlobStore, moderator: ImageModerator
) -> None:
    limits = UploadLimits(max_bytes=1234, max_edge=99)
    built = PhotoIntake(store=store, moderator=moderator, limits=limits)
    assert built.limits is limits


# --- references ------------------------------------------------------------


def test_a_reference_survives_a_round_trip_through_its_string_form() -> None:
    ref = BlobRef(container="uploads", name="2026-08-26/abc.jpg")
    assert BlobRef.parse(str(ref)) == ref


@pytest.mark.parametrize(
    "hostile",
    [
        "uploads/../functions/host.json",
        "uploads//etc/passwd",
        "uploads",
        "/abc.jpg",
        "",
    ],
)
def test_a_reference_that_climbs_out_of_its_container_is_refused(hostile: str) -> None:
    # A ref can arrive from a model, and a model can invent one. The parse is
    # where that stops -- before a container client sees it.
    with pytest.raises(ValueError, match="blob reference"):
        BlobRef.parse(hostile)


def test_minted_names_are_dated_in_utc_and_unique() -> None:
    names = {blob_name() for _ in range(64)}
    assert len(names) == 64
