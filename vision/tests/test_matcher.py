"""Stage 5: described slots become catalogue SKUs, or become a question.

Issue #54's four acceptance criteria, in order, and then the two gates RFC-001
section 07 puts in front of stage 5 and the drift check that keeps a
vocabulary and a catalogue from being two different builds.

The first criterion asks for a test "over the whole labeled photo set", which is
issue #56 and does not exist yet. What stands in for it here is stronger than
thirty photographs and available today: the *entire* space of answers the
stage-4 schema permits, enumerated. Every vessel crossed with every protein
crossed with every rice, bean, salsa and topping the vocabulary publishes, with
every subset of the multi-valued slots -- if any of those resolves to an
identifier the catalogue does not carry, this fails. A photograph can only ever
produce one of them, so a matcher that survives this cannot fabricate a SKU from
a photograph either. #56 feeds it real photographs and scores precision and
recall, which is a different question from this one.
"""

import itertools
import sys
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from opentelemetry.util.types import AttributeValue

from chip_chat.catalog.records import MenuCatalog, Slot
from chip_chat.otel.testing import SpanRecorder
from chip_chat.vision.describe import DescribedMeal, SlotValue
from chip_chat.vision.matcher import (
    ENV_PREFIX,
    REQUIRED_SLOTS,
    CatalogueDriftError,
    ClarificationReason,
    MealMatcher,
    Outcome,
    SlotRule,
    SlotRules,
)
from chip_chat.vision.testing import (
    COMPARISON_RESTAURANT,
    DEFAULT_TERMS,
    REFERENCE_RESTAURANT,
    generated_vocabulary,
    menu_catalog,
    photo_tool_call,
)

CATALOG_TESTS = Path(__file__).resolve().parents[2] / "catalog" / "tests"
"""Where the built-catalogue fixture lives.

``catalog/tests/catalog_fixtures.py`` builds a real catalogue by harvesting the
harvest tests' fixture site, and it is what the last two tests here resolve
against. Pytest puts a test file's own directory on ``sys.path`` and not its
siblings', so ``pytest vision/tests`` on its own needs telling -- the same
insertion, for the same reason, that ``catalog_fixtures`` itself does for
``harvest/tests``.
"""
if str(CATALOG_TESTS) not in sys.path:
    sys.path.insert(0, str(CATALOG_TESTS))

from catalog_fixtures import fixture_catalog  # noqa: E402

SURE = 1.0
"""A confidence above every default floor, for a test about something else."""

IMAGE_REF = "uploads/2026-01-01/a-photograph.jpg"
"""The reference the tool span carries. A key, and never bytes."""


@pytest.fixture
def outside_a_tool_call() -> None:
    """Requested by the tests that open the span tree themselves.

    ``matcher.resolve`` is a child of ``tool.<tool_name>`` and
    :mod:`chip_chat.otel` enforces that, so every other test in this module runs
    inside one. The two that exercise :meth:`MealMatcher.resolve_as_tool` open
    ``agent.step`` themselves, and ``agent.step`` is a child of ``chat.turn`` --
    so they have to run outside the tool call rather than inside it.
    """


@pytest.fixture(autouse=True)
def _within_a_tool_call(request: pytest.FixtureRequest) -> Iterator[None]:
    """Open ``tool.match_meal_from_photo`` around every test that wants one."""
    if "outside_a_tool_call" in request.fixturenames:
        yield
        return
    with photo_tool_call(IMAGE_REF):
        yield


@pytest.fixture
def catalog() -> MenuCatalog:
    """A catalogue publishing rows for exactly :data:`DEFAULT_TERMS`.

    ``("bowl", "steak")`` is left out, because a menu that sold every pair of
    every term could not exercise the case the whole design turns on: two real
    terms that name no real row.
    """
    return menu_catalog(without=[("bowl", "steak")])


@pytest.fixture
def matcher(catalog: MenuCatalog) -> MealMatcher:
    """A matcher on that catalogue, at the default floors."""
    return MealMatcher(catalog)


