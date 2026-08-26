"""That the vision model's vocabulary comes out of the catalogue.

RFC-001 section 07 makes a structural claim: "every enum is generated from the
live catalogue at build time, so the model's vocabulary cannot drift from what
is orderable". The claim is only true if nothing in the pipeline holds a second
copy of the vocabulary — so these tests check both directions, that every term
traces back to a published row, and that a row appearing in the catalogue
appears in the vocabulary without anybody editing a list.
"""

from datetime import UTC, datetime

import pytest
from catalog_fixtures import fixture_catalog

from chip_chat.catalog import (
    Derivation,
    Modifier,
    Slot,
    VocabularyCollisionError,
    build_vocabulary,
    render_module,
    slot_of,
)
from chip_chat.catalog.vocabulary import member, slug

WHEN = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def modifier(name: str, *, group: str | None = None, kind: str = "Toppings") -> Modifier:
    """A modifier row, for exercising the classifier without a whole harvest."""
    placement = slot_of(modifier_type=kind, name=name)
    return Modifier(
        modifier_id=f"CMG-2:{name}",
        item_id="CMG-2",
        modifier_item_id=f"CMG-{abs(hash(name)) % 9000 + 1000}",
        name=name,
        slot=placement[0] if placement else None,
        derivation=placement[1] if placement else None,
        group_name=group,
        modifier_type=kind,
        min_quantity=None,
        max_quantity=None,
        is_default=False,
        delta_calories=None,
        portion_options=(),
        source_url="https://example.test/menu",
        harvested_at=WHEN,
        nutrition_source_url=None,
        nutrition_harvested_at=None,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("White Rice", "white_rice"),
        ("Tomatillo-Green Chili Salsa", "tomatillo_green_chili_salsa"),
        ("Queso Blanco", "queso_blanco"),
        ("Napkins & Utensils", "napkins_utensils"),
        ("Chicken al Pastor", "chicken_al_pastor"),
        ("Crème Fraîche", "creme_fraiche"),
    ],
)
def test_a_published_name_slugifies_the_same_way_every_time(
    name: str, expected: str
) -> None:
    """The value depends on the published name and on nothing else."""
    assert slug(name) == expected


def test_a_name_starting_with_a_digit_still_makes_an_identifier() -> None:
    """``3_POINTER`` is not an identifier, and renaming the term is not the fix."""
    assert member(slug("3 Pointer")) == "ITEM_3_POINTER"
    assert member(slug("White Rice")) == "WHITE_RICE"


def test_four_of_the_six_slots_are_a_published_column() -> None:
    """``itemType`` answers it for rice, beans, salsas and toppings alike.

    The content groups say the same thing less reliably: rice is a
    ``RiceContentGroup`` choice on a burrito and a ``ToppingsContentGroup``
    choice on a salad, and both rows are the same rice.
    """
    assert slot_of(modifier_type="Rice", name="White Rice") == (
        Slot.RICE,
        Derivation.MODIFIER_TYPE,
    )
    assert slot_of(modifier_type="Beans", name="Black Beans") == (
        Slot.BEANS,
        Derivation.MODIFIER_TYPE,
    )
    assert slot_of(modifier_type="Salsa", name="Fresh Tomato Salsa") == (
        Slot.SALSAS,
        Derivation.MODIFIER_TYPE,
    )
    assert slot_of(modifier_type="Toppings", name="Cheese") == (
        Slot.TOPPINGS,
        Derivation.MODIFIER_TYPE,
    )


def test_a_salsa_published_as_a_topping_is_split_out_by_name_and_says_so() -> None:
    """The one inference in the package, and it is labelled on every row.

    Chipotle's ``Salsa`` modifier type only appears on its build-your-own
    items; on an ordinary entree the same four salsas arrive as toppings, with
    nothing published separating them from cheese. RFC-001 section 07 gives
    salsas their own slot, so the split happens — visibly, as ``NAME_SUFFIX``,
    rather than silently.
    """
    for name in (
        "Fresh Tomato Salsa",
        "Tomatillo-Green Chili Salsa",
        "Roasted Chili-Corn Salsa",
    ):
        assert slot_of(modifier_type="Toppings", name=name) == (
            Slot.SALSAS,
            Derivation.NAME_SUFFIX,
        )
    assert slot_of(modifier_type="Toppings", name="Cheese") != (
        Slot.SALSAS,
        Derivation.NAME_SUFFIX,
    )


def test_a_modifier_with_no_slot_in_the_schema_gets_none() -> None:
    """A photograph does not say whether the chicken in a bowl was extra."""
    assert slot_of(modifier_type="ExtraPortion", name="Extra Chicken") is None
    assert slot_of(modifier_type="HalfPortion", name="Half Chicken") is None
    assert slot_of(modifier_type="Tortillas", name="Soft Flour Tortilla") is None
    assert slot_of(modifier_type="Beverage", name="Mexican Coca-Cola") is None


