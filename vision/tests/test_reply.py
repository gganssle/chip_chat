"""Issue #55: the three photographs that are not the happy path.

Each of the three has a specified behaviour rather than a default, and each has
a section here that puts a photograph in one end of the pipeline and asserts on
the sentence that comes out the other:

* food this restaurant does not serve is *told so*, and offered the closest
  thing it does have -- not refused, and not silently matched anyway
* a component the model was unsure of becomes a question naming *that slot*
* several meals in one frame become a count and a question, and nothing is built

"A test with a real photo" is the first acceptance criterion, and what stands in
for it is a photograph that really is one -- encoded JPEG bytes, through the
same four gates a stranger's upload goes through, read back out of the store by
the describer and resolved by the real matcher against a real catalogue. What is
stubbed is the vision deployment, because a test cannot hold a model's opinion
still and every assertion here is about what happens *given* an opinion. The
thirty labelled photographs that score the opinion itself are issue #56, which
this issue unblocks.

The fourth section is the one that keeps the other three honest. PRD V6 -- never
name a menu item that does not exist -- is a property of this layer too, because
this layer is where the words a visitor reads are assembled. It holds because
:func:`~chip_chat.vision.reply.reply_for` is given a resolution and nothing
else, and the test for it is the same move ``test_vocabulary.py`` makes: change
the catalogue, and watch the sentence change with it.
"""

import ast
import inspect
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from chip_chat.catalog.records import MenuCatalog, Slot
from chip_chat.vision import reply as reply_module
from chip_chat.vision.describe import Description, MealDescriber
from chip_chat.vision.intake import PhotoIntake
from chip_chat.vision.matcher import (
    NOTHING_SEEN,
    ClarificationReason,
    MealMatcher,
    Outcome,
    Resolution,
)
from chip_chat.vision.moderation import ImageModerator
from chip_chat.vision.reply import (
    SLOT_NOUNS,
    Reply,
    ReplyKind,
    reply_for,
    slot_noun,
)
from chip_chat.vision.testing import (
    REFERENCE_RESTAURANT,
    InMemoryBlobStore,
    StubImageAnalyzer,
    StubVisionModel,
    generated_vocabulary,
    menu_catalog,
    photo_tool_call,
    published,
    solid_image,
)

A_MEAL: Mapping[str, Any] = {
    "is_chipotle_style": True,
    "meals_visible": 1,
    "vessel": {"value": "bowl", "confidence": 0.94},
    "protein": {"value": "chicken", "confidence": 0.88},
    "rice": {"value": "white_rice", "confidence": 0.81},
    "beans": {"value": "black_beans", "confidence": 0.79},
    "notes": "Looks like a generous scoop of everything.",
}
"""A stage-4 answer every default floor believes. The three cases edit it."""


@dataclass(frozen=True, slots=True)
class Turn:
    """One photograph, all the way through: what was seen, matched and said."""

    description: Description
    resolution: Resolution
    reply: Reply


@pytest.fixture
def catalog() -> MenuCatalog:
    """A catalogue publishing rows for the fixture vocabulary.

    ``("bowl", "steak")`` is left out for the same reason
    ``test_matcher.py`` leaves it out: a menu that sold every pair of every term
    could not exercise two real terms naming no real row.
    """
    return menu_catalog(without=[("bowl", "steak")])


@pytest.fixture
def photograph(catalog: MenuCatalog) -> "Photographed":
    """Send one photograph through the whole pipeline and answer the visitor."""
    return Photographed(catalog)