def described(
    *,
    vessel: str | None = "bowl",
    protein: str | None = "chicken",
    rice: str | None = "white_rice",
    beans: str | None = "black_beans",
    salsas: Sequence[tuple[str, float]] = (),
    toppings: Sequence[tuple[str, float]] = (),
    at: Mapping[str, float] | None = None,
    confidence: float = SURE,
    is_chipotle_style: bool = True,
    meals_visible: int = 1,
) -> DescribedMeal:
    """A stage-4 answer, filled in from what a test actually cares about.

    Args:
        vessel: The vessel term, or ``None`` for a slot the model left out.
        protein: The protein term, or ``None``.
        rice: The rice term, or ``None``.
        beans: The beans term, or ``None``.
        salsas: ``(term, confidence)`` pairs.
        toppings: ``(term, confidence)`` pairs.
        at: A confidence per named slot, e.g. ``{"protein": 0.4}``. Every slot
            it does not name gets ``confidence``.
        confidence: What to say about every single-valued slot ``at`` omits.
        is_chipotle_style: Whether the food is the kind this restaurant serves.
        meals_visible: How many orderable meals are in the frame.

    Returns:
        The described meal.
    """
    said = {} if at is None else at

    def slot(name: str, term: str | None) -> SlotValue | None:
        if term is None:
            return None
        return SlotValue(value=term, confidence=said.get(name, confidence))

    return DescribedMeal(
        is_chipotle_style=is_chipotle_style,
        meals_visible=meals_visible,
        vessel=slot("vessel", vessel),
        protein=slot("protein", protein),
        rice=slot("rice", rice),
        beans=slot("beans", beans),
        salsas=tuple(SlotValue(value=term, confidence=score) for term, score in salsas),
        toppings=tuple(
            SlotValue(value=term, confidence=score) for term, score in toppings
        ),
    )


# --- AC1: every resolved item is a real catalogue row ------------------------


def _every_describable_meal() -> Iterator[DescribedMeal]:
    """Every answer the stage-4 schema permits over :data:`DEFAULT_TERMS`.

    Confidence is pinned at 1.0 throughout, which is the point: this asks what
    the matcher does when it believes the model completely. A threshold that
    refused everything would make the assertion below pass for the wrong
    reason, so :func:`test_the_exhaustive_sweep_actually_resolves_meals` asserts
    that most of these produce a draft.
    """
    salsas = _subsets(DEFAULT_TERMS["salsas"])
    toppings = _subsets(DEFAULT_TERMS["toppings"])
    for vessel, protein, rice, beans, chosen_salsas, chosen_toppings in itertools.product(
        DEFAULT_TERMS["vessel"],
        DEFAULT_TERMS["protein"],
        DEFAULT_TERMS["rice"],
        DEFAULT_TERMS["beans"],
        salsas,
        toppings,
    ):
        yield described(
            vessel=vessel,
            protein=protein,
            rice=rice,
            beans=beans,
            salsas=[(term, SURE) for term in chosen_salsas],
            toppings=[(term, SURE) for term in chosen_toppings],
        )