def test_every_term_traces_to_a_published_row() -> None:
    """The direction that keeps a term from being invented here."""
    catalog = fixture_catalog()
    items = {row.item_id for row in catalog.menu_items}
    names = {row.name for row in catalog.menu_items}
    types = {row.item_type for row in catalog.menu_items}
    fillings = {row.primary_filling for row in catalog.menu_items if row.primary_filling}
    for term in catalog.vocabulary:
        assert term.value == slug(term.name)
        if term.item_ids:
            assert set(term.item_ids) <= items
            assert term.name in names
        elif term.slot is Slot.VESSEL:
            assert term.name in types
        else:
            assert term.name in fillings


def test_every_orderable_entree_is_reachable_from_the_vocabulary() -> None:
    """The direction that keeps a published item from being unreachable.

    A vessel and a protein together name an entree. If the catalogue gains a
    Carne Asada Bowl, this passes on the next build without anybody editing a
    list — which is the whole of the "cannot drift" claim.
    """
    catalog = fixture_catalog()
    vessels = {term.name for term in catalog.vocabulary if term.slot is Slot.VESSEL}
    proteins = {term.name for term in catalog.vocabulary if term.slot is Slot.PROTEIN}
    entrees = [row for row in catalog.menu_items if row.category == "Entree"]
    assert entrees
    for entree in entrees:
        assert entree.item_type in vessels
        if entree.primary_filling is not None:
            assert entree.primary_filling in proteins


def test_the_same_ingredient_on_two_items_is_one_term() -> None:
    """White Rice is a modifier on every entree and one value in the enum."""
    catalog = fixture_catalog()
    rice = [term for term in catalog.vocabulary if term.slot is Slot.RICE]
    assert [term.value for term in rice] == sorted({term.value for term in rice})
    modifiers = [row for row in catalog.modifiers if row.slot is Slot.RICE]
    assert len(modifiers) > len(rice)


def test_two_foods_cannot_share_one_enum_member() -> None:
    """A collision is refused rather than resolved by dictionary order."""
    with pytest.raises(VocabularyCollisionError, match="cannot share"):
        build_vocabulary((), (modifier("Queso Blanco"), modifier("queso  blanco")))


def test_the_generated_module_is_valid_python() -> None:
    """The enum file is generated, and this is the check that it is a file."""
    catalog = fixture_catalog()
    source = render_module(catalog.vocabulary, catalog.content_version())
    namespace: dict[str, object] = {}
    exec(compile(source, "vision_vocabulary.py", "exec"), namespace)

    vessels = namespace["Vessel"]
    assert {member.value for member in vessels} == {  # type: ignore[attr-defined]
        term.value for term in catalog.vocabulary if term.slot is Slot.VESSEL
    }
    items = namespace["SLOT_ITEMS"]
    assert isinstance(items, dict)
    assert items["rice"]["white_rice"] == ("CMG-5001",)
    assert items["vessel"]["bowl"] == ()


def test_the_generated_module_records_the_catalogue_it_came_from() -> None:
    """A vocabulary whose catalogue has moved is detectable rather than trusted."""
    catalog = fixture_catalog()
    source = render_module(catalog.vocabulary, catalog.content_version())
    assert catalog.content_version() in source
    assert "DO NOT EDIT" in source


def test_the_schema_constrains_the_model_to_catalogue_values() -> None:
    """RFC-001 section 07's stage-4 schema, with the enums substituted in."""
    catalog = fixture_catalog()
    source = render_module(catalog.vocabulary, catalog.content_version())
    namespace: dict[str, object] = {}
    exec(compile(source, "vision_vocabulary.py", "exec"), namespace)

    schema = namespace["DESCRIBE_SCHEMA"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["vessel"]["properties"]["value"]["enum"] == [
        term.value for term in catalog.vocabulary if term.slot is Slot.VESSEL
    ]
    assert properties["toppings"]["type"] == "array"
    assert schema["additionalProperties"] is False
    assert properties["notes"] == {"type": "string"}


def test_an_empty_slot_still_renders_a_usable_module() -> None:
    """A slot the catalogue publishes nothing for must not break the build.

    The fixture site publishes no salsa, which is how this case is reached
    here; in production it is what a menu that dropped a whole slot would look
    like. An enum with no members is a vocabulary that admits nothing, which
    is the correct thing for a model to be handed.
    """
    source = render_module((), "0" * 64)
    namespace: dict[str, object] = {}
    exec(compile(source, "vision_vocabulary.py", "exec"), namespace)
    assert list(namespace["Salsa"]) == []  # type: ignore[call-overload]
