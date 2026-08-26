"""Stage 4: what the model may say, what it may not, and where its words go.

Four acceptance criteria on issue #53, and each has a section here:

* a schema-violating response is rejected rather than coerced
* the enums regenerate when the catalogue changes
* ``notes`` is provably unreachable from the matcher
* confidences are meaningfully distributed rather than pinned at 1.0

The third is the one worth reading. "Nothing downstream parses ``notes``" is a
rule, and rules are obeyed until somebody is in a hurry. What is asserted here is
the arrangement that makes the rule unnecessary: the object stage 5 receives does
not contain the text, anywhere, at any depth.
"""

import dataclasses
import json
from collections.abc import Iterator

import pytest

from chip_chat.otel.testing import SpanRecorder
from chip_chat.vision.describe import (
    DESCRIBE_UNAVAILABLE_MESSAGE,
    SYSTEM_PROMPT,
    ConfidenceProfile,
    DescribedMeal,
    DescribeError,
    DescribeUnavailableError,
    Description,
    DescriptionRejectedError,
    MealDescriber,
    SlotValue,
    confidence_profile,
)
from chip_chat.vision.normalize import NORMALIZED_MEDIA_TYPE
from chip_chat.vision.store import PHOTO_REF_ARGUMENT, BlobRef
from chip_chat.vision.testing import (
    DESCRIBED_MEAL,
    InMemoryBlobStore,
    StubVisionModel,
    generated_vocabulary,
    photo_tool_call,
    solid_image,
)

REF = BlobRef(container="uploads", name="2026-08-26/photo.jpg")


@pytest.fixture
def images() -> InMemoryBlobStore:
    """A store holding one screened photograph, as stage 3 leaves it."""
    store = InMemoryBlobStore()
    store.put(REF.name, solid_image(), content_type=NORMALIZED_MEDIA_TYPE)
    return store


@pytest.fixture
def model() -> StubVisionModel:
    return StubVisionModel()


@pytest.fixture
def describer(images: InMemoryBlobStore, model: StubVisionModel) -> MealDescriber:
    return MealDescriber(model, images=images, vocabulary=generated_vocabulary())


@pytest.fixture
def described(describer: MealDescriber) -> Iterator[Description]:
    """One description, produced inside the spans the schema requires."""
    with photo_tool_call(REF):
        yield describer.describe(REF)


def _answering(payload: object) -> StubVisionModel:
    return StubVisionModel(response=json.dumps(payload))


def _describer(model: StubVisionModel, store: InMemoryBlobStore) -> MealDescriber:
    return MealDescriber(model, images=store, vocabulary=generated_vocabulary())


# --- the happy path, and what it is allowed to contain -----------------------


def test_a_described_meal_is_slots_and_confidences(described: Description) -> None:
    meal = described.meal
    assert meal.is_chipotle_style is True
    assert meal.meals_visible == 1
    assert meal.vessel == SlotValue(value="bowl", confidence=0.94)
    assert meal.protein == SlotValue(value="chicken", confidence=0.71)
    assert meal.toppings == (
        SlotValue(value="cheese", confidence=0.83),
        SlotValue(value="guacamole", confidence=0.29),
    )


def test_an_unfilled_slot_is_absent_rather_than_guessed(
    images: InMemoryBlobStore,
) -> None:
    """A slot the model could not see becomes a question, not the popular answer."""
    model = _answering({"is_chipotle_style": True, "meals_visible": 1})
    with photo_tool_call(REF):
        meal = _describer(model, images).describe(REF).meal

    assert meal.protein is None
    assert meal.salsas == ()
    assert meal.slots() == ()


def test_the_response_format_reaches_the_api_rather_than_the_prompt(
    described: Description, model: StubVisionModel
) -> None:
    """Structural enforcement, not instructions the model may decline to follow."""
    sent = model.calls[0]["response_format"]
    assert sent["json_schema"]["strict"] is True
    assert sent["json_schema"]["schema"]["properties"]["protein"]["properties"]["value"][
        "enum"
    ] == ["chicken", "steak"]