def _subsets(terms: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """Every subset of ``terms``, since a multi-valued slot may hold any of them."""
    return tuple(
        combination
        for size in range(len(terms) + 1)
        for combination in itertools.combinations(terms, size)
    )


def test_no_resolved_item_is_absent_from_the_catalogue(
    matcher: MealMatcher, catalog: MenuCatalog
) -> None:
    """The guarantee this issue owns, asserted by exhaustion over model output.

    "No SKU in any response that does not exist in the catalogue." Every answer
    the schema permits is resolved, and every identifier that comes back is
    looked up in ``menu_items``. There is no sampling here and no photograph:
    the space of things a model can say is small and finite, because the
    vocabulary made it so, and finite spaces can be checked rather than
    estimated.
    """
    published = {item.item_id for item in catalog.menu_items}
    for meal in _every_describable_meal():
        for item_id in matcher.resolve(meal).item_ids():
            assert item_id in published


def test_every_resolved_modifier_is_offered_on_the_entree_it_is_on(
    matcher: MealMatcher, catalog: MenuCatalog
) -> None:
    """A real row is not enough: it has to be a real row *of this entree*.

    ``docs/action-surface.md`` section 1.3 -- a modifier's identity is
    per-parent, and resolving one to a single identifier and reusing it "will
    price a taco wrong". So the assertion is against ``modifiers``, keyed by the
    pair, rather than against the flat set of items.
    """
    offered = {modifier.modifier_id for modifier in catalog.modifiers}
    for meal in _every_describable_meal():
        resolution = matcher.resolve(meal)
        if resolution.entree is None:
            continue
        for item in resolution.modifiers:
            assert item.modifier_id == f"{resolution.entree.item_id}:{item.item_id}"
            assert item.modifier_id in offered


def test_the_exhaustive_sweep_actually_resolves_meals(matcher: MealMatcher) -> None:
    """Guards the sweep above against passing because nothing resolved.

    Three of the four vessel-and-protein pairs are on this menu, so at a
    confidence of 1.0 three quarters of the space should produce a draft. A
    matcher that refused everything would satisfy "no fabricated SKU" perfectly
    and be useless, and that is exactly the failure a test asserting only an
    absence cannot see.
    """
    outcomes = [matcher.resolve(meal).outcome for meal in _every_describable_meal()]
    resolved = outcomes.count(Outcome.RESOLVED)
    assert resolved == len(outcomes) * 3 // 4
    assert set(outcomes) == {Outcome.RESOLVED, Outcome.CLARIFY}


def test_two_real_terms_that_name_no_real_row_resolve_to_nothing(
    matcher: MealMatcher,
) -> None:
    """The case D3 exists for, and the one a nearest-match would get wrong.

    This menu sells a Chicken Bowl, a Chicken Burrito and a Steak Burrito. Both
    halves of a described steak bowl are terms the catalogue publishes and the
    pair is not an entree, so the answer is a question -- not the nearest
    entree, which would be a real SKU on an order nobody asked for.
    """
    resolution = matcher.resolve(described(vessel="bowl", protein="steak"))

    assert resolution.outcome is Outcome.CLARIFY
    assert resolution.item_ids() == ()
    assert resolution.entree is None
    reasons = {clarification.reason for clarification in resolution.clarifications}
    assert reasons == {ClarificationReason.NO_CATALOGUE_ROW}


def test_a_term_the_catalogue_does_not_publish_never_becomes_a_sku(
    matcher: MealMatcher,
) -> None:
    """Structured output makes this unreachable, which is why it is checked.

    Stage 4 rejects a response carrying a term outside the enum, so nothing
    should reach the matcher with one. If something ever does -- a hand-built
    description, a future caller, a schema that drifted -- the answer is still
    a question and never the nearest publishable thing.
    """
    resolution = matcher.resolve(described(protein="carnitas"))

    assert resolution.outcome is Outcome.CLARIFY
    assert resolution.item_ids() == ()


def test_an_unpublished_topping_is_dropped_rather_than_resolved(
    matcher: MealMatcher,
) -> None:
    """The same refusal on an optional slot, which drops instead of asking."""
    resolution = matcher.resolve(described(toppings=[("queso", SURE)]))

    assert resolution.outcome is Outcome.RESOLVED
    assert [item.slot for item in resolution.modifiers] == [Slot.RICE, Slot.BEANS]
    assert [dropped.term for dropped in resolution.discarded] == ["queso"]


# --- AC2: a low-confidence protein asks rather than guesses ------------------


def test_a_low_confidence_protein_asks_a_question(matcher: MealMatcher) -> None:
    """PRD V5, and the acceptance criterion that names this slot specifically.

    The protein is legible, publishable and above every other slot's floor. It
    is below *its* floor, and that is the whole of the difference: nothing is
    proposed and the turn asks.
    """
    resolution = matcher.resolve(described(at={"protein": 0.4}))

    assert resolution.escalates
    assert resolution.outcome is Outcome.CLARIFY
    assert resolution.item_ids() == ()
    assert resolution.total() is None
    (clarification,) = resolution.clarifications
    assert clarification.slot is Slot.PROTEIN
    assert clarification.reason is ClarificationReason.LOW_CONFIDENCE
    assert clarification.term == "chicken"
    assert clarification.confidence == pytest.approx(0.4)


def test_the_same_protein_above_its_floor_resolves(matcher: MealMatcher) -> None:
    """The other half of the previous test: the floor is doing the work.

    Same photograph, same term, one number different. If this resolved for any
    reason other than the confidence, the test above would be measuring
    something else.
    """
    floor = matcher.rules.for_slot(Slot.PROTEIN).floor
    resolution = matcher.resolve(described(at={"protein": floor}))

    assert resolution.outcome is Outcome.RESOLVED
    assert resolution.entree is not None


def test_a_required_slot_the_model_left_out_is_a_question(
    matcher: MealMatcher,
) -> None:
    """An omitted slot is a question, which is what the stage-4 prompt promises.

    "Leave a slot out rather than filling it with the most likely answer -- an
    omitted slot becomes a question to the visitor." This is where that becomes
    true.
    """
    resolution = matcher.resolve(described(rice=None))

    assert resolution.outcome is Outcome.CLARIFY
    (clarification,) = resolution.clarifications
    assert clarification.slot is Slot.RICE
    assert clarification.reason is ClarificationReason.MISSING
    assert clarification.term is None


def test_an_optional_slot_the_model_left_out_is_simply_absent(
    matcher: MealMatcher,
) -> None:
    """No salsa in the frame is not a question. It is a bowl with no salsa."""
    resolution = matcher.resolve(described(salsas=[], toppings=[]))

    assert resolution.outcome is Outcome.RESOLVED
    assert resolution.clarifications == ()
    assert resolution.discarded == ()


def test_a_low_confidence_topping_is_dropped_and_recorded(
    matcher: MealMatcher,
) -> None:
    """The asymmetry: an optional slot below its floor is dropped, not asked about.

    A topping the model half-saw must not arrive as an order the visitor did
    not want, and the confirmation card is editable in place -- so the cheap
    correction is adding one back rather than noticing one that was never
    mentioned. It is recorded either way, because a floor nobody can see the
    effect of is a floor nobody can tune.
    """
    resolution = matcher.resolve(
        described(toppings=[("guacamole", SURE), ("cheese", 0.2)])
    )

    assert resolution.outcome is Outcome.RESOLVED
    assert [item.term for item in resolution.modifiers] == [
        "white_rice",
        "black_beans",
        "guacamole",
    ]
    (dropped,) = resolution.discarded
    assert dropped.slot is Slot.TOPPINGS
    assert dropped.term == "cheese"
    assert dropped.reason is ClarificationReason.LOW_CONFIDENCE


def test_the_less_certain_half_of_an_impossible_pair_is_the_one_asked_about(
    matcher: MealMatcher,
) -> None:
    """It is the *pair* that failed, so which half to ask about is a choice.

    Asking about the half the model was more sure of reads to a visitor as not
    having looked at the photograph, so the question goes to the other one.
    """
    asked_about_vessel = matcher.resolve(
        described(vessel="bowl", protein="steak", at={"vessel": 0.72, "protein": 0.99})
    )
    asked_about_protein = matcher.resolve(
        described(vessel="bowl", protein="steak", at={"vessel": 0.99, "protein": 0.80})
    )

    assert [c.slot for c in asked_about_vessel.clarifications] == [Slot.VESSEL]
    assert [c.slot for c in asked_about_protein.clarifications] == [Slot.PROTEIN]


def test_nothing_is_proposed_alongside_a_question(matcher: MealMatcher) -> None:
    """A draft is complete or there is no draft.

    A card missing its protein, priced as though it were not, is a worse thing
    to put in front of a visitor than a question -- the price is wrong and the
    order is incomplete, and neither is visible at a glance. The slots that
    *did* resolve are not lost: they are on the clarifications as terms, which
    is what issue #55 renders the question from.
    """
    resolution = matcher.resolve(described(at={"protein": 0.1}))

    assert resolution.entree is None
    assert resolution.modifiers == ()
    assert resolution.items() == ()
    assert resolution.total() is None


# --- AC3: thresholds are configuration, not constants ------------------------


def test_every_slot_has_a_floor(matcher: MealMatcher) -> None:
    """A slot with no configured floor is an error rather than a permissive one."""
    for slot in Slot:
        assert 0.0 <= matcher.rules.for_slot(slot).floor <= 1.0


def test_the_default_required_slots_are_the_ones_a_draft_needs(
    matcher: MealMatcher,
) -> None:
    """Vessel and protein because they are the entree; rice and beans because
    ``docs/action-surface.md`` reads their groups off the menu as ``(1, 1)``."""
    assert matcher.rules.required == REQUIRED_SLOTS


def test_a_floor_read_from_the_environment_changes_the_answer(
    catalog: MenuCatalog,
) -> None:
    """The criterion, stated as the thing it is for: retuning without a code change.

    One environment variable, one confidence, two different outcomes for the
    same photograph. That is what "configuration, not constants" has to mean if
    issue #56 is going to tune these against a labeled set.
    """
    meal = described(at={"protein": 0.5})
    strict = MealMatcher(catalog, rules=SlotRules.from_env({}))
    relaxed = MealMatcher(
        catalog, rules=SlotRules.from_env({f"{ENV_PREFIX}PROTEIN_THRESHOLD": "0.4"})
    )

    assert strict.resolve(meal).outcome is Outcome.CLARIFY
    assert relaxed.resolve(meal).outcome is Outcome.RESOLVED


def test_required_ness_is_configuration_too(catalog: MenuCatalog) -> None:
    """The published grammar is per ``item_type``, so the required set is a knob.

    A taco has no rice group at all. Turning the requirement off is how a
    catalogue whose entrees do not carry one is matched without editing this
    package.
    """
    relaxed = MealMatcher(
        catalog, rules=SlotRules.from_env({f"{ENV_PREFIX}BEANS_REQUIRED": "false"})
    )

    assert Slot.BEANS not in relaxed.rules.required
    assert relaxed.resolve(described(beans=None)).outcome is Outcome.RESOLVED


def test_an_unset_environment_is_the_argued_starting_point() -> None:
    """No configuration means the defaults, never an unthresholded matcher."""
    assert SlotRules.from_env({}) == SlotRules.defaults()


def test_the_protein_floor_is_above_the_topping_floor() -> None:
    """ "Protein being wrong matters more than a topping being wrong", asserted.

    The numbers will move against the labeled set. The ordering is the argument,
    and it should survive the retuning.
    """
    rules = SlotRules.defaults()
    assert rules.for_slot(Slot.PROTEIN).floor > rules.for_slot(Slot.TOPPINGS).floor
    assert rules.for_slot(Slot.PROTEIN).floor > rules.for_slot(Slot.RICE).floor


@pytest.mark.parametrize("floor", [-0.1, 1.1])
def test_a_floor_that_is_not_a_probability_is_refused(floor: float) -> None:
    """Above one refuses every photograph; below zero accepts whatever was said."""
    with pytest.raises(ValueError, match="floor"):
        SlotRule(floor=floor, required=True)


def test_an_unparseable_floor_fails_at_startup() -> None:
    """A misspelled threshold that kept the default would be a tuning run that
    measured the wrong number."""
    with pytest.raises(ValueError, match="PROTEIN_THRESHOLD"):
        SlotRules.from_env({f"{ENV_PREFIX}PROTEIN_THRESHOLD": "quite high"})


def test_an_unparseable_required_flag_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="RICE_REQUIRED"):
        SlotRules.from_env({f"{ENV_PREFIX}RICE_REQUIRED": "sometimes"})


