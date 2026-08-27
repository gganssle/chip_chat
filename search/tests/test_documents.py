"""What a chunk has to carry before it is allowed into the index."""

import pytest
from fakes import chunk

from chip_chat.search import chunks, schema
from chip_chat.search.documents import ACTION, DocumentError, document, documents


def test_a_chunk_becomes_an_upload() -> None:
    rendered = document(chunk("a" * 64))
    assert rendered[ACTION] == "upload"
    assert rendered[chunks.CHUNK_ID] == "a" * 64


def test_only_the_fields_the_row_publishes_are_written() -> None:
    # A policy section has no calorie count, and writing null for one would say
    # "zero calories" to anything that did not check.
    rendered = document(chunk("a" * 64, calories=None, allergens=None))
    assert chunks.CALORIES not in rendered
    assert chunks.ALLERGENS not in rendered


def test_calories_arrive_as_a_number_from_a_decimal_string() -> None:
    assert document(chunk("a" * 64, calories="630.00"))[chunks.CALORIES] == 630.0


def test_every_document_carries_a_resolvable_source_url() -> None:
    # #48's second acceptance criterion. The word is the issue's own.
    with pytest.raises(DocumentError, match="resolvable"):
        document(chunk("a" * 64, source_url="chipotle.com/nutrition"))


def test_a_document_with_no_source_url_is_refused() -> None:
    with pytest.raises(DocumentError, match="cannot be cited"):
        document(chunk("a" * 64, source_url=None))


def test_a_document_with_no_harvested_at_is_refused() -> None:
    with pytest.raises(DocumentError, match="cannot be cited"):
        document(chunk("a" * 64, harvested_at=None))


def test_a_chunk_the_source_published_no_heading_for_is_not_refused() -> None:
    # Every kind of chunk HAS a heading column and plenty of published sections
    # have no heading in them. Refusing those for tidiness would refuse a third
    # of the policy corpus. Found by the first live build, not by a test.
    rendered = document(chunk("a" * 64, heading=None))
    assert chunks.HEADING not in rendered


def test_a_timestamp_with_no_offset_is_refused() -> None:
    # The service would store it in UTC by guessing, and this timestamp is
    # rendered to a visitor beside a published allergen claim.
    with pytest.raises(DocumentError, match="no UTC"):
        document(chunk("a" * 64, harvested_at="2026-08-26T19:58:46"))


def test_a_timestamp_that_is_not_one_is_refused() -> None:
    with pytest.raises(DocumentError, match="not a timestamp"):
        document(chunk("a" * 64, harvested_at="last Tuesday"))


def test_a_field_the_schema_does_not_declare_stops_the_build() -> None:
    # How a rename gets halfway: the new name arrives, the old one is still in
    # the index, and every filter on it silently matches nothing.
    with pytest.raises(DocumentError, match="calorie_count"):
        document({**chunk("a" * 64), "calorie_count": 630})


def test_allergens_arrive_as_a_collection() -> None:
    assert document(chunk("a" * 64, allergens=["DAIRY", "SOY"]))[chunks.ALLERGENS] == [
        "DAIRY",
        "SOY",
    ]


def test_an_allergen_array_that_is_a_string_is_refused() -> None:
    with pytest.raises(DocumentError, match="not a list"):
        document(chunk("a" * 64, allergens="DAIRY"))


def test_a_citation_carries_its_own_source_and_clock() -> None:
    rendered = document(
        chunk(
            "a" * 64,
            kind="DOCUMENT_BLOCK",
            citations=[
                {
                    "source_url": "https://www.chipotle.com/allergens",
                    "harvested_at": "2026-08-20T10:00:00+00:00",
                }
            ],
        )
    )
    assert rendered[chunks.CITATIONS][0]["source_url"].startswith("https://")


def test_a_citation_pointing_nowhere_is_refused_too() -> None:
    with pytest.raises(DocumentError, match="resolvable"):
        document(
            chunk(
                "a" * 64,
                kind="DOCUMENT_BLOCK",
                citations=[{"source_url": "", "harvested_at": "2026-08-20T10:00:00Z"}],
            )
        )


def test_a_boolean_that_is_a_string_is_refused() -> None:
    with pytest.raises(DocumentError, match="not a boolean"):
        document(chunk("a" * 64, is_composed="true"))


def test_an_integer_that_would_round_is_refused() -> None:
    with pytest.raises(DocumentError, match="not an integer"):
        document(chunk("a" * 64, character_count=52.5))


def test_the_vector_lands_on_the_vector_field() -> None:
    rendered = document(chunk("a" * 64), [0.5, 0.25])
    assert rendered[schema.VECTOR_FIELD] == [0.5, 0.25]


def test_a_vector_short_of_a_chunk_is_refused() -> None:
    # A vector attached to the wrong chunk is the one error here that nothing
    # downstream can see.
    with pytest.raises(DocumentError, match="wrong chunk"):
        documents([chunk("a" * 64), chunk("b" * 64)], [[0.1]])
