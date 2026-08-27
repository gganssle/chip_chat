"""Conventions this package holds itself to."""

from chip_chat.search import chunks, schema
from chip_chat.search.client import SEARCH_SCOPE
from chip_chat.search.embedding import COGNITIVE_SERVICES_SCOPE


def test_the_two_data_planes_are_reached_with_different_scopes() -> None:
    # A token for one is a 401 at the other, and the two calls sit four lines
    # apart in the build.
    assert SEARCH_SCOPE != COGNITIVE_SERVICES_SCOPE
    assert SEARCH_SCOPE.endswith("/.default")
    assert COGNITIVE_SERVICES_SCOPE.endswith("/.default")


def test_the_api_version_is_a_preview_one_and_says_why() -> None:
    # Not by preference: every GA version this service answers returns 400 on
    # /aliases, and an alias is the whole rebuild-never-patch design. A
    # constant that breaks that convention has to carry the reason with it,
    # because the next person to read it will assume it was carelessness.
    assert schema.API_VERSION.endswith("-preview")


def test_the_alias_is_the_only_name_the_application_is_given() -> None:
    assert schema.ALIAS == "corpus"
    assert schema.index_name("20260101T000000Z") != schema.ALIAS


def test_the_vector_field_is_not_a_chunk_field() -> None:
    assert schema.VECTOR_FIELD not in chunks.names()
