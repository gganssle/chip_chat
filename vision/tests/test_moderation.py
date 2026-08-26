"""Stage 3, and mostly the ordering rather than the verdict.

The verdict is the easy half: Content Safety says a number, a threshold compares
it, a photograph is refused. What these tests are actually for is the half that
is easy to get wrong and impossible to notice -- **that moderation happens
before anything else touches the image**, and that it still refuses when the
moderation service is the thing that broke.

So the assertions here are deliberately not about the return value. They are
about the store (nothing unmoderated was written), about the recorded spans
(``guard.content_safety`` happened, ``vision.describe`` did not), and about the
order the two occurred in. A reordering that left every happy path working would
fail three of them.
"""

from collections.abc import Mapping

import pytest

from chip_chat.otel import ToolName, agent_step, tool_call, vision_describe
from chip_chat.otel.attributes import ChipChatAttributes, GuardOutcome
from chip_chat.otel.testing import SpanRecorder
from chip_chat.vision import (
    ImageModerator,
    ModerationThresholds,
    ModerationUnavailableError,
    PhotoIntake,
    RejectionReason,
    SafetyCategory,
    StoredPhoto,
    UploadRejectedError,
)
from chip_chat.vision.moderation import (
    ENDPOINT_VARIABLE,
    SERVICE_MAX_EDGE,
    SERVICE_MIN_EDGE,
    AzureImageAnalyzer,
    _fit_for_service,
)
from chip_chat.vision.normalize import normalize
from chip_chat.vision.testing import (
    InMemoryBlobStore,
    StubImageAnalyzer,
    photo_with_location,
    safe_severities,
    solid_image,
)
from chip_chat.vision.validate import rejection, validate

CONTENT_SAFETY_SPAN = "guard.content_safety"
VISION_SPAN = "vision.describe"


def severities(**overrides: int) -> dict[str, int]:
    """A full safe answer with some categories raised.

    Args:
        **overrides: Category attribute names from :class:`SafetyCategory`
            (``SEXUAL``, ``VIOLENCE``, ...) and the severity to report.

    Returns:
        A severity per category, as Content Safety returns one.
    """
    answer = safe_severities()
    for name, severity in overrides.items():
        answer[SafetyCategory[name].value] = severity
    return answer


@pytest.fixture
def store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


def intake_with(
    store: InMemoryBlobStore, analyzer: object, **kwargs: object
) -> PhotoIntake:
    """Build an intake whose stage 3 answers from ``analyzer``."""
    thresholds = kwargs.pop("thresholds", None)
    moderator = ImageModerator(
        analyzer=analyzer,  # type: ignore[arg-type]
        thresholds=thresholds,  # type: ignore[arg-type]
    )
    return PhotoIntake(store=store, moderator=moderator, **kwargs)  # type: ignore[arg-type]


# --- the ordering, which is the requirement --------------------------------


def describe(photo: StoredPhoto) -> None:
    """Emit the span tree stage 4 will emit, over a photo stage 3 allowed.

    The describe stage does not exist yet (issue #53), so this stands in for it.
    What the tests below assert is which spans happened and in what order, and
    that assertion survives whatever the real stage 4 turns out to look like.
    """
    with (
        agent_step(index=0),
        tool_call(ToolName.MATCH_MEAL_FROM_PHOTO),
        vision_describe(image_ref=str(photo.blob_ref), model="gpt-4o"),
    ):
        pass


class StoreWatchingAnalyzer:
    """An analyzer that records what the container held when it was called.

    This is the reordering detector. Stage 3 is supposed to run *before* the
    write, so the only correct answer to "how many blobs existed when Content
    Safety was asked" is zero. Moving ``put`` above ``screen`` in
    :meth:`~chip_chat.vision.intake.PhotoIntake.accept` leaves every happy-path
    assertion in this file passing and fails this one.
    """

    def __init__(self, store: InMemoryBlobStore) -> None:
        self._store = store
        self.blobs_at_call_time: list[int] = []

    def analyze(self, image: bytes) -> Mapping[str, int]:
        self.blobs_at_call_time.append(len(self._store))
        return safe_severities()


def test_nothing_is_written_before_content_safety_has_answered(
    store: InMemoryBlobStore,
) -> None:
    analyzer = StoreWatchingAnalyzer(store)
    intake_with(store, analyzer).accept(solid_image())
    assert analyzer.blobs_at_call_time == [0]
    assert len(store) == 1


def test_what_is_screened_is_the_normalized_copy(store: InMemoryBlobStore) -> None:
    # Stage 3 sits behind stage 2, so what Content Safety sees is what would be
    # kept -- not the upload, which still had its location data attached.
    analyzer = StubImageAnalyzer()
    uploaded = photo_with_location((4032, 3024))
    photo = intake_with(store, analyzer).accept(uploaded)

    assert analyzer.calls == [store.get(photo.blob_ref).data]
    assert analyzer.calls[0] != uploaded


