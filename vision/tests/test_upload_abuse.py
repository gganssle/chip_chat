"""The adversarial suite: the upload endpoint attacked on purpose, issue #80.

``test_validate.py`` asks whether the gates work. This file asks what an
attacker gets for trying, which is a different question and produces different
tests. Every one of them is a permanent regression test for an attack the
endpoint has to survive, and they are grouped by the attack rather than by the
module that happens to refuse it -- because which module refuses is an
implementation detail and "a decompression bomb does not take the container
down" is not.

Three properties are asserted here that no other file asserts:

**Refusing costs a bounded amount of work.** Not "the verdict was TOO_LARGE" --
that is ``test_validate.py``'s job -- but *how many bytes were read* and
*whether the pixels were ever allocated*. A gate that reaches the right verdict
after decoding forty megapixels has not defended anything.

**A refusal that looked inside the file says nothing about what it found.** The
message for a disguised payload is byte-for-byte the message for a flagged one,
so an attacker cannot tell detection from moderation and cannot iterate against
either.

**A payload that survives the gates is neutralised anyway.** The polyglot is the
case: stage 1 accepts it, correctly, and stage 2 throws the payload away because
it copies pixels rather than bytes.
"""

import asyncio

import pytest
from PIL import Image, ImageFile

from chip_chat.vision import (
    NORMALIZED_MEDIA_TYPE,
    ImageModerator,
    PhotoIntake,
    UploadLimits,
    UploadRejectedError,
    content_length,
    read_upload,
    read_upload_async,
    validate,
)
from chip_chat.vision.normalize import normalize
from chip_chat.vision.testing import (
    ZIP_ARCHIVE,
    AsyncScriptedStream,
    InMemoryBlobStore,
    ManualMonotonic,
    ScriptedStream,
    StubImageAnalyzer,
    TricklingStream,
    jpeg_signed_but_not_jpeg,
    jpeg_with_appended_archive,
    png_declaring,
    solid_image,
)
from chip_chat.vision.validate import _MESSAGES, RejectionReason

_CHUNK = 64 * 1024
"""What the reader asks for per read. Mirrored here so the arithmetic is visible."""


@pytest.fixture
def store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def intake(store: InMemoryBlobStore) -> PhotoIntake:
    """A whole intake, because most of these attacks are aimed at the whole path."""
    return PhotoIntake(store=store, moderator=ImageModerator(StubImageAnalyzer()))


def _reason(payload: bytes, **kwargs: object) -> RejectionReason:
    """Validate ``payload``, expecting a refusal, and return why."""
    with pytest.raises(UploadRejectedError) as caught:
        validate(payload, **kwargs)  # type: ignore[arg-type]
    return caught.value.reason


# --- oversized files -------------------------------------------------------
#
# The attack is not "a big file". It is "a big file that we read all of before
# noticing", which is why every test here asserts on the stream and not only on
# the verdict.


def test_a_body_over_the_ceiling_stops_being_read_at_the_ceiling() -> None:
    limits = UploadLimits(max_bytes=256 * 1024)
    stream = ScriptedStream(b"\x00" * 20_000_000, chunk_size=_CHUNK)

    with pytest.raises(UploadRejectedError) as caught:
        read_upload(stream, limits=limits)

    assert caught.value.reason is RejectionReason.TOO_LARGE
    # The ceiling is crossed by at most one chunk, so twenty megabytes cost us
    # a quarter of one. A reader that buffered the body first would have taken
    # all twenty and only then had an opinion.
    assert stream.bytes_delivered <= limits.max_bytes + _CHUNK
    assert stream.reads == limits.max_bytes // _CHUNK + 1


def test_a_declared_length_over_the_ceiling_costs_no_reads_at_all() -> None:
    # The one direction Content-Length is believed: a sender who admits to
    # being oversized is taken at their word, and the socket is dropped.
    stream = ScriptedStream(b"\x00" * 4096)

    with pytest.raises(UploadRejectedError) as caught:
        read_upload(stream, declared_length=99_000_000, limits=UploadLimits())

    assert caught.value.reason is RejectionReason.TOO_LARGE
    assert stream.reads == 0


def test_a_small_declared_length_does_not_buy_a_large_body_a_pass() -> None:
    # The named attack: declare ten bytes, send twenty megabytes. The declared
    # number is not a promise, it is not a ceiling, and nothing is sized from it.
    limits = UploadLimits(max_bytes=128 * 1024)
    stream = ScriptedStream(b"\x00" * 20_000_000, chunk_size=_CHUNK)

    with pytest.raises(UploadRejectedError) as caught:
        read_upload(stream, declared_length=10, limits=limits)

    assert caught.value.reason is RejectionReason.TOO_LARGE
    assert stream.bytes_delivered <= limits.max_bytes + _CHUNK