class Photographed:
    """Stages 1 to 5 and the sentence, over a JPEG that really is one.

    The intake is the real one -- validate, normalize, moderate, write -- so the
    reference stage 4 is handed is a reference to a photograph that passed
    moderation, which is the ordering RFC-001 section 07 requires and not a
    fixture's promise. Only the vision deployment is a stub, and the response it
    is scripted with is the whole subject of every test here.
    """

    def __init__(
        self, catalog: MenuCatalog, terms: Mapping[str, Sequence[str]] | None = None
    ) -> None:
        self._store = InMemoryBlobStore()
        self._intake = PhotoIntake(
            store=self._store, moderator=ImageModerator(analyzer=StubImageAnalyzer())
        )
        self._catalog = catalog
        self._terms = terms

    def __call__(
        self,
        described: Mapping[str, Any],
        *,
        image: bytes | None = None,
        restaurant_id: int | None = None,
    ) -> Turn:
        """Upload one photograph, describe it, resolve it, and answer.

        Args:
            described: What the vision deployment is scripted to answer.
            image: The photograph. A plain JPEG by default.
            restaurant_id: Whose prices and availability apply.

        Returns:
            The :class:`Turn`.
        """
        photo = self._intake.accept(
            solid_image() if image is None else image, declared_media_type="image/jpeg"
        )
        describer = MealDescriber(
            StubVisionModel(response=json.dumps(described)),
            images=self._store,
            vocabulary=generated_vocabulary(self._terms),
        )
        matcher = MealMatcher(self._catalog)
        # The vocabulary fixture and the catalogue fixture are two builds, so
        # the drift check would fire on a version this test has no opinion
        # about. That check is `test_matcher.py`'s, and asserted there.
        with photo_tool_call(photo.blob_ref):
            description = describer.describe(photo.blob_ref)
            resolution = matcher.resolve(description.meal, restaurant_id=restaurant_id)
        return Turn(
            description=description,
            resolution=resolution,
            reply=reply_for(resolution),
        )


def _edited(**slots: Any) -> Mapping[str, Any]:
    """:data:`A_MEAL` with some slots replaced, and any set to ``None`` removed."""
    described = dict(A_MEAL)
    for name, value in slots.items():
        if value is None:
            described.pop(name, None)
        else:
            described[name] = value
    return described


# --- case 1: food that is not this restaurant's food -------------------------
#
# PRD V4 is two clauses in one breath, and issue #55's second acceptance
# criterion is that the second one never goes missing: "always offers a concrete
# alternative, never a bare refusal".


@pytest.fixture
def poke_bowl(photograph: Photographed) -> Turn:
    """A photograph of food this menu does not serve, in a vessel it does.

    The model still fills what slots it can from the generated vocabulary --
    that is what the vocabulary is for -- so the interesting part is that a
    bowl of raw fish and rice comes back as a bowl, with rice, and
    ``is_chipotle_style`` false.
    """
    return photograph(
        _edited(
            is_chipotle_style=False,
            protein=None,
            beans=None,
            notes="That looks like a poke bowl.",
        )
    )


def test_food_we_do_not_make_is_said_to_be_food_we_do_not_make(poke_bowl: Turn) -> None:
    assert poke_bowl.resolution.outcome is Outcome.NOT_ORDERABLE
    assert poke_bowl.reply.kind is ReplyKind.ALTERNATIVE
    assert "isn't something we make" in poke_bowl.reply.text


def test_the_alternative_is_a_concrete_meal_and_not_a_refusal(poke_bowl: Turn) -> None:
    """The whole of the second acceptance criterion, in one assertion each way."""
    offered = poke_bowl.reply.items
    assert offered, "a bare refusal is the one behaviour V4 rules out"
    assert "The closest we do is a Chicken Bowl with White Rice and Black Beans" in (
        poke_bowl.reply.text
    )
    assert [item.name for item in offered] == [
        "Chicken Bowl",
        "White Rice",
        "Black Beans",
    ]


def test_the_alternative_is_composed_only_of_real_catalogue_rows(
    poke_bowl: Turn, catalog: MenuCatalog
) -> None:
    """PRD V6, on the one path where nothing in the frame was on the menu."""
    published_ids = {item.item_id for item in catalog.menu_items}
    for item in poke_bowl.reply.items:
        assert item.item_id in published_ids
        assert item.name in {
            row.name for row in catalog.menu_items if row.item_id == item.item_id
        }


def test_the_alternative_is_available_and_priced(poke_bowl: Turn) -> None:
    """ "Available" is the word PRD V4 uses, so it is a filter and not a hope."""
    for item in poke_bowl.reply.items:
        assert item.available is True
        assert item.unit_price is not None


def test_the_alternative_keeps_what_the_photograph_did_show(
    photograph: Photographed,
) -> None:
    """A burrito of something we do not serve is offered a burrito.

    "Closest" is the word in the requirement, and the believed vessel is the
    most visible thing about a photograph of food. An offer that ignored it
    would be a house favourite wearing the word.
    """
    turn = photograph(
        _edited(
            is_chipotle_style=False,
            vessel={"value": "burrito", "confidence": 0.93},
            protein=None,
            rice=None,
            beans=None,
        )
    )
    assert turn.reply.items[0].name == "Chicken Burrito"


