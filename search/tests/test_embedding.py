"""One deployment, both ends of integrated vectorization."""

import pytest

from chip_chat.search import schema
from chip_chat.search.embedding import (
    DEFAULT_DEPLOYMENT,
    DEFAULT_MODEL,
    EmbeddingDeployment,
    EmbeddingError,
    batched,
)

ENDPOINT = "https://aif-example.cognitiveservices.azure.com/"
ENV = {
    "CHIP_CHAT_FOUNDRY_ENDPOINT": ENDPOINT,
    "CHIP_CHAT_FOUNDRY_EMBEDDING_DEPLOYMENT": "text-embedding-3-small",
}


def test_the_query_vectorizer_and_the_document_vectors_are_one_configuration() -> None:
    # The failure this exists to prevent is silent: a query embedded by a
    # different model is not a worse query, it is a query in a different vector
    # space, and the index answers it with confident nonsense.
    deployment = EmbeddingDeployment(endpoint=ENDPOINT, dimensions=512)
    vectorizer = deployment.vectorizer("v", "a-key")["azureOpenAIParameters"]
    assert vectorizer["deploymentId"] == deployment.deployment
    assert vectorizer["modelName"] == deployment.model
    assert deployment.request_body(["one"])["dimensions"] == 512


def test_the_index_declares_the_length_the_deployment_returns() -> None:
    deployment = EmbeddingDeployment(endpoint=ENDPOINT, dimensions=512)
    fields = schema.index("corpus-x", deployment, "a-key")["fields"]
    vector = next(f for f in fields if f["name"] == schema.VECTOR_FIELD)
    assert vector["dimensions"] == deployment.request_body(["one"])["dimensions"]


def test_the_embeddings_url_carries_the_deployment_and_the_pinned_version() -> None:
    deployment = EmbeddingDeployment(endpoint=ENDPOINT)
    assert deployment.embeddings_url == (
        f"{ENDPOINT}openai/deployments/{DEFAULT_DEPLOYMENT}/embeddings"
        f"?api-version={deployment.api_version}"
    ).replace(".azure.com//", ".azure.com/")


def test_an_empty_request_is_refused_before_it_is_billed() -> None:
    with pytest.raises(ValueError, match="at least one text"):
        EmbeddingDeployment(endpoint=ENDPOINT).request_body([])


def test_the_model_is_the_one_with_quota_in_this_subscription() -> None:
    # text-embedding-3-large reports a limit of 0 in eastus2 on every SKU.
    assert DEFAULT_MODEL == "text-embedding-3-small"


def test_reading_the_deployment_out_of_the_environment() -> None:
    deployment = EmbeddingDeployment.from_env(ENV)
    assert deployment.endpoint == ENDPOINT
    assert deployment.deployment == "text-embedding-3-small"
    assert deployment.dimensions == 1536


def test_a_missing_endpoint_fails_where_it_reads_as_a_missing_endpoint() -> None:
    with pytest.raises(EmbeddingError, match="CHIP_CHAT_FOUNDRY_ENDPOINT"):
        EmbeddingDeployment.from_env({})


def test_a_dimension_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(EmbeddingError, match="not a number"):
        EmbeddingDeployment.from_env(
            {**ENV, "CHIP_CHAT_FOUNDRY_EMBEDDING_DIMENSIONS": "half"}
        )


def test_a_vector_of_no_length_is_refused() -> None:
    with pytest.raises(EmbeddingError, match="positive length"):
        EmbeddingDeployment.from_env(
            {**ENV, "CHIP_CHAT_FOUNDRY_EMBEDDING_DIMENSIONS": "0"}
        )


def test_shortening_the_vector_is_one_variable() -> None:
    deployment = EmbeddingDeployment.from_env(
        {**ENV, "CHIP_CHAT_FOUNDRY_EMBEDDING_DIMENSIONS": "768"}
    )
    assert deployment.dimensions == 768


def test_batching_keeps_the_order_and_covers_everything() -> None:
    items = [str(number) for number in range(10)]
    assert [item for batch in batched(items, 4) for item in batch] == items


def test_a_batch_of_nothing_is_a_loop_that_never_ends() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        list(batched(["a"], 0))