def test_the_model_is_sent_the_stored_bytes(
    described: Description, model: StubVisionModel, images: InMemoryBlobStore
) -> None:
    """The screened image is the described image.

    Stage 3 moderates the normalized bytes and stage 4 reads those same bytes
    back, so there is no re-encode in between for something to slip through.
    """
    assert model.calls[0]["image"] == images.get(REF).data
    assert model.calls[0]["media_type"] == NORMALIZED_MEDIA_TYPE


def test_the_meal_count_is_available_without_reading_prose(
    images: InMemoryBlobStore,
) -> None:
    """V0 gates on this integer -- see ``docs/decisions/multi-meal-photos.md``."""
    model = _answering({**DESCRIBED_MEAL, "meals_visible": 4})
    with photo_tool_call(REF):
        meal = _describer(model, images).describe(REF).meal

    assert meal.meals_visible == 4
    assert meal.several_meals is True


def test_one_meal_with_a_side_is_one_meal(described: Description) -> None:
    assert described.meal.several_meals is False


# --- AC1: a schema-violating response is rejected rather than coerced ---------


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            json.dumps(
                {**DESCRIBED_MEAL, "protein": {"value": "wagyu", "confidence": 1.0}}
            ),
            id="a-protein-the-catalogue-does-not-publish",
        ),
        pytest.param(
            json.dumps({**DESCRIBED_MEAL, "item_id": "CMG-101"}),
            id="a-sku-smuggled-in-under-a-new-key",
        ),
        pytest.param(
            json.dumps({**DESCRIBED_MEAL, "meals_visible": "a few"}),
            id="a-count-that-is-not-a-number",
        ),
        pytest.param(
            json.dumps({"notes": "It's a chicken bowl."}),
            id="prose-and-nothing-else",
        ),
        pytest.param("Looks like a chicken bowl to me!", id="not-json-at-all"),
        pytest.param("", id="empty"),
    ],
)
def test_a_schema_violating_response_is_refused(
    response: str, images: InMemoryBlobStore
) -> None:
    describer = _describer(StubVisionModel(response=response), images)
    with photo_tool_call(REF), pytest.raises(DescriptionRejectedError):
        describer.describe(REF)


def test_a_refusal_is_not_a_partial_description(images: InMemoryBlobStore) -> None:
    """Nothing is salvaged from a violating response.

    The tempting alternative -- keep the slots that validated, drop the one that
    did not -- produces a description of a photograph nobody looked at, composed
    of real catalogue terms. That is exactly the failure shape
    ``docs/decisions/multi-meal-photos.md`` rejects one layer up.
    """
    model = _answering(
        {**DESCRIBED_MEAL, "vessel": {"value": "trough", "confidence": 0.9}}
    )
    describer = _describer(model, images)

    with photo_tool_call(REF), pytest.raises(DescriptionRejectedError) as refused:
        describer.describe(REF)
    assert refused.value.violation.path == "vessel.value"


def test_a_refused_response_still_declines_gracefully(images: InMemoryBlobStore) -> None:
    """RFC-001 section 10: the lane fails, the conversation does not.

    So the exception a caller catches carries the line to show the visitor, and
    that line asks for the thing the visitor can still do.
    """
    describer = _describer(StubVisionModel(response="{"), images)
    with photo_tool_call(REF), pytest.raises(DescriptionRejectedError) as refused:
        describer.describe(REF)
    assert refused.value.message == DESCRIBE_UNAVAILABLE_MESSAGE


# --- the lane is allowed to fail ---------------------------------------------


def test_the_deployment_being_down_declines_rather_than_guesses(
    images: InMemoryBlobStore,
) -> None:
    model = StubVisionModel(error=DescribeUnavailableError("deployment is unreachable"))
    with photo_tool_call(REF), pytest.raises(DescribeUnavailableError):
        _describer(model, images).describe(REF)


