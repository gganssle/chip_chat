"""The index definition, which is a pure function and therefore checkable."""

from typing import Any

import pytest

from chip_chat.search import chunks, schema
from chip_chat.search.embedding import EmbeddingDeployment, EmbeddingError

DEPLOYMENT = EmbeddingDeployment(
    endpoint="https://aif-example.cognitiveservices.azure.com/"
)


def definition(key: str | None = "a-key") -> dict[str, Any]:
    return schema.index("corpus-20260826t195844z", DEPLOYMENT, key)


def field_named(name: str) -> dict[str, Any]:
    for entry in definition()["fields"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"no field called {name}")


# --- The fields --------------------------------------------------------------


def test_every_chunk_field_reaches_the_index() -> None:
    names = {entry["name"] for entry in definition()["fields"]}
    assert set(chunks.names()) <= names


def test_the_only_field_that_is_not_a_chunk_field_is_the_vector() -> None:
    names = {entry["name"] for entry in definition()["fields"]}
    assert names - set(chunks.names()) == {schema.VECTOR_FIELD}


def test_the_key_is_the_chunk_id_and_nothing_else_is() -> None:
    keys = [entry["name"] for entry in definition()["fields"] if entry.get("key") is True]
    assert keys == [chunks.CHUNK_ID]


def test_the_citation_fields_come_back_on_a_hit() -> None:
    assert field_named(chunks.SOURCE_URL)["retrievable"] is True
    assert field_named(chunks.HARVESTED_AT)["retrievable"] is True


def test_the_vector_is_searchable_and_neither_stored_nor_returned() -> None:
    vector = field_named(schema.VECTOR_FIELD)
    assert vector["searchable"] is True
    assert vector["retrievable"] is False
    # The 50 MB the Free tier allows is charged twice during a build, because
    # the live and the rebuilding index are resident together.
    assert vector["stored"] is False
    assert vector["dimensions"] == DEPLOYMENT.dimensions


def test_a_complex_collection_carries_no_flags_of_its_own() -> None:
    citations = field_named(chunks.CITATIONS)
    assert citations["type"] == "Collection(Edm.ComplexType)"
    assert set(citations) == {"name", "type", "fields"}
    assert [member["name"] for member in citations["fields"]] == [
        "source_url",
        "harvested_at",
    ]


def test_a_collection_is_never_sortable() -> None:
    assert "sortable" not in field_named(chunks.ALLERGENS)
    assert field_named(chunks.TEXT)["sortable"] is False


def test_calories_is_a_double_because_the_service_has_no_decimal() -> None:
    assert field_named(chunks.CALORIES)["type"] == "Edm.Double"
    assert field_named(chunks.CALORIES)["filterable"] is True


# --- Vector search -----------------------------------------------------------


def test_the_profile_names_the_algorithm_and_the_compression() -> None:
    profile = definition()["vectorSearch"]["profiles"][0]
    assert profile["algorithm"] == schema.HNSW_ALGORITHM
    assert profile["compression"] == schema.VECTOR_COMPRESSION


def test_compression_reranks_against_the_full_precision_vectors() -> None:
    # int8 is a quarter of the resident size; reranking is what stops that
    # being paid for in recall.
    compression = definition()["vectorSearch"]["compressions"][0]
    assert compression["kind"] == "scalarQuantization"
    assert compression["rerankWithOriginalVectors"] is True


def test_the_vectorizer_names_the_deployment_the_documents_were_embedded_with() -> None:
    vectorizer = definition()["vectorSearch"]["vectorizers"][0]
    parameters = vectorizer["azureOpenAIParameters"]
    assert parameters["deploymentId"] == DEPLOYMENT.deployment
    assert parameters["modelName"] == DEPLOYMENT.model
    assert definition()["vectorSearch"]["profiles"][0]["vectorizer"] == vectorizer["name"]


def test_without_a_key_there_is_no_vectorizer_at_all() -> None:
    # Not a keyless vectorizer, which the service accepts at creation and then
    # fails at every query, because the Free tier gives it no identity to be.
    section = definition(None)["vectorSearch"]
    assert "vectorizers" not in section
    assert "vectorizer" not in section["profiles"][0]


def test_a_keyless_vectorizer_is_refused_where_it_is_built() -> None:
    with pytest.raises(EmbeddingError, match="no managed identity"):
        DEPLOYMENT.vectorizer("v", None)


# --- Semantic and scoring ----------------------------------------------------


def test_every_semantic_field_is_searchable() -> None:
    # The service rejects a semantic configuration over a field it cannot read,
    # and the rejection arrives at index creation rather than at query time.
    prioritized = definition()["semantic"]["configurations"][0]["prioritizedFields"]
    referenced = [prioritized["titleField"]["fieldName"]]
    referenced += [
        entry["fieldName"]
        for group in ("prioritizedContentFields", "prioritizedKeywordsFields")
        for entry in prioritized[group]
    ]
    for name in referenced:
        assert name in chunks.SEARCHABLE, name


def test_the_scoring_profile_weights_only_searchable_fields() -> None:
    weights = definition()["scoringProfiles"][0]["text"]["weights"]
    assert set(weights) <= chunks.SEARCHABLE


def test_the_heading_outweighs_the_body() -> None:
    # RFC-001 section 08: item names are proper nouns, and a menu item's name
    # is its heading.
    weights = definition()["scoringProfiles"][0]["text"]["weights"]
    assert weights[chunks.HEADING] > weights[chunks.TEXT]


def test_the_scoring_profile_is_the_default() -> None:
    assert definition()["defaultScoringProfile"] == schema.SCORING_PROFILE


# --- Naming ------------------------------------------------------------------


def test_an_index_is_named_after_the_corpus_release_it_holds() -> None:
    assert schema.index_name("20260826T195844Z") == "corpus-20260826t195844z"


def test_the_name_reads_back_to_the_release() -> None:
    name = schema.index_name("20260826T195844Z")
    assert schema.run_id_of(name) == "20260826T195844Z"


def test_an_index_nobody_here_created_reads_back_as_none() -> None:
    assert schema.run_id_of("someone-elses-index") is None
    assert schema.run_id_of("corpus") is None
    assert schema.run_id_of("corpus-") is None


def test_a_run_id_that_could_not_be_a_name_is_refused() -> None:
    with pytest.raises(ValueError, match="legal index name"):
        schema.index_name("2026/08/26")


def test_an_empty_run_id_is_refused() -> None:
    with pytest.raises(ValueError, match="run_id is empty"):
        schema.index_name("")