def test_a_declared_length_is_never_used_to_truncate_an_honest_body() -> None:
    # The mirror image, and the reason the number is not trusted in either
    # direction: a proxy that rewrote Content-Length must not silently cost a
    # visitor the tail of their photograph.
    payload = solid_image((64, 48))
    stream = ScriptedStream(payload, chunk_size=16)

    assert read_upload(stream, declared_length=10) == payload


@pytest.mark.parametrize("raw", [None, "", "not-a-number", "-1", "1e6", "12 34", "0x40"])
def test_an_unusable_content_length_is_treated_as_no_claim(raw: str | None) -> None:
    # The header is attacker-controlled, so it arrives malformed on purpose as
    # well as by accident. Either way it means "no claim", never an error.
    assert content_length(raw) is None


def test_a_usable_content_length_survives_as_a_number() -> None:
    assert content_length("2048") == 2048
    assert content_length(2048) == 2048
    assert content_length(0) == 0


# --- slow uploads ----------------------------------------------------------
#
# Invisible to every size ceiling: eight mebibytes at one byte a second is under
# an eight mebibyte limit for ninety-seven days.


def test_a_body_that_never_ends_is_cut_off_at_the_deadline() -> None:
    clock = ManualMonotonic()
    stream = TricklingStream(clock, chunk=b"\x00" * 8, seconds_per_read=1.0)

    with pytest.raises(UploadRejectedError) as caught:
        read_upload(stream, limits=UploadLimits(max_seconds=5.0), monotonic=clock)

    assert caught.value.reason is RejectionReason.TOO_SLOW
    # Six reads: five inside the deadline, and the sixth is where the check
    # fires. The stream would have gone on forever.
    assert stream.reads == 6


def test_an_upload_that_finishes_late_is_still_refused() -> None:
    # A trickle that happens to end is still a worker held for too long, so the
    # deadline is checked again at the end rather than only between reads.
    clock = ManualMonotonic()
    stream = ScriptedStream(
        solid_image(), chunk_size=8, seconds_per_read=4.0, clock=clock
    )

    with pytest.raises(UploadRejectedError) as caught:
        read_upload(stream, limits=UploadLimits(max_seconds=30.0), monotonic=clock)

    assert caught.value.reason is RejectionReason.TOO_SLOW


def test_an_ordinary_upload_is_nowhere_near_the_deadline() -> None:
    # The other half of the claim: thirty seconds refuses the attack and not the
    # visitor on a bad connection.
    clock = ManualMonotonic()
    payload = solid_image((640, 480))
    stream = ScriptedStream(payload, chunk_size=1024, seconds_per_read=0.01, clock=clock)

    assert read_upload(stream, monotonic=clock) == payload
    assert clock() < UploadLimits().max_seconds


def test_the_async_reader_enforces_the_same_two_ceilings() -> None:
    # The route will be async, so the ceilings have to be there on the path the
    # route actually takes -- not only on the one the tests find convenient.
    limits = UploadLimits(max_bytes=64 * 1024, max_seconds=5.0)

    async def too_big() -> bytes:
        stream = AsyncScriptedStream(ScriptedStream(b"\x00" * 5_000_000))
        return await read_upload_async(stream, limits=limits)

    async def honest() -> bytes:
        return await read_upload_async(
            AsyncScriptedStream(ScriptedStream(solid_image())), limits=limits
        )

    with pytest.raises(UploadRejectedError) as caught:
        asyncio.run(too_big())
    assert caught.value.reason is RejectionReason.TOO_LARGE
    assert asyncio.run(honest()) == solid_image()


def test_the_stream_entry_point_enforces_the_intakes_own_ceilings(
    store: InMemoryBlobStore,
) -> None:
    # The structural half: a route that calls `accept_stream` cannot forget to
    # pass the limits, because it does not pass them.
    intake = PhotoIntake(
        store=store,
        moderator=ImageModerator(StubImageAnalyzer()),
        limits=UploadLimits(max_bytes=512),
    )
    stream = AsyncScriptedStream(ScriptedStream(solid_image((400, 400))))

    with pytest.raises(UploadRejectedError) as caught:
        asyncio.run(intake.accept_stream(stream))

    assert caught.value.reason is RejectionReason.TOO_LARGE
    assert len(store) == 0