def test_the_alternative_honours_a_believed_protein_too(
    photograph: Photographed,
) -> None:
    turn = photograph(
        _edited(
            is_chipotle_style=False,
            vessel={"value": "burrito", "confidence": 0.93},
            protein={"value": "steak", "confidence": 0.91},
            rice=None,
            beans=None,
        )
    )
    assert turn.reply.items[0].name == "Steak Burrito"


def test_a_slot_below_its_floor_does_not_steer_the_alternative(
    photograph: Photographed,
) -> None:
    """An offer built from a term nobody believed is a guess with a nicer name."""
    turn = photograph(
        _edited(
            is_chipotle_style=False,
            vessel={"value": "burrito", "confidence": 0.21},
            protein=None,
            rice=None,
            beans=None,
        )
    )
    assert turn.reply.items[0].name == "Chicken Bowl"


def test_the_alternative_carries_no_confidence_it_was_never_given(
    poke_bowl: Turn,
) -> None:
    """Nothing in the frame was seen as a Chicken Bowl. The frame is a poke bowl."""
    assert {item.confidence for item in poke_bowl.reply.items} == {NOTHING_SEEN}


def test_the_alternative_is_not_a_draft(poke_bowl: Turn) -> None:
    """It is offered, not proposed: no entree, no item ids, no total."""
    resolution = poke_bowl.resolution
    assert resolution.entree is None
    assert resolution.item_ids() == ()
    assert resolution.total() is None
    assert resolution.resolved is False


def test_nothing_unavailable_is_ever_offered(photograph: Photographed) -> None:
    """A menu with no available entree still gets a next step, not a full stop."""
    empty = menu_catalog(terms={"vessel": ("bowl",), "protein": ("chicken",)})
    turn = Photographed(_sold_out(empty))(
        _edited(is_chipotle_style=False, rice=None, beans=None)
    )
    assert turn.reply.kind is ReplyKind.ALTERNATIVE
    assert turn.reply.items == ()
    assert "Tell me what you're after" in turn.reply.text


# --- case 2: a component the model was not sure of ---------------------------
#
# PRD V5 asks a question rather than guessing; PRD V3 says what the question has
# to contain. Issue #55's third acceptance criterion is the join of the two: the
# clarifying question names the uncertain slot.


@pytest.fixture
def unsure_about_the_protein(photograph: Photographed) -> Turn:
    """Everything plain except the one slot half-hidden under everything else."""
    return photograph(_edited(protein={"value": "chicken", "confidence": 0.31}))


def test_a_component_below_its_floor_becomes_a_question(
    unsure_about_the_protein: Turn,
) -> None:
    turn = unsure_about_the_protein
    assert turn.resolution.outcome is Outcome.CLARIFY
    assert turn.reply.kind is ReplyKind.QUESTION
    assert [item.reason for item in turn.resolution.clarifications] == [
        ClarificationReason.LOW_CONFIDENCE
    ]


def test_the_question_names_the_uncertain_slot(unsure_about_the_protein: Turn) -> None:
    """The acceptance criterion, and the whole difference from a generic hedge."""
    turn = unsure_about_the_protein
    assert turn.reply.asks_about == (Slot.PROTEIN,)
    assert "I'm least sure about the protein" in turn.reply.text


def test_the_question_says_what_it_believes_it_saw(
    unsure_about_the_protein: Turn,
) -> None:
    """PRD V3, in the visitor's language, so they can correct it."""
    turn = unsure_about_the_protein
    assert "It looks like a Bowl with White Rice and Black Beans." in turn.reply.text
    assert "I read it as Chicken. Is that right?" in turn.reply.text


def test_a_generic_hedge_is_not_what_this_produces(
    unsure_about_the_protein: Turn,
) -> None:
    """Named as a test because it is the failure mode, not merely an absence."""
    text = unsure_about_the_protein.reply.text.lower()
    assert "i might be wrong" not in text
    assert "i'm not certain about all" not in text


def test_a_question_proposes_nothing(unsure_about_the_protein: Turn) -> None:
    """A draft under a question is a guess with a disclaimer on it."""
    turn = unsure_about_the_protein
    assert turn.reply.builds_nothing
    assert turn.reply.items == ()
    assert turn.resolution.item_ids() == ()


def test_a_slot_the_model_left_out_is_asked_about_by_name(
    photograph: Photographed,
) -> None:
    turn = photograph(_edited(beans=None))
    assert turn.reply.asks_about == (Slot.BEANS,)
    assert "I couldn't make out the beans — what was it?" in turn.reply.text