def test_screening_precedes_the_vision_model_in_the_trace(
    store: InMemoryBlobStore, spans: SpanRecorder
) -> None:
    describe(intake_with(store, StubImageAnalyzer()).accept(solid_image()))

    names = spans.names()
    assert names.index(CONTENT_SAFETY_SPAN) < names.index(VISION_SPAN)


def test_a_flagged_image_never_reaches_the_vision_model(
    store: InMemoryBlobStore, spans: SpanRecorder
) -> None:
    # The acceptance criterion, verified where it is stated: in the trace. The
    # describe stage is inside the `try`, so a stage 3 that returned instead of
    # raising would emit `vision.describe` here and fail.
    intake = intake_with(store, StubImageAnalyzer(severities(SEXUAL=6)))
    with pytest.raises(UploadRejectedError):
        describe(intake.accept(solid_image()))

    assert CONTENT_SAFETY_SPAN in spans.names()
    assert VISION_SPAN not in spans.names()
    assert len(store) == 0


def test_an_intake_cannot_be_built_without_a_moderator(
    store: InMemoryBlobStore,
) -> None:
    # The other half of the ordering guarantee. An optional moderator would be
    # a moderator somebody forgets, and a forgotten one is an unmoderated
    # vision lane that looks exactly like a working one.
    with pytest.raises(TypeError):
        PhotoIntake(store=store)  # type: ignore[call-arg]


# --- what a refused visitor sees -------------------------------------------


def test_a_flagged_image_writes_nothing(store: InMemoryBlobStore) -> None:
    intake = intake_with(store, StubImageAnalyzer(severities(VIOLENCE=6)))
    with pytest.raises(UploadRejectedError) as refusal:
        intake.accept(solid_image())
    assert refusal.value.reason is RejectionReason.UNSAFE_IMAGE
    assert len(store) == 0


def test_the_refusal_says_the_same_thing_however_stage_three_refused() -> None:
    # An outage that announced itself would tell the previous uploader that
    # theirs, specifically, was the one that got flagged.
    flagged = rejection(RejectionReason.UNSAFE_IMAGE).message
    unavailable = rejection(RejectionReason.MODERATION_UNAVAILABLE).message
    assert flagged == unavailable


@pytest.mark.parametrize("category", list(SafetyCategory))
def test_the_refusal_names_nothing_it_detected(
    store: InMemoryBlobStore, category: SafetyCategory
) -> None:
    analyzer = StubImageAnalyzer({**safe_severities(), category.value: 6})
    with pytest.raises(UploadRejectedError) as refusal:
        intake_with(store, analyzer).accept(solid_image())

    message = refusal.value.message
    assert category.value.lower() not in message.lower()
    assert message == rejection(RejectionReason.UNSAFE_IMAGE).message
    # Nor a severity, nor a threshold, nor anything else to iterate against.
    assert not any(character.isdigit() for character in message)


# --- fail closed ------------------------------------------------------------


def test_an_outage_disables_the_vision_lane_rather_than_bypassing_the_check(
    store: InMemoryBlobStore,
) -> None:
    intake = intake_with(store, StubImageAnalyzer(unavailable=True))
    with pytest.raises(UploadRejectedError) as refusal:
        intake.accept(solid_image())
    assert refusal.value.reason is RejectionReason.MODERATION_UNAVAILABLE
    assert len(store) == 0


def test_a_partial_answer_is_not_a_pass(store: InMemoryBlobStore) -> None:
    # Three categories at zero and one absent is an answer with a hole in it.
    # Reading the hole as "safe" is how a service change becomes a silent
    # bypass, so stage 3 treats it as no answer at all.
    partial = safe_severities()
    del partial[SafetyCategory.SELF_HARM.value]
    with pytest.raises(UploadRejectedError) as refusal:
        intake_with(store, StubImageAnalyzer(partial)).accept(solid_image())
    assert refusal.value.reason is RejectionReason.MODERATION_UNAVAILABLE
    assert len(store) == 0


def test_an_unknown_category_is_screened_against_the_unknown_threshold(
    store: InMemoryBlobStore,
) -> None:
    # A category added to the service after this module was written is one
    # nobody chose a threshold for, and an unchosen threshold must not default
    # to permissive.
    invented = {**safe_severities(), "SomethingNew": 4}
    with pytest.raises(UploadRejectedError) as refusal:
        intake_with(store, StubImageAnalyzer(invented)).accept(solid_image())
    assert refusal.value.reason is RejectionReason.UNSAFE_IMAGE


# --- the thresholds ---------------------------------------------------------