def test_the_stream_entry_point_accepts_an_ordinary_photograph(
    store: InMemoryBlobStore, intake: PhotoIntake
) -> None:
    stream = AsyncScriptedStream(ScriptedStream(solid_image((320, 240)), chunk_size=64))

    photo = asyncio.run(intake.accept_stream(stream, declared_media_type="image/jpeg"))

    assert len(store) == 1
    assert store.get(photo.blob_ref).content_type == NORMALIZED_MEDIA_TYPE
    assert (photo.width, photo.height) == (320, 240)
    # Read in sixty-four byte pieces and reassembled without loss: the bounded
    # read must not be a truncating one.
    assert stream.reads > 1


# --- non-images and disguised payloads -------------------------------------


def test_nothing_that_is_not_a_photograph_reaches_the_store(
    intake: PhotoIntake, store: InMemoryBlobStore, disguised_payload: bytes
) -> None:
    # Archives, executables, scripts, HTML, SVG and PDF, each of them claiming
    # to be a JPEG. Asserted on the container, because "refused" is a claim
    # about what was written and only the container can settle it.
    with pytest.raises(UploadRejectedError):
        intake.accept(disguised_payload, declared_media_type="image/jpeg")
    assert len(store) == 0


def test_a_file_that_decodes_as_another_format_is_refused_without_saying_so(
    intake: PhotoIntake, store: InMemoryBlobStore
) -> None:
    # The signature says JPEG and the decoder says otherwise. Nothing writes a
    # file like that by accident, and the refusal is the neutral line: naming
    # the mismatch would confirm both the detection and how it was reached.
    payload = jpeg_signed_but_not_jpeg()

    with pytest.raises(UploadRejectedError) as caught:
        intake.accept(payload, declared_media_type="image/jpeg")

    assert caught.value.reason is RejectionReason.DISGUISED_PAYLOAD
    assert caught.value.message == _MESSAGES[RejectionReason.UNSAFE_IMAGE]
    assert len(store) == 0


@pytest.mark.parametrize(
    "reason",
    [
        RejectionReason.DISGUISED_PAYLOAD,
        RejectionReason.UNSAFE_IMAGE,
        RejectionReason.MODERATION_UNAVAILABLE,
    ],
)
def test_every_refusal_that_looked_inside_gives_back_one_sentence(
    reason: RejectionReason,
) -> None:
    # A disguised payload, a flagged photograph and a Content Safety outage are
    # indistinguishable from outside. Distinct copy on any of the three would
    # tell an uploader which one they hit, and each of those is a fact worth
    # paying nothing for.
    assert _MESSAGES[reason] == _MESSAGES[RejectionReason.UNSAFE_IMAGE]


@pytest.mark.parametrize(
    "reason",
    [
        RejectionReason.EMPTY,
        RejectionReason.TOO_LARGE,
        RejectionReason.NOT_AN_IMAGE,
        RejectionReason.UNSUPPORTED_FORMAT,
        RejectionReason.TOO_MANY_PIXELS,
        RejectionReason.CORRUPT,
        RejectionReason.TOO_SLOW,
    ],
)
def test_a_refusal_that_only_measured_the_file_stays_helpful(
    reason: RejectionReason,
) -> None:
    # The other side of the same rule, pinned so that a later pass at "make the
    # abuse responses neutral" cannot quietly take the useful copy with it.
    # None of these says anything about what is *in* the file, and "something
    # went wrong" in front of a size limit makes a visitor try four times.
    assert _MESSAGES[reason] != _MESSAGES[RejectionReason.UNSAFE_IMAGE]


def test_no_refusal_interpolates_anything_that_came_from_the_upload() -> None:
    # The reflection bug this endpoint is one careless f-string away from. The
    # only value any message quotes is a server-side constant.
    hostile = "<script>alert(1)</script>/../../etc/passwd\x00"
    limits = UploadLimits(max_bytes=2048)
    payloads: list[bytes] = [
        b"",
        b"\x00" * 4096,
        ZIP_ARCHIVE,
        b"\xff\xd8\xff" + ZIP_ARCHIVE,
        png_declaring(9000, 9000),
        jpeg_signed_but_not_jpeg(),
    ]
    for payload in payloads:
        with pytest.raises(UploadRejectedError) as caught:
            validate(payload, declared_media_type=hostile, limits=limits)
        message = caught.value.message
        assert "script" not in message
        assert "passwd" not in message
        assert "\x00" not in message
        # Nor the sniffed type, nor a byte excerpt, nor the format the decoder
        # settled on -- all three would describe the file back to whoever sent it.
        assert "jpeg" not in message.lower()
        assert "MPO" not in message