# --- AC4: the resolved draft prices correctly from the catalogue -------------


def _price(catalog: MenuCatalog, restaurant: int, item_id: str) -> Decimal:
    """The published price of one item at one restaurant."""
    for row in catalog.item_prices:
        if row.restaurant_id == restaurant and row.item_id == item_id:
            return row.unit_price
    raise AssertionError(f"{item_id} has no price at {restaurant}")


def test_the_total_is_the_sum_of_the_published_prices(
    matcher: MealMatcher, catalog: MenuCatalog
) -> None:
    """Not a number this package computes -- a number it adds up out of rows."""
    resolution = matcher.resolve(described(toppings=[("guacamole", SURE)]))

    assert resolution.outcome is Outcome.RESOLVED
    expected = sum(
        (
            _price(catalog, REFERENCE_RESTAURANT, item_id)
            for item_id in resolution.item_ids()
        ),
        Decimal(0),
    )
    assert resolution.total() == expected
    for item in resolution.items():
        assert item.unit_price == _price(catalog, REFERENCE_RESTAURANT, item.item_id)


def test_prices_are_the_restaurant_s_own(matcher: MealMatcher) -> None:
    """Money is local -- ``docs/decisions/menu-pricing.md``.

    The same draft at two restaurants is two totals, and quoting the reference
    restaurant's prices to somebody ordering elsewhere is the failure a
    catalogue with one restaurant in it cannot show.
    """
    meal = described(toppings=[("guacamole", SURE)])
    here = matcher.resolve(meal, restaurant_id=REFERENCE_RESTAURANT)
    there = matcher.resolve(meal, restaurant_id=COMPARISON_RESTAURANT)

    assert here.item_ids() == there.item_ids()
    assert here.total() is not None
    assert there.total() is not None
    assert there.total() != here.total()
    assert there.restaurant_id == COMPARISON_RESTAURANT