def test_the_documented_defaults_are_the_defaults() -> None:
    # These numbers are an argued choice, not an accident -- see the module
    # docstring. If one changes, this test is where the argument gets revisited.
    thresholds = ModerationThresholds()
    assert (thresholds.hate, thresholds.self_harm, thresholds.sexual) == (2, 2, 2)
    assert thresholds.violence == 4
    assert thresholds.unknown == 2


@pytest.mark.parametrize(
    ("category", "severity", "refused"),
    [
        ("SEXUAL", 0, False),
        ("SEXUAL", 2, True),
        ("SELF_HARM", 2, True),
        ("HATE", 2, True),
        # The deliberate exception: knives and raw meat are ordinary food
        # photography and are exactly what this classifier's low band reports.
        ("VIOLENCE", 2, False),
        ("VIOLENCE", 4, True),
    ],
)
def test_a_category_refuses_at_its_own_threshold_and_not_below_it(
    store: InMemoryBlobStore, category: str, severity: int, refused: bool
) -> None:
    analyzer = StubImageAnalyzer(severities(**{category: severity}))
    intake = intake_with(store, analyzer)
    if refused:
        with pytest.raises(UploadRejectedError):
            intake.accept(solid_image())
        assert len(store) == 0
    else:
        intake.accept(solid_image())
        assert len(store) == 1


def test_thresholds_can_be_tightened_per_category(store: InMemoryBlobStore) -> None:
    strict = ModerationThresholds(violence=2)
    analyzer = StubImageAnalyzer(severities(VIOLENCE=2))
    with pytest.raises(UploadRejectedError):
        intake_with(store, analyzer, thresholds=strict).accept(solid_image())


@pytest.mark.parametrize("value", [0, 1, 3, 8, -2])
def test_a_threshold_that_is_not_a_severity_is_refused(value: int) -> None:
    # 0 would refuse every photograph and 8 would refuse none -- and there is
    # deliberately no value meaning "never refuse", because switching a category
    # off is a decision to make in the open rather than in an env var.
    with pytest.raises(ValueError, match="must be one of"):
        ModerationThresholds(sexual=value)


def test_thresholds_come_from_the_environment_when_it_sets_them() -> None:
    configured = ModerationThresholds.from_env(
        {"CHIP_CHAT_MODERATION_VIOLENCE_BLOCK_AT": "6"}
    )
    assert configured.violence == 6
    assert configured.sexual == ModerationThresholds().sexual


def test_an_empty_environment_leaves_the_defaults_alone() -> None:
    assert ModerationThresholds.from_env({}) == ModerationThresholds()


def test_the_moderator_reports_the_thresholds_it_enforces() -> None:
    thresholds = ModerationThresholds(violence=6)
    moderator = ImageModerator(analyzer=StubImageAnalyzer(), thresholds=thresholds)
    assert moderator.thresholds is thresholds


# --- what the span says -----------------------------------------------------


def test_an_allowed_image_records_the_severities_it_was_allowed_on(
    store: InMemoryBlobStore, spans: SpanRecorder
) -> None:
    intake_with(store, StubImageAnalyzer(severities(VIOLENCE=2))).accept(solid_image())
    attributes = spans.attributes_of(CONTENT_SAFETY_SPAN)
    assert attributes[ChipChatAttributes.GUARD_OUTCOME] == GuardOutcome.ALLOWED
    # Recorded so a threshold can be retuned against real traffic rather than
    # against an intuition about what real traffic looks like.
    assert '"Violence": 2' in str(attributes["metadata"])