def test_two_missing_slots_become_one_question_naming_both(
    photograph: Photographed,
) -> None:
    turn = photograph(_edited(rice=None, beans=None))
    assert turn.reply.asks_about == (Slot.RICE, Slot.BEANS)
    assert (
        "I couldn't make out the rice or the beans — what were they?" in turn.reply.text
    )


def test_two_real_terms_naming_no_real_row_ask_about_the_less_certain_half(
    photograph: Photographed,
) -> None:
    """The menu sells a Chicken Bowl and a Steak Burrito and no Steak Bowl."""
    turn = photograph(
        _edited(
            vessel={"value": "bowl", "confidence": 0.95},
            protein={"value": "steak", "confidence": 0.80},
        )
    )
    assert turn.reply.asks_about == (Slot.PROTEIN,)
    assert "I read the protein as Steak, and that isn't one we make here" in (
        turn.reply.text
    )


def test_the_question_does_not_claim_to_have_seen_what_it_is_asking_about(
    photograph: Photographed,
) -> None:
    """A term above its floor that resolves to nothing is both, and says one.

    "I can see Steak" followed by "what was the protein?" reads as not having
    looked at the photograph, which is the opposite of what V3 is for.
    """
    turn = photograph(
        _edited(
            vessel={"value": "bowl", "confidence": 0.95},
            protein={"value": "steak", "confidence": 0.80},
        )
    )
    assert "Steak" not in turn.reply.text.split("I read the protein")[0]


def test_every_slot_has_a_word_a_visitor_would_use() -> None:
    """A slot added to the schema without one would be asked about by its key.

    ``"I couldn't make out salsas"`` is the schema leaking into a sentence a
    stranger reads, and it is what a missing entry here would produce.
    """
    assert set(SLOT_NOUNS) == set(Slot)
    for slot in Slot:
        noun = slot_noun(slot)
        assert noun == SLOT_NOUNS[slot]
        assert noun != slot.value
        assert noun.startswith(("the ", "what "))


# --- case 3: several meals in one frame --------------------------------------
#
# Decided in #58: detect and decline gracefully. `docs/decisions/multi-meal-
# photos.md` states three properties of the turn as requirements rather than as
# copy, and each is a test here.


@pytest.fixture
def a_table_of_four(photograph: Photographed) -> Turn:
    """One frame, four orderable meals -- the photograph of a table."""
    return photograph(_edited(meals_visible=4))


def test_several_meals_stop_the_pipeline_before_anything_resolves(
    a_table_of_four: Turn,
) -> None:
    turn = a_table_of_four
    assert turn.resolution.outcome is Outcome.SEVERAL_MEALS
    assert turn.resolution.entree is None
    assert turn.resolution.item_ids() == ()
    assert turn.resolution.seen == ()


def test_the_decline_says_how_many_it_saw(a_table_of_four: Turn) -> None:
    """ "Several meals" is a category; "about four" is a correctable observation."""
    assert "about four meals" in a_table_of_four.reply.text
    assert a_table_of_four.reply.meals_visible == 4


def test_the_decline_offers_a_next_step_in_both_modalities(
    a_table_of_four: Turn,
) -> None:
    text = a_table_of_four.reply.text
    assert "Send me a photo of just the one you want" in text
    assert "tell me which it is" in text


def test_the_decline_builds_nothing(a_table_of_four: Turn) -> None:
    """The worst failure shape this product has, asserted as an absence."""
    turn = a_table_of_four
    assert turn.reply.kind is ReplyKind.ONE_AT_A_TIME
    assert turn.reply.builds_nothing
    assert turn.reply.items == ()


def test_two_meals_are_counted_rather_than_estimated(photograph: Photographed) -> None:
    """Two things in a frame is not a guess, and hedging it invites a correction."""
    turn = photograph(_edited(meals_visible=2))
    assert "Looks like two meals in that photo" in turn.reply.text


@pytest.mark.parametrize("count", [2, 3, 4, 9, 17])
def test_the_gate_is_the_count_and_nothing_else(
    photograph: Photographed, count: int
) -> None:
    """Detection is the whole behaviour, so it is asserted across the range."""
    turn = photograph(_edited(meals_visible=count))
    assert turn.resolution.outcome is Outcome.SEVERAL_MEALS
    assert turn.reply.kind is ReplyKind.ONE_AT_A_TIME
    assert str(count) in turn.reply.text or _worded(count) in turn.reply.text


