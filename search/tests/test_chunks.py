"""The chunk schema as the index reads it."""

import pytest

from chip_chat.search import chunks


def test_every_field_has_an_azure_type() -> None:
    for entry in chunks.FIELDS:
        assert chunks.edm_type_of(entry)


def test_a_type_nobody_mapped_stops_the_build_rather_than_becoming_text() -> None:
    invented = chunks.ChunkField("weight", "MAP<STRING, INT>")
    with pytest.raises(KeyError, match="no Azure AI Search equivalent"):
        chunks.edm_type_of(invented)


def test_the_citation_fields_are_retrievable_not_merely_filterable() -> None:
    # #48's first scope bullet, in as many words. A citation the application
    # cannot read back is not a citation.
    assert chunks.SOURCE_URL in chunks.retrievable()
    assert chunks.HARVESTED_AT in chunks.retrievable()


def test_every_field_is_retrievable() -> None:
    assert chunks.retrievable() == chunks.names()


def test_the_comparative_questions_are_filters() -> None:
    # "fewer calories", "vegetarian", "without dairy" -- the three the issue
    # names. The first two are a number and a category; the third is the
    # allergen array.
    for name in (chunks.CALORIES, chunks.CATEGORY, chunks.ALLERGENS):
        assert chunks.field(name).filterable, name


def test_allergens_are_filterable_and_never_searchable() -> None:
    # A searchable allergen field scores the dairy items highest for "something
    # without dairy", because free-text has no idea the query negated it.
    assert chunks.field(chunks.ALLERGENS).filterable
    assert chunks.ALLERGENS not in chunks.SEARCHABLE
    assert chunks.ALLERGEN_DISCLOSURE not in chunks.SEARCHABLE


def test_the_proper_nouns_are_searchable() -> None:
    # RFC-001 section 08: keyword recall matters because item names are proper
    # nouns that embeddings handle poorly, and those live in these three.
    for name in (chunks.HEADING, chunks.ITEM_TYPE, chunks.PRIMARY_FILLING):
        assert name in chunks.SEARCHABLE, name


def test_facetable_is_a_subset_of_filterable() -> None:
    assert set(chunks.facetable()) <= set(chunks.filterable())


def test_every_searchable_field_is_a_string() -> None:
    for name in chunks.SEARCHABLE:
        assert chunks.edm_type_of(chunks.field(name)) == "Edm.String", name


def test_the_universal_fields_are_the_ones_no_kind_owns() -> None:
    universal = {entry.name for entry in chunks.FIELDS if entry.universal}
    assert chunks.CHUNK_ID in universal
    assert chunks.TEXT in universal
    assert chunks.SOURCE_URL in universal
    assert chunks.HARVESTED_AT in universal
    # Menu metadata is not universal: a policy section has no calorie count,
    # and null there means "this kind does not have one" rather than "zero".
    assert chunks.CALORIES not in universal


def test_field_names_are_unique() -> None:
    assert len(set(chunks.names())) == len(chunks.FIELDS)


def test_an_unknown_field_name_is_an_error() -> None:
    with pytest.raises(KeyError):
        chunks.field("nutrition")