def test_a_polyglot_loses_its_payload_in_the_re_encode(
    intake: PhotoIntake, store: InMemoryBlobStore
) -> None:
    # Accepted, and correctly so: it is a real JPEG with real dimensions, and
    # refusing every photograph with a trailing byte would refuse a lot of real
    # photographs. What handles it is stage 2 copying pixels rather than bytes.
    payload = jpeg_with_appended_archive()
    assert ZIP_ARCHIVE in payload

    photo = intake.accept(payload, declared_media_type="image/jpeg")

    assert ZIP_ARCHIVE not in store.get(photo.blob_ref).data
    assert b"PK\x03\x04" not in store.get(photo.blob_ref).data


def test_an_svg_is_refused_even_though_browsers_call_it_an_image(
    intake: PhotoIntake,
) -> None:
    # Kept as its own test rather than folded into the parametrised sweep: SVG
    # is the one entry in that list that is genuinely an image format, can carry
    # script, and would be let in by any check that asks "does this parse as an
    # image" instead of "does this start with a signature we allow".
    from chip_chat.vision.testing import SVG_WITH_SCRIPT

    assert _reason(SVG_WITH_SCRIPT, declared_media_type="image/svg+xml") is (
        RejectionReason.NOT_AN_IMAGE
    )


# --- decompression bombs and malformed decoders ----------------------------


def test_a_pixel_bomb_never_reaches_a_decoder(
    intake: PhotoIntake, store: InMemoryBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "Does not take the container down" made precise: the pixels are never
    # allocated at all. The header gate reads the declared size and refuses, so
    # nothing ever asks Pillow to load a row.
    bomb = png_declaring(30_000, 30_000)
    assert len(bomb) < 1024  # nine hundred megapixels, in under a kilobyte

    # Patched after the fixture is built, since building a PNG decodes one too.
    loads: list[tuple[int, int]] = []
    original = ImageFile.ImageFile.load

    def spy(self: ImageFile.ImageFile) -> object:
        loads.append(self.size)
        return original(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy)

    with pytest.raises(UploadRejectedError) as caught:
        intake.accept(bomb, declared_media_type="image/png")

    assert caught.value.reason is RejectionReason.TOO_MANY_PIXELS
    assert loads == []
    assert len(store) == 0


def test_the_decoder_is_asked_for_the_size_we_are_going_to_keep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half of the bomb defence, and the one that applies to images
    # *under* the pixel ceiling: fifty megapixels is a hundred and fifty
    # megabytes of RGB, and none of it survives the downscale. `draft` makes
    # libjpeg do the reduction inside the DCT instead.
    payload = solid_image((4032, 3024))

    decoded: list[tuple[int, int]] = []
    original = ImageFile.ImageFile.load

    def spy(self: ImageFile.ImageFile) -> object:
        decoded.append(self.size)
        return original(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy)

    normalize(validate(payload), limits=UploadLimits(max_edge=512))

    # Asserted on what the decoder was asked to produce rather than on whether
    # `draft` was called, because which class defines `draft` is Pillow's
    # business and the size of the buffer is ours. A quarter of each edge is a
    # sixteenth of the memory.
    assert decoded[0] == (1008, 756)


def test_a_large_photograph_still_comes_out_at_the_working_resolution() -> None:
    # The behavioural half: drafting must not change the answer, only the cost.
    photo = normalize(validate(solid_image((4032, 3024))))
    assert (photo.width, photo.height) == (1024, 768)


def test_a_header_that_lies_about_its_body_dies_without_a_500(
    intake: PhotoIntake, store: InMemoryBlobStore
) -> None:
    # Well-formed for exactly as long as a header parser looks at it. Stage 1
    # passes it and stage 2 is the first thing to read the body, so the refusal
    # arrives from there -- as a refusal, not as an exception out of a decoder.
    payload = bytearray(solid_image((320, 240)))
    del payload[len(payload) // 2 :]

    with pytest.raises(UploadRejectedError) as caught:
        intake.accept(bytes(payload))

    assert caught.value.reason is RejectionReason.CORRUPT
    assert len(store) == 0


def test_pillows_own_bomb_ceiling_is_an_error_rather_than_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Between its limit and twice it, Pillow only warns -- and a warning nobody
    # listens to is a decode that happens. Our ceiling normally refuses long
    # before, so this drops ours below Pillow's to reach the band at all.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 4096)
    limits = UploadLimits(max_pixels=10_000_000)

    assert _reason(png_declaring(128, 128), limits=limits) is (
        RejectionReason.TOO_MANY_PIXELS
    )


def test_every_reason_has_copy_and_every_message_is_reachable() -> None:
    # A reason added without a message is a KeyError in front of a visitor, on
    # the one path where an unhandled exception is least affordable.
    assert set(_MESSAGES) == set(RejectionReason)