def test_the_reference_restaurant_is_the_default(
    matcher: MealMatcher, catalog: MenuCatalog
) -> None:
    resolution = matcher.resolve(described())
    assert resolution.restaurant_id == catalog.reference_restaurant_id


def test_the_same_ingredient_on_two_entrees_is_two_rows_at_two_prices(
    matcher: MealMatcher,
) -> None:
    """ "The first mistake a naive matcher makes", asserted as a difference.

    Guacamole is published under one identifier per parent, and the two carry
    different prices. A matcher that resolved the term without consulting the
    entree would return the same identifier for both, and the two lines below
    would be equal.
    """
    topping = [("guacamole", SURE)]
    on_a_bowl = matcher.resolve(described(vessel="bowl", toppings=topping))
    on_a_burrito = matcher.resolve(described(vessel="burrito", toppings=topping))

    (bowl_guacamole,) = [item for item in on_a_bowl.modifiers if item.term == "guacamole"]
    (burrito_guacamole,) = [
        item for item in on_a_burrito.modifiers if item.term == "guacamole"
    ]
    assert bowl_guacamole.item_id != burrito_guacamole.item_id
    assert bowl_guacamole.unit_price != burrito_guacamole.unit_price


def test_an_item_with_no_price_row_does_not_total_to_a_smaller_number() -> None:
    """``None`` is not zero, and a total missing a line is a wrong number.

    A partial sum is the dangerous answer here: it looks like a price, it is
    lower than the real one, and nothing about it says a line is missing.
    """
    matcher = MealMatcher(menu_catalog(unpriced=["guacamole"]))
    resolution = matcher.resolve(described(toppings=[("guacamole", SURE)]))

    assert resolution.outcome is Outcome.RESOLVED
    (guacamole,) = [item for item in resolution.modifiers if item.term == "guacamole"]
    assert guacamole.unit_price is None
    assert resolution.total() is None