def test_a_blocked_image_records_the_categories_the_visitor_was_not_told(
    store: InMemoryBlobStore, spans: SpanRecorder
) -> None:
    analyzer = StubImageAnalyzer(severities(SEXUAL=4, HATE=6))
    with pytest.raises(UploadRejectedError):
        intake_with(store, analyzer).accept(solid_image())

    span = spans.span_named(CONTENT_SAFETY_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes[ChipChatAttributes.GUARD_OUTCOME] == GuardOutcome.BLOCKED
    assert attributes[ChipChatAttributes.GUARD_REASON] == (
        RejectionReason.UNSAFE_IMAGE.value
    )
    # Not an error span. A refusal is what this guard is for, and marking one
    # failed would put a working block in the same bucket as an outage.
    assert span.status.is_ok
    # The operator can tell which categories fired. The uploader cannot.
    assert set(attributes[ChipChatAttributes.SAFETY_CATEGORIES]) == {  # type: ignore[arg-type]
        SafetyCategory.SEXUAL.value,
        SafetyCategory.HATE.value,
    }


def test_an_outage_is_a_different_span_from_a_refusal(
    store: InMemoryBlobStore, spans: SpanRecorder
) -> None:
    with pytest.raises(UploadRejectedError):
        intake_with(store, StubImageAnalyzer(unavailable=True)).accept(solid_image())

    span = spans.span_named(CONTENT_SAFETY_SPAN)
    attributes = dict(span.attributes or {})
    assert attributes[ChipChatAttributes.GUARD_REASON] == (
        RejectionReason.MODERATION_UNAVAILABLE.value
    )
    # A declining lane that looked like a success is a lane nobody fixes -- and
    # this is the one outcome here that really is a failure rather than a
    # decision, so it is the only one that marks the span.
    assert span.status.is_ok is False


def test_the_span_says_it_screened_an_image(
    store: InMemoryBlobStore, spans: SpanRecorder
) -> None:
    # Text moderation on the prompt is a separate call to the same service, and
    # the two are only distinguishable in a trace by this.
    intake_with(store, StubImageAnalyzer()).accept(solid_image())
    assert spans.attributes_of(CONTENT_SAFETY_SPAN)["input.value"] == "image"


# --- the real client --------------------------------------------------------


class FakeContentSafetyClient:
    """Stands in for ``ContentSafetyClient``, returning a scripted analysis."""

    def __init__(self, analyses: object, error: Exception | None = None) -> None:
        self._analyses = analyses
        self._error = error
        self.requests: list[object] = []

    def analyze_image(self, options: object) -> object:
        self.requests.append(options)
        if self._error is not None:
            raise self._error
        return _Result(self._analyses)


class _Result:
    def __init__(self, categories_analysis: object) -> None:
        self.categories_analysis = categories_analysis


class _Analysis:
    def __init__(self, category: str, severity: int | None) -> None:
        self.category = category
        self.severity = severity


def test_the_azure_analyzer_reads_a_severity_per_category() -> None:
    client = FakeContentSafetyClient(
        [_Analysis("Hate", 0), _Analysis("Sexual", 4), _Analysis("Violence", 2)]
    )
    analyzer = AzureImageAnalyzer(client)  # type: ignore[arg-type]
    assert analyzer.analyze(solid_image()) == {"Hate": 0, "Sexual": 4, "Violence": 2}


def test_a_category_reported_without_a_severity_is_not_a_zero() -> None:
    client = FakeContentSafetyClient([_Analysis("Hate", None)])
    with pytest.raises(ModerationUnavailableError):
        AzureImageAnalyzer(client).analyze(solid_image())  # type: ignore[arg-type]


def test_a_transport_failure_becomes_an_outage_rather_than_a_verdict() -> None:
    from azure.core.exceptions import ServiceRequestError

    client = FakeContentSafetyClient([], error=ServiceRequestError("no route to host"))
    with pytest.raises(ModerationUnavailableError):
        AzureImageAnalyzer(client).analyze(solid_image())  # type: ignore[arg-type]


def test_an_unconfigured_endpoint_fails_at_startup_rather_than_per_upload() -> None:
    # An unconfigured moderator would otherwise fail closed on every upload,
    # which looks exactly like a service outage and takes a day to tell apart.
    with pytest.raises(RuntimeError, match=ENDPOINT_VARIABLE):
        AzureImageAnalyzer.from_env({})


# --- fitting an image to what the service will look at ----------------------


def test_a_normalized_photo_is_sent_exactly_as_it_will_be_stored() -> None:
    # The default ceilings land a normalized photo inside all three of Content
    # Safety's limits, so nothing is re-encoded and the screened bytes and the
    # stored bytes are the same object.
    photo = normalize(validate(photo_with_location((4032, 3024)))).data
    assert _fit_for_service(photo) is photo


def test_an_image_below_the_service_minimum_is_still_screened() -> None:
    # 50x50 is Content Safety's floor. A photograph under it must not become a
    # photograph nothing looked at, so it is padded rather than refused --
    # padding adds white margin, where upscaling would invent pixels.
    fitted = _fit_for_service(solid_image((10, 10)))
    assert _size_of(fitted) == (SERVICE_MIN_EDGE, SERVICE_MIN_EDGE)


def test_an_image_above_the_service_maximum_is_downscaled_to_fit() -> None:
    # `max_edge` is configurable, so a deployment can produce a normalized photo
    # larger than the service accepts.
    fitted = _fit_for_service(solid_image((4000, 3000)))
    assert max(_size_of(fitted)) == SERVICE_MAX_EDGE


def test_an_extreme_aspect_ratio_ends_up_inside_both_limits() -> None:
    # Neither bound can be met by scaling alone here: shrinking the long edge to
    # 2048 takes the short edge further below 50.
    fitted = _fit_for_service(solid_image((4000, 20)))
    width, height = _size_of(fitted)
    assert max(width, height) <= SERVICE_MAX_EDGE
    assert min(width, height) >= SERVICE_MIN_EDGE


def test_something_that_is_not_an_image_is_an_outage_not_a_pass() -> None:
    with pytest.raises(ModerationUnavailableError):
        _fit_for_service(b"not a photograph")


def _size_of(data: bytes) -> tuple[int, int]:
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        return image.size