def test_an_unreadable_blob_declines(model: StubVisionModel) -> None:
    empty = InMemoryBlobStore()
    describer = MealDescriber(model, images=empty, vocabulary=generated_vocabulary())

    with photo_tool_call(REF), pytest.raises(DescribeUnavailableError):
        describer.describe(REF)
    assert model.calls == []


def test_a_ref_from_another_container_never_reaches_the_model(
    images: InMemoryBlobStore, model: StubVisionModel
) -> None:
    """A ref is the one part of this lane that can arrive from outside."""
    describer = _describer(model, images)
    elsewhere = BlobRef(container="config", name="host.json")

    with photo_tool_call(elsewhere), pytest.raises(DescribeUnavailableError):
        describer.describe(elsewhere)
    assert model.calls == []


def test_both_failures_say_the_same_thing_to_the_visitor(
    images: InMemoryBlobStore,
) -> None:
    outage = StubVisionModel(error=DescribeUnavailableError("down"))
    nonsense = StubVisionModel(response="{}")

    messages = set()
    for model in (outage, nonsense):
        with photo_tool_call(REF), pytest.raises(DescribeError) as raised:
            _describer(model, images).describe(REF)
        messages.add(raised.value.message)

    # One sentence for the visitor; two types for the trace.
    assert len(messages) == 1


# --- AC2: the enums regenerate when the catalogue changes --------------------


def test_the_describer_holds_no_vocabulary_of_its_own(
    images: InMemoryBlobStore,
) -> None:
    """The same response, two catalogue builds, two different verdicts."""
    response = json.dumps(
        {
            "is_chipotle_style": True,
            "meals_visible": 1,
            "protein": {"value": "barbacoa", "confidence": 0.8},
        }
    )
    published = MealDescriber(
        StubVisionModel(response=response),
        images=images,
        vocabulary=generated_vocabulary({"protein": ("barbacoa",)}),
    )
    withdrawn = MealDescriber(
        StubVisionModel(response=response),
        images=images,
        vocabulary=generated_vocabulary({"protein": ("chicken",)}),
    )

    with photo_tool_call(REF):
        assert published.describe(REF).meal.protein == SlotValue("barbacoa", 0.8)
    with photo_tool_call(REF), pytest.raises(DescriptionRejectedError):
        withdrawn.describe(REF)


def test_the_prompt_names_no_catalogue_term() -> None:
    """The one file a hand-maintained vocabulary would end up in.

    RFC-001 section 07 illustrates the vessel slot as ``bowl|burrito|tacos|
    salad|quesadilla``. Copying that into the prompt would be a list nobody
    would think to regenerate, and it would rot the first time the menu changed.
    The permitted values reach the model through the generated schema only.
    """
    vocabulary = generated_vocabulary()
    prompt = SYSTEM_PROMPT.lower()
    for slot in vocabulary.slots:
        for term in vocabulary.values(slot):
            for word in term.split("_"):
                assert word not in prompt, f"{term!r} is hand-written into the prompt"


def test_the_prompt_carries_the_multi_meal_decisions_two_requirements() -> None:
    """``docs/decisions/multi-meal-photos.md`` puts both of these on issue #53."""
    prompt = SYSTEM_PROMPT.lower()
    assert "meal-sized compositions" in prompt
    assert "rank" in prompt


def test_the_prompt_asks_for_a_calibrated_confidence() -> None:
    assert "1.0 on every" in SYSTEM_PROMPT


# --- AC3: notes is provably unreachable from the matcher ---------------------