def test_one_meal_beside_a_side_is_one_meal(photograph: Photographed) -> None:
    """The likeliest false positive, and the one that costs a working order.

    ``meals_visible`` counts orderable meal-sized compositions, so a bowl with a
    bag of chips next to it is one. The count is the model's to make and issue
    #56 scores it; what is asserted here is that a one at this gate builds an
    order rather than asking which meal.
    """
    turn = photograph(_edited(meals_visible=1))
    assert turn.resolution.outcome is Outcome.RESOLVED
    assert turn.reply.kind is ReplyKind.PROPOSAL


def test_several_meals_wins_over_food_we_do_not_serve(
    photograph: Photographed,
) -> None:
    """A table of four poke bowls is both, and asking which is the safer answer.

    Offering the closest thing would answer the question -- which meal? -- that
    PRD V7 requires asking rather than answering.
    """
    turn = photograph(_edited(is_chipotle_style=False, meals_visible=4))
    assert turn.reply.kind is ReplyKind.ONE_AT_A_TIME
    assert turn.resolution.alternative == ()


# --- PRD V6: no sentence here can name something the menu does not ------------


def test_the_sentence_changes_when_the_catalogue_does() -> None:
    """The `test_vocabulary.py` move, one layer up.

    No food word is written in ``reply.py``, and the only way to settle that is
    to build a different menu and watch the same code say different words. If a
    name were hard-coded anywhere below, this is the test that would still be
    saying "Chicken Bowl".
    """
    terms = {
        "vessel": ("cauldron",),
        "protein": ("tofu",),
        "rice": ("purple_rice",),
        "beans": ("navy_beans",),
    }
    described = _edited(
        is_chipotle_style=False,
        vessel={"value": "cauldron", "confidence": 0.9},
        protein=None,
        rice=None,
        beans=None,
    )
    turn = Photographed(menu_catalog(terms=terms), terms)(described)
    assert "a Tofu Cauldron with Purple Rice and Navy Beans" in turn.reply.text
    for term in ("cauldron", "tofu", "purple_rice", "navy_beans"):
        assert published(term) in turn.reply.text


def test_the_replier_is_given_a_resolution_and_nothing_else() -> None:
    """The arrangement PRD V6 rests on here, asserted rather than described."""
    signature = inspect.signature(reply_for)
    assert list(signature.parameters) == ["resolution"]


def test_the_replier_holds_no_catalogue() -> None:
    """``Slot`` names the schema's slots; everything else in that module is a menu.

    A rule saying "only interpolate catalogue names here" is obeyed until
    somebody wants a nicer word for one item. A module that cannot reach a
    catalogue cannot look one up to be nicer about, and this is what keeps it
    unable to.
    """
    source = Path(inspect.getfile(reply_module)).read_text()
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("chip_chat.catalog")
        for alias in node.names
    }
    assert imported == {"Slot"}


def test_no_reply_names_an_item_the_catalogue_does_not_publish(
    catalog: MenuCatalog,
) -> None:
    """Every row any of the four turns puts on the screen, over every answer.

    ``test_matcher.py`` sweeps the whole space the stage-4 schema permits and
    asserts no draft carries a fabricated SKU. This asks the same question of
    the layer that turns a draft into words, including the one path the matcher
    sweep does not reach -- the alternative, whose rows nothing in the frame
    resolved to.
    """
    published_ids = {item.item_id for item in catalog.menu_items}
    names = {item.name for item in catalog.menu_items}
    for resolution in _every_conclusion(catalog):
        answer = reply_for(resolution)
        for item in answer.items:
            assert item.item_id in published_ids
            assert item.name in names
            assert item.name in answer.text


def test_every_outcome_has_a_sentence(catalog: MenuCatalog) -> None:
    """`reply_for` is total, and each kind arrives from the outcome it belongs to."""
    kinds = {
        Outcome.RESOLVED: ReplyKind.PROPOSAL,
        Outcome.CLARIFY: ReplyKind.QUESTION,
        Outcome.NOT_ORDERABLE: ReplyKind.ALTERNATIVE,
        Outcome.SEVERAL_MEALS: ReplyKind.ONE_AT_A_TIME,
    }
    assert set(kinds) == set(Outcome)
    seen: set[Outcome] = set()
    for resolution in _every_conclusion(catalog):
        answer = reply_for(resolution)
        assert answer.kind is kinds[resolution.outcome]
        assert answer.text.endswith((".", "?"))
        seen.add(resolution.outcome)
    assert seen == set(Outcome)


