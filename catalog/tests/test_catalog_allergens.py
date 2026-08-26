"""That the three-valued allergen answer survives being consolidated.

This is the one file in the package whose failure could hurt somebody. The
harvest of issue #20 established that Chipotle's allergen control is titled
"I'm Avoiding" with the subheader "Tagged items contain your selection", so a
*tag means contains* and an *absent tag is not a published negative*. That
finding is not merely documented upstream, it is encoded in the type:
:class:`~chip_chat.harvest.AllergenStatus` has three members and no boolean
anywhere near it.

The job of the catalogue is not to get that right. It is to not undo it. A
consolidation is exactly where a correct three-state model gets flattened,
because a boolean has room for ``CONTAINS`` and one other thing — so
``NOT_LISTED`` and ``NOT_PUBLISHED`` merge and both come out reading as "does
not contain". The tests here are the three checks that would catch it: that
the catalogue still carries three distinguishable states, that ``AllergenStatus``
survives into it rather than being mapped away, and that ``NOT_LISTED`` and
``NOT_PUBLISHED`` never become the same value.

The failure they guard against is silent. A flattened catalogue gives
confident, well-typed, fully-tested answers that are wrong in the one
direction that matters, and the rest of this suite would stay green throughout.
"""

from catalog_fixtures import fixture_catalog

from chip_chat.catalog import AllergenDisclosure
from chip_chat.harvest.sources.chipotle import AllergenStatus


def test_all_three_states_are_present() -> None:
    """A catalogue that reached only two of them has collapsed one already."""
    catalog = fixture_catalog()
    states = {row.status for row in catalog.item_allergens}
    assert states == {
        AllergenStatus.CONTAINS,
        AllergenStatus.NOT_LISTED,
        AllergenStatus.NOT_PUBLISHED,
    }


def test_the_status_is_the_harvests_enum_and_not_a_string() -> None:
    """Mapped away to ``str`` or ``bool``, this is where it would show."""
    catalog = fixture_catalog()
    for row in catalog.item_allergens:
        assert isinstance(row.status, AllergenStatus)


def test_every_item_has_a_statement_about_every_allergen() -> None:
    """A join that misses is a silence, and a silence reads as reassurance."""
    catalog = fixture_catalog()
    items = {row.item_id for row in catalog.menu_items}
    codes = {row.allergen_code for row in catalog.allergens}
    stated = {(row.item_id, row.allergen_code) for row in catalog.item_allergens}
    assert stated == {(item_id, code) for item_id in items for code in codes}


def test_the_item_row_reconstructs_the_status_exactly() -> None:
    """The one assertion that proves the merge into ``menu_items`` lost nothing.

    ``menu_items.allergens`` is the ``CONTAINS`` set and
    ``menu_items.allergen_disclosure`` is the item-level half. Together they
    have to reproduce ``item_allergens.status`` for every pair, or the RFC's
    single ``allergens[]`` column has quietly turned two kinds of silence into
    one.
    """
    catalog = fixture_catalog()
    by_id = {row.item_id: row for row in catalog.menu_items}
    checked = 0
    for row in catalog.item_allergens:
        assert by_id[row.item_id].allergen_status(row.allergen_code) is row.status
        checked += 1
    assert checked == len(catalog.item_allergens)


def test_not_listed_and_not_published_stay_apart() -> None:
    """Cheese is not tagged for gluten; napkins are not tagged for anything.

    Those are different answers. The first is "Chipotle publishes marks for
    this food and gluten is not among them", which is not a claim that it is
    gluten free. The second is "Chipotle publishes nothing about this at all".
    A model that answers both with the same sentence is answering one of them
    wrongly.
    """
    catalog = fixture_catalog()
    by_id = {row.item_id: row for row in catalog.menu_items}
    cheese = by_id["CMG-5252"]
    napkins = by_id["CMG-6110"]

    assert cheese.allergen_disclosure is AllergenDisclosure.PUBLISHED
    assert cheese.allergen_status("dair") is AllergenStatus.CONTAINS
    assert cheese.allergen_status("glut") is AllergenStatus.NOT_LISTED

    assert napkins.allergen_disclosure is AllergenDisclosure.NOT_PUBLISHED
    assert napkins.allergen_status("dair") is AllergenStatus.NOT_PUBLISHED
    assert napkins.allergen_status("glut") is AllergenStatus.NOT_PUBLISHED

    assert cheese.allergen_status("glut") is not napkins.allergen_status("glut")


def test_an_unknown_code_is_not_published_rather_than_absent() -> None:
    """Asking about an allergen nobody publishes is not a negative answer.

    ``allergen_status`` has no way to say "no", and that is deliberate: the
    published data contains no negatives to report. A caller that wants one
    has to decide to invent it, in the open, rather than receive it from here.
    """
    catalog = fixture_catalog()
    cheese = {row.item_id: row for row in catalog.menu_items}["CMG-5252"]
    assert cheese.allergen_status("no-such-code") is AllergenStatus.NOT_LISTED
    napkins = {row.item_id: row for row in catalog.menu_items}["CMG-6110"]
    assert napkins.allergen_status("no-such-code") is AllergenStatus.NOT_PUBLISHED


def test_no_column_in_the_catalogue_is_a_nullable_boolean() -> None:
    """The shape check, made structurally rather than by reading the code.

    ``Optional[bool]`` is not a safe way to carry three states either, because
    the ``None`` has to be propagated as *unknown* all the way to the answer
    and one ``or False`` anywhere in the chain destroys it silently. The
    catalogue has no such column, and this fails the moment one is added.
    """
    import dataclasses
    import typing

    from chip_chat.catalog import records

    for name in records.TABLES:
        row_type = getattr(records, _class_of(name))
        hints = typing.get_type_hints(row_type)
        for field in dataclasses.fields(row_type):
            annotation = hints[field.name]
            options = typing.get_args(annotation)
            assert not (bool in options and type(None) in options), (
                f"{row_type.__name__}.{field.name} is a nullable boolean"
            )


def _class_of(table: str) -> str:
    """The record class name for a table name."""
    names = {
        "menu_items": "MenuItem",
        "item_prices": "ItemPrice",
        "modifiers": "Modifier",
        "stores": "Store",
        "item_allergens": "ItemAllergen",
        "allergens": "Allergen",
        "caveats": "Caveat",
        "vocabulary": "VocabularyTerm",
    }
    return names[table]