def _reachable(value: object, seen: set[int] | None = None) -> Iterator[object]:
    """Yield every value reachable from ``value``, following the object graph."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    yield value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _reachable(getattr(value, field.name), seen)
        return
    if isinstance(value, str | bytes):
        return
    if isinstance(value, dict):
        for key, member in value.items():
            yield from _reachable(key, seen)
            yield from _reachable(member, seen)
        return
    if isinstance(value, list | tuple | set | frozenset):
        for member in value:
            yield from _reachable(member, seen)
        return
    for name in getattr(value, "__slots__", ()) or ():
        yield from _reachable(getattr(value, name, None), seen)


def test_the_notes_text_is_nowhere_in_the_object_the_matcher_receives(
    described: Description,
) -> None:
    """Issue #53's third acceptance criterion, asserted structurally.

    Not "the matcher does not read notes" -- that is a promise about future
    code. This walks everything reachable from the object stage 5 is handed and
    asserts the sentence is not in it, at any depth, under any name.
    """
    notes = described.notes
    assert notes, "the fixture must actually have notes to lose"

    for value in _reachable(described.meal):
        assert value != notes
        if isinstance(value, str):
            assert notes not in value


def test_the_matchers_object_has_no_field_notes_could_hide_in() -> None:
    names = {field.name for field in dataclasses.fields(DescribedMeal)}
    assert "notes" not in names
    # No raw payload, no back-reference, nothing that would carry the whole
    # response along for the ride.
    assert names == {
        "is_chipotle_style",
        "meals_visible",
        "vessel",
        "protein",
        "rice",
        "beans",
        "salsas",
        "toppings",
    }


def test_a_described_meal_cannot_be_given_notes_afterwards(
    described: Description,
) -> None:
    """Frozen and slotted, so there is no room to bolt the field back on.

    The exception type is CPython's business -- a slotted class raises on the
    missing slot, a frozen one on the assignment -- so what is asserted is that
    the assignment does not succeed and the attribute still does not exist.
    """
    assert not hasattr(described.meal, "notes")
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        described.meal.notes = "a menu item name"  # type: ignore[attr-defined]
    assert not hasattr(described.meal, "notes")


def test_notes_survives_for_the_one_reader_it_has(described: Description) -> None:
    """Display-only is not the same as discarded: the visitor is shown it."""
    assert described.notes == DESCRIBED_MEAL["notes"]


def test_a_response_with_no_notes_describes_a_meal_just_the_same(
    images: InMemoryBlobStore,
) -> None:
    model = _answering({"is_chipotle_style": True, "meals_visible": 1})
    with photo_tool_call(REF):
        description = _describer(model, images).describe(REF)
    assert description.notes == ""


def test_a_product_name_in_notes_reaches_nothing_that_could_act_on_it(
    images: InMemoryBlobStore,
) -> None:
    """The failure this arrangement exists for.

    A model that ignores the prompt and writes a SKU into the one free-text
    field has produced a fabricated product identifier. It goes to the visitor's
    screen and stops there, because there is no path from it to the matcher.
    """
    fabricated = "Order CMG-9999, the Double Wagyu Bowl"
    model = _answering({**DESCRIBED_MEAL, "notes": fabricated})
    with photo_tool_call(REF):
        description = _describer(model, images).describe(REF)

    assert description.notes == fabricated
    for value in _reachable(description.meal):
        if isinstance(value, str):
            assert "CMG-9999" not in value


# --- AC4: confidences are meaningfully distributed ---------------------------


def test_a_describer_that_pins_every_slot_at_one_is_detected() -> None:
    """What issue #53's fourth criterion is checking for, in miniature.

    The labeled photo set is issue #56 and does not exist yet, so what ships
    here is the check itself plus the proof that it catches the shape it is
    looking for. #56 feeds it real photographs.
    """
    pinned = DescribedMeal(
        is_chipotle_style=True,
        meals_visible=1,
        vessel=SlotValue("bowl", 1.0),
        protein=SlotValue("chicken", 1.0),
        rice=SlotValue("white_rice", 1.0),
        toppings=(SlotValue("cheese", 1.0),),
    )
    profile = confidence_profile([pinned])

    assert profile.slots == 4
    assert profile.pinned_fraction == 1.0
    assert profile.distinct == 1
    assert profile.spread == 0.0
    assert not profile.is_meaningfully_distributed()


def test_a_calibrated_run_passes_the_same_check(described: Description) -> None:
    profile = confidence_profile([described.meal])

    assert profile.slots == 7
    assert profile.pinned == 0
    assert profile.distinct == 7
    assert profile.spread > 0.0
    assert profile.is_meaningfully_distributed()


def test_a_run_that_is_certain_about_half_its_slots_is_still_credible() -> None:
    """Some photographs really are unambiguous; the check is not a hair trigger."""
    profile = ConfidenceProfile(confidences=(1.0, 1.0, 0.8, 0.4))
    assert profile.pinned_fraction == 0.5
    assert profile.is_meaningfully_distributed()


def test_a_run_with_no_slots_at_all_is_not_reported_as_well_distributed() -> None:
    assert not ConfidenceProfile(confidences=()).is_meaningfully_distributed()
    assert ConfidenceProfile(confidences=()).pinned_fraction == 0.0


def test_the_profile_gathers_every_slot_across_every_photograph(
    described: Description,
) -> None:
    profile = confidence_profile([described.meal, described.meal])
    assert profile.slots == 14


# --- the span --------------------------------------------------------------


def test_describe_emits_the_span_rfc_001_names(
    described: Description, spans: SpanRecorder
) -> None:
    attributes = spans.attributes_of("vision.describe")
    assert attributes["chip_chat.vision.image_ref"] == str(REF)
    assert attributes["chip_chat.vision.meals_visible"] == 1
    assert attributes["llm.model_name"] == "gpt-4.1-mini-test"


def test_the_span_carries_the_reference_and_never_the_image(
    described: Description, spans: SpanRecorder, images: InMemoryBlobStore
) -> None:
    """A trace is not an image store -- RFC-001 section 07 on the tool boundary."""
    import base64

    encoded = base64.b64encode(images.get(REF).data).decode("ascii")
    for span in spans.finished_spans():
        for value in (span.attributes or {}).values():
            if isinstance(value, str):
                assert encoded not in value


def test_the_structured_output_is_on_the_span_verbatim(
    described: Description, spans: SpanRecorder
) -> None:
    """Including notes. A trace is where an operator reads what the model said,
    which is a different thing from a downstream parser."""
    recorded = json.loads(str(spans.attributes_of("vision.describe")["output.value"]))
    assert recorded == DESCRIBED_MEAL


def test_the_span_records_which_catalogue_build_constrained_the_model(
    described: Description, spans: SpanRecorder
) -> None:
    metadata = json.loads(str(spans.attributes_of("vision.describe")["metadata"]))
    assert metadata["catalogue_content_version"] == "0" * 64


def test_a_declining_lane_is_marked_failed_rather_than_silent(
    images: InMemoryBlobStore, spans: SpanRecorder
) -> None:
    model = StubVisionModel(error=DescribeUnavailableError("deployment is unreachable"))
    with photo_tool_call(REF), pytest.raises(DescribeUnavailableError):
        _describer(model, images).describe(REF)

    span = spans.span_named("vision.describe")
    assert span.status.is_ok is False


def test_describe_refuses_to_run_outside_the_tool_span_it_belongs_under(
    describer: MealDescriber,
) -> None:
    """RFC-001 section 09 puts ``vision.describe`` under ``tool.<tool_name>``,
    and :mod:`chip_chat.otel` enforces the tree rather than documenting it."""
    from chip_chat.otel.spans import SpanSchemaError

    with pytest.raises(SpanSchemaError):
        describer.describe(REF)


def test_describe_as_tool_opens_the_spans_above_it(
    describer: MealDescriber, spans: SpanRecorder
) -> None:
    """For the callers that are not the agent -- a batch over the labeled set."""
    describer.describe_as_tool(REF)

    tree = spans.tree_text()
    assert "agent.step" in tree
    assert "tool.match_meal_from_photo" in tree
    assert "vision.describe" in tree


def test_the_tool_span_records_the_ref_and_only_the_ref(
    describer: MealDescriber, spans: SpanRecorder
) -> None:
    describer.describe_as_tool(REF)
    arguments = json.loads(
        str(spans.attributes_of("tool.match_meal_from_photo")["input.value"])
    )
    assert arguments == {PHOTO_REF_ARGUMENT: str(REF)}
