"""One deployment, both ends of integrated vectorization."""

import pytest

from chip_chat.search import schema
from chip_chat.search.embedding import (
    DEFAULT_DEPLOYMENT,
    DEFAULT_MODEL,
    DEFAULT_RETRY_AFTER,
    MAXIMUM_RETRY_AFTER,
    EmbeddingDeployment,
    EmbeddingError,
    HttpEmbedder,
    batched,
    throttled_attempts,
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


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------
#
# The build embeds every chunk in the corpus and the corpus grew twelve-fold on
# GitHub #106 -- 31 chunks to 358 -- which is the first time this path met the
# S0 deployment's rate limit. It met it by dying: `429 RateLimitReached`, a new
# index created and then abandoned, the alias still serving the old one. The
# right *failure* semantics and the wrong outcome, because nothing was wrong.
#
# Retrying is safe here in a way it is not everywhere, and that is the whole
# argument for doing it at this layer: embedding is a pure function of the text,
# so a repeated batch produces the same vectors and costs one more call. There
# is no partial state, which is why these tests are about *when* it waits and
# *how long*, and there is nothing about reconciliation to test.


class _Response:
    """One answer from the deployment, with only what the embedder reads."""

    def __init__(
        self, status_code: int, *, headers: dict[str, str] | None = None, vectors: int = 0
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = "rate limited" if status_code != 200 else ""
        self._vectors = vectors

    def json(self) -> dict[str, object]:
        return {
            "data": [{"index": i, "embedding": [0.0, 1.0]} for i in range(self._vectors)]
        }


class _Client:
    """Answers a scripted sequence and records how many times it was asked."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.calls = 0

    def post(self, _url: str, **_kwargs: object) -> _Response:
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


class _Token:
    def token(self) -> str:
        return "a-token"


def _embedder(
    responses: list[_Response], waited: list[float], attempts: int = 6
) -> "HttpEmbedder":
    return HttpEmbedder(
        EmbeddingDeployment(endpoint=ENDPOINT),
        _Client(responses),
        _Token(),
        attempts=attempts,
        sleep=waited.append,
    )


def test_a_throttled_batch_is_offered_again_rather_than_abandoned() -> None:
    waited: list[float] = []
    embedder = _embedder(
        [
            _Response(429, headers={"Retry-After": "7"}),
            _Response(200, vectors=1),
        ],
        waited,
    )

    assert embedder.embed(["one"]) == [[0.0, 1.0]]
    assert waited == [7.0]


def test_it_waits_as_long_as_the_deployment_asked_for() -> None:
    # The account's own Retry-After has been observed at 54 seconds. Waiting a
    # fixed shorter interval would be a second refusal rather than a retry.
    waited: list[float] = []
    embedder = _embedder(
        [_Response(429, headers={"Retry-After": "54"}), _Response(200, vectors=1)],
        waited,
    )

    embedder.embed(["one"])

    assert waited == [54.0]


def test_a_refusal_with_no_header_still_waits_a_defensible_interval() -> None:
    waited: list[float] = []
    embedder = _embedder([_Response(429), _Response(200, vectors=1)], waited)

    embedder.embed(["one"])

    assert waited == [DEFAULT_RETRY_AFTER]


def test_an_absurd_retry_after_is_capped_rather_than_slept_through() -> None:
    waited: list[float] = []
    embedder = _embedder(
        [_Response(429, headers={"Retry-After": "3600"}), _Response(200, vectors=1)],
        waited,
    )

    embedder.embed(["one"])

    assert waited == [MAXIMUM_RETRY_AFTER]


def test_a_deployment_that_never_yields_raises_rather_than_looping() -> None:
    waited: list[float] = []
    embedder = _embedder([_Response(429)], waited, attempts=3)

    with pytest.raises(EmbeddingError, match="429"):
        embedder.embed(["one"])

    assert len(waited) == 2, "the last attempt is a call, not a wait"


def test_nothing_but_a_429_is_retried() -> None:
    # A 400 is a request this build will never get right by asking again, and a
    # 401 is a credential. Retrying either would turn a clear error into a slow
    # one.
    waited: list[float] = []
    embedder = _embedder([_Response(400)], waited)

    with pytest.raises(EmbeddingError, match="400"):
        embedder.embed(["one"])

    assert waited == []


def test_a_batch_that_succeeds_first_time_waits_for_nothing() -> None:
    waited: list[float] = []
    embedder = _embedder([_Response(200, vectors=2)], waited)

    assert len(embedder.embed(["one", "two"])) == 2
    assert waited == []


def test_no_attempts_is_a_build_that_reports_success_without_calling_anything() -> None:
    with pytest.raises(ValueError, match="attempts must be positive"):
        throttled_attempts(0)