def test_a_resolution_with_nothing_on_it_has_no_total(matcher: MealMatcher) -> None:
    """Zero is a price. An order that does not exist does not have one."""
    assert matcher.resolve(described(at={"protein": 0.1})).total() is None


# --- the two gates in front of stage 5 ---------------------------------------


def test_several_meals_in_one_frame_does_not_resolve(matcher: MealMatcher) -> None:
    """RFC-001 section 07: at two or more, stage 5 does not run.

    The schema returns one slot set, so on a frame with several meals those
    slots describe the photograph rather than any one meal -- and resolving them
    produces a draft composed entirely of real catalogue items that nobody in
    the picture is eating.
    """
    resolution = matcher.resolve(described(meals_visible=2))

    assert resolution.outcome is Outcome.SEVERAL_MEALS
    assert resolution.item_ids() == ()
    assert resolution.clarifications == ()


def test_the_meal_count_survives_onto_the_resolution(matcher: MealMatcher) -> None:
    """PRD V7 requires saying how many were seen rather than picking one, so the
    number has to reach whoever writes the sentence."""
    assert matcher.resolve(described(meals_visible=4)).meals_visible == 4


def test_food_this_restaurant_does_not_serve_does_not_resolve(
    matcher: MealMatcher,
) -> None:
    """PRD V4. What to offer instead is issue #55's; refusing to build a draft
    out of a poke bowl's slots is this one's."""
    resolution = matcher.resolve(described(is_chipotle_style=False))

    assert resolution.outcome is Outcome.NOT_ORDERABLE
    assert resolution.item_ids() == ()