def test_a_resolved_photograph_states_what_it_believes_it_saw(
    photograph: Photographed,
) -> None:
    """PRD V3 on the happy path. The card underneath it is issue #62's."""
    turn = photograph(A_MEAL)
    assert turn.reply.kind is ReplyKind.PROPOSAL
    assert turn.reply.text == (
        "It looks like a Chicken Bowl with White Rice and Black Beans — tell me "
        "if I've got any of that wrong before I put it in."
    )


def test_the_model_s_own_prose_is_not_in_the_reply(poke_bowl: Turn) -> None:
    """``notes`` is display-only, and this module is not one of its readers.

    It reaches whatever renders the turn from ``Description.notes`` directly. A
    reply that quoted it would make this the second reader of the one field
    nothing downstream may parse -- and would put unvalidated model prose inside
    a sentence whose other words are all catalogue rows.
    """
    assert poke_bowl.description.notes == "That looks like a poke bowl."
    assert "poke" not in poke_bowl.reply.text.lower()


# --- helpers -----------------------------------------------------------------


def _worded(count: int) -> str:
    """How :mod:`chip_chat.vision.reply` spells a small count."""
    return (
        "",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
    )[count]


def _sold_out(catalog: MenuCatalog) -> MenuCatalog:
    """The same catalogue with nothing available anywhere."""
    from dataclasses import replace

    return replace(
        catalog,
        item_prices=tuple(
            replace(price, is_available=False) for price in catalog.item_prices
        ),
    )


def _every_conclusion(catalog: MenuCatalog) -> Iterator[Resolution]:
    """One resolution for every shape stage 5 can conclude in.

    Not the exhaustive sweep of describable meals -- that is
    ``test_matcher.py``'s, and it asks a question about the matcher. This asks
    whether the sentence layer holds over every *kind* of conclusion, including
    the ones a single fixture would never reach.
    """
    matcher = MealMatcher(catalog)
    with photo_tool_call("uploads/2026-08-26/a-photograph.jpg"):
        for described in _every_answer():
            yield matcher.resolve(described, restaurant_id=REFERENCE_RESTAURANT)


def _every_answer() -> Iterator[Any]:
    """Stage-4 answers covering all four outcomes and every clarification reason."""
    from chip_chat.vision.describe import DescribedMeal, SlotValue

    def meal(**slots: Any) -> DescribedMeal:
        return DescribedMeal(
            is_chipotle_style=slots.pop("is_chipotle_style", True),
            meals_visible=slots.pop("meals_visible", 1),
            vessel=_value(slots.pop("vessel", ("bowl", 0.95))),
            protein=_value(slots.pop("protein", ("chicken", 0.95))),
            rice=_value(slots.pop("rice", ("white_rice", 0.95))),
            beans=_value(slots.pop("beans", ("black_beans", 0.95))),
            salsas=tuple(
                SlotValue(value=term, confidence=score)
                for term, score in slots.pop("salsas", ())
            ),
            toppings=tuple(
                SlotValue(value=term, confidence=score)
                for term, score in slots.pop("toppings", ())
            ),
        )

    yield meal()
    yield meal(salsas=[("fresh_tomato_salsa", 0.9)], toppings=[("cheese", 0.9)])
    yield meal(toppings=[("guacamole", 0.1)])
    yield meal(protein=None)
    yield meal(vessel=None, protein=None)
    yield meal(protein=("chicken", 0.2))
    yield meal(rice=("white_rice", 0.1), beans=None)
    yield meal(protein=("steak", 0.9))
    yield meal(is_chipotle_style=False)
    yield meal(is_chipotle_style=False, vessel=("burrito", 0.9))
    yield meal(is_chipotle_style=False, vessel=None, protein=None)
    yield meal(meals_visible=2)
    yield meal(meals_visible=6, is_chipotle_style=False)


def _value(slot: Sequence[Any] | None) -> Any:
    """A ``(term, confidence)`` pair as a slot value, or ``None`` for an absence."""
    from chip_chat.vision.describe import SlotValue

    if slot is None:
        return None
    term, confidence = slot
    return SlotValue(value=term, confidence=confidence)