def test_neither_gate_is_an_exception(matcher: MealMatcher) -> None:
    """A deterministic matcher answering "not this" is a result, not a failure.

    Stage 4 raises because a model that did not answer has nothing to return.
    Stage 5 always has something to return, and a caller that had to catch two
    exceptions to find out which of four ordinary outcomes happened would write
    the branch wrong.
    """
    outcomes = [
        matcher.resolve(described(meals_visible=2)).outcome,
        matcher.resolve(described(is_chipotle_style=False)).outcome,
    ]
    assert outcomes == [Outcome.SEVERAL_MEALS, Outcome.NOT_ORDERABLE]


# --- one catalogue, one vocabulary -------------------------------------------


def test_a_description_from_another_catalogue_build_is_refused(
    matcher: MealMatcher,
) -> None:
    """The drift the vocabulary and the catalogue must not be able to have.

    Resolving terms constrained by one menu against the rows of another is how
    a real SKU ends up in front of a visitor for food the photograph does not
    show. It is a build fault, so it raises rather than becoming an outcome.
    """
    with pytest.raises(CatalogueDriftError, match="regenerate the vocabulary"):
        matcher.resolve(described(), content_version="0" * 64)


def test_the_catalogue_s_own_content_version_resolves(
    matcher: MealMatcher, catalog: MenuCatalog
) -> None:
    resolution = matcher.resolve(described(), content_version=catalog.content_version())

    assert resolution.outcome is Outcome.RESOLVED
    assert resolution.content_version == catalog.content_version()


def test_a_vocabulary_module_and_the_catalogue_publish_the_same_terms(
    catalog: MenuCatalog,
) -> None:
    """The fixture pair is one build, which is what makes every test above valid.

    ``generated_vocabulary()`` is what stage 4 constrains the model to and
    ``menu_catalog()`` is what stage 5 resolves against. If they published
    different terms, a resolution failing would prove nothing about the matcher.
    """
    vocabulary = generated_vocabulary()
    for slot in Slot:
        published = {row.value for row in catalog.vocabulary if row.slot is slot}
        assert published == set(vocabulary.values(slot.value))


# --- the span ----------------------------------------------------------------


def _recorded(value: AttributeValue) -> tuple[object, ...]:
    """Read one list-valued span attribute back.

    OpenTelemetry's attribute type is a union of scalars and homogeneous
    sequences, so a test that wants the list has to say which it expected.
    """
    assert isinstance(value, Sequence)
    assert not isinstance(value, str)
    return tuple(value)


def test_the_span_records_slot_confidences_and_resolved_skus(
    matcher: MealMatcher, spans: SpanRecorder
) -> None:
    """RFC-001 section 09 names both, and they are what a threshold is retuned from.

    The terms are the model's and the SKUs are the catalogue's, and they are
    separate attributes so a trace can show a term that resolved to nothing.
    """
    resolution = matcher.resolve(described(toppings=[("guacamole", SURE)]))
    recorded = spans.attributes_of("matcher.resolve")

    assert set(_recorded(recorded["chip_chat.matcher.slot_values"])) == {
        "vessel=bowl",
        "protein=chicken",
        "rice=white_rice",
        "beans=black_beans",
        "toppings[0]=guacamole",
    }
    assert set(_recorded(recorded["chip_chat.matcher.slot_confidences"])) == {SURE}
    assert _recorded(recorded["chip_chat.matcher.resolved_skus"]) == resolution.item_ids()


def test_the_span_says_when_the_turn_escalated(
    matcher: MealMatcher, spans: SpanRecorder
) -> None:
    """An escalation nobody can count is a threshold nobody can tune."""
    matcher.resolve(described(at={"protein": 0.4}))
    recorded = spans.attributes_of("matcher.resolve")

    assert recorded["chip_chat.matcher.escalated"] is True
    assert recorded["chip_chat.guard.reason"] == "protein:low_confidence"


def test_the_span_is_silent_about_escalation_when_there_was_none(
    matcher: MealMatcher, spans: SpanRecorder
) -> None:
    matcher.resolve(described())
    assert "chip_chat.matcher.escalated" not in spans.attributes_of("matcher.resolve")


def test_resolve_as_tool_opens_the_two_spans_above_it(
    matcher: MealMatcher, spans: SpanRecorder, outside_a_tool_call: None
) -> None:
    """``matcher.resolve`` is a child of ``tool.<tool_name>``, which is a child
    of ``agent.step`` -- and :mod:`chip_chat.otel` enforces that rather than
    documenting it, so a caller that is not the agent needs this."""
    matcher.resolve_as_tool(described(), image_ref=IMAGE_REF)

    assert spans.names()[:3] == (
        "matcher.resolve",
        "tool.match_meal_from_photo",
        "agent.step",
    )


def test_the_photograph_reaches_no_span(
    matcher: MealMatcher, spans: SpanRecorder, outside_a_tool_call: None
) -> None:
    """A reference crosses the tool boundary. The image does not, and here there
    are no bytes to cross it with -- stage 5 is handed slots."""
    matcher.resolve_as_tool(described(), image_ref=IMAGE_REF)
    recorded = spans.attributes_of("tool.match_meal_from_photo")

    assert IMAGE_REF in str(recorded["tool.parameters"])
    assert "base64" not in str(recorded)


# --- against a catalogue nobody wrote by hand --------------------------------


def test_the_matcher_resolves_against_a_catalogue_built_from_the_harvest() -> None:
    """Everything above uses a fixture catalogue. This one uses a built one.

    ``catalog/tests/catalog_fixtures.py`` builds a real
    :class:`~chip_chat.catalog.records.MenuCatalog` by harvesting the fixture
    site and running the whole consolidation over it, vocabulary generation
    included. A matcher that worked only against the shape this package's own
    fixtures happen to have would pass every test above and fail on the first
    real catalogue.
    """
    catalog = fixture_catalog()
    matcher = MealMatcher(catalog)
    published = {item.item_id for item in catalog.menu_items}

    resolution = matcher.resolve(
        described(
            vessel="bowl",
            protein="chicken",
            rice="white_rice",
            beans="black_beans",
            toppings=[("guacamole", SURE)],
        ),
        content_version=catalog.content_version(),
    )

    assert resolution.outcome is Outcome.RESOLVED
    assert set(resolution.item_ids()) <= published
    assert resolution.entree is not None
    assert resolution.entree.item_id == "CMG-101"
    assert resolution.total() == Decimal("14.10")


def test_the_built_catalogue_refuses_a_pair_it_does_not_sell() -> None:
    """The fixture site publishes a Chicken Bowl and a Steak Burrito and no
    Steak Bowl, so the case is real rather than arranged."""
    matcher = MealMatcher(fixture_catalog())
    resolution = matcher.resolve(described(vessel="bowl", protein="steak"))

    assert resolution.outcome is Outcome.CLARIFY
    assert resolution.item_ids() == ()
