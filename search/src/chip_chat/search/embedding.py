"""One embedding deployment, read by both halves of integrated vectorization.

Integrated vectorization is two things that are usually discussed as one:

*index time*
    the documents' vectors are produced as they are ingested, rather than by a
    pipeline of ours that has to be kept in step with the index;

*query time*
    the application sends the index **text** and the service embeds it, so no
    caller anywhere holds an embedding model name.

The second is the one that has to be right, and it is the one that fails
silently. A query embedded by a different model — or by the same model at a
different ``dimensions`` — is not a worse query, it is a query in a different
vector space, and the index answers it with confident nonsense rather than with
an error. So :class:`EmbeddingDeployment` is a single object that produces
*both* the request the build sends to Azure OpenAI and the ``vectorizer``
declaration the index is created with. The two cannot disagree, because there is
one of them; ``test_embedding.py`` asserts the model name and the dimensions in
the vectorizer are the ones the build embeds with.

**Why the build embeds at all, on this estate.** The textbook shape is an
indexer with a skillset: Azure AI Search pulls the documents itself and the
``AzureOpenAIEmbedding`` skill vectorizes them, and nothing of ours touches an
embedding. That shape is unavailable here, and not for a reason anybody chose:

* the Free tier gives a search service no **outbound** managed identity, which
  is the only keyless way an indexer authenticates to a data source;
* ``stchipchat4cy39i`` is created with ``allowSharedKeyAccess = false``
  (``infra/terraform/storage.tf``), so there is no account key, no connection
  string and no account SAS to give it instead.

An indexer therefore cannot reach this corpus at all — not with a weaker
credential, with none. Both facts were read off the live estate rather than
assumed; ``docs/retrieval-index.md`` §3 records the commands. The alternatives
are Basic ($73.73/month, buys the service an identity) or re-enabling shared
keys on the storage account, which would put back a credential #8 deliberately
removed. Neither is worth it while the corpus is one 50 MB index, so the build
pushes documents and calls this deployment for their vectors, and the *query*
half of integrated vectorization — the half a caller could get wrong — is
configured on the index exactly as it would be otherwise.

========================================== ==================================
Variable                                   Meaning
========================================== ==================================
``CHIP_CHAT_FOUNDRY_ENDPOINT``             Account endpoint. The chat and
                                           vision lanes read the same one.
``CHIP_CHAT_FOUNDRY_EMBEDDING_DEPLOYMENT`` Deployment answering the knowledge
                                           lane.
``CHIP_CHAT_FOUNDRY_EMBEDDING_DIMENSIONS`` Optional. Shorten the vector; see
                                           :attr:`EmbeddingDeployment.dimensions`.
========================================== ==================================

The deployment name is configuration for the same reason
``chip_chat.agent.foundry``'s are: issue #8's *"deployment names are
configuration, not hardcoded, so they can be swapped for eval experiments
later"*. Swapping the embedding model is a rebuild rather than a restart — every
vector in the index came out of the old one — which is the one way this lane's
swap differs from the chat lane's, and is another thing the alias makes cheap.
"""

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from chip_chat.search.errors import SearchError

__all__ = [
    "COGNITIVE_SERVICES_SCOPE",
    "DEFAULT_API_VERSION",
    "DEFAULT_DEPLOYMENT",
    "DEFAULT_MODEL",
    "Embedder",
    "EmbeddingDeployment",
    "EmbeddingError",
    "HttpEmbedder",
    "TokenSource",
    "batched",
]

COGNITIVE_SERVICES_SCOPE: Final = "https://cognitiveservices.azure.com/.default"
"""Entra scope for the Foundry data plane. Not the management-plane scope."""

DEFAULT_MODEL: Final = "text-embedding-3-small"
"""The model, and the only one of its family with quota here.

Read on 2026-08-27 with ``az cognitiveservices usage list -l eastus2``:
``text-embedding-3-large`` reports a limit of **0** on GlobalStandard,
DataZoneStandard and Standard alike, and ``text-embedding-3-small`` reports
1,000 (thousand TPM) on GlobalStandard. So the choice was made by the
subscription, the same way #8's chat and vision models were. ``ada-002`` has
Standard quota and is the previous generation at a higher price per token; there
is no version of this decision in which it wins.
"""

DEFAULT_DEPLOYMENT: Final = DEFAULT_MODEL
"""Deployment name. A model name here, as in ``var.model_deployments``."""

DEFAULT_API_VERSION: Final = "2024-10-21"
"""Azure OpenAI data-plane version. The same GA pin ``chip_chat.agent.foundry``
uses, and pinned for the same reason: a silently newer API version is a silently
different response shape."""

_DEFAULT_DIMENSIONS: Final = 1536


class EmbeddingError(SearchError):
    """The embedding deployment is misconfigured or would not answer."""


@dataclass(frozen=True, slots=True)
class EmbeddingDeployment:
    """Where the vectors come from, at index time and at query time both."""

    endpoint: str
    """Foundry account endpoint, ``https://<name>.cognitiveservices.azure.com/``."""

    deployment: str = DEFAULT_DEPLOYMENT
    """The deployment name on that account."""

    model: str = DEFAULT_MODEL
    """The model behind the deployment.

    Named separately from :attr:`deployment` because the ``vectorizer`` needs
    both and they are not required to be equal — ``var.model_deployments`` keys
    a deployment by name and this repository happens to name them after their
    models.
    """

    dimensions: int = _DEFAULT_DIMENSIONS
    """Vector length. The model's native 1536 unless something says otherwise.

    ``text-embedding-3-small`` is trained so that a prefix of its output is
    itself a usable embedding, so this can be lowered to trade recall for space.
    The reason to think about it here is the Free tier's **50 MB**: the
    alias-swap pattern has a live index and a rebuilding one resident at once,
    so a vector is charged twice for the length of a build. At 1536 that is
    about 6 KB of raw vector per chunk per index.

    It is left at the native length anyway, because two settings on the index
    reclaim more than shortening would and neither costs recall:
    ``stored: false`` on the vector field, which drops the JSON copy kept for
    returning vectors to a caller that never asks for one, and scalar
    quantization to ``int8``. See :mod:`chip_chat.search.schema`.
    """

    api_version: str = DEFAULT_API_VERSION

    @property
    def embeddings_url(self) -> str:
        """The data-plane URL the build POSTs texts to."""
        base = self.endpoint.rstrip("/")
        return (
            f"{base}/openai/deployments/{self.deployment}/embeddings"
            f"?api-version={self.api_version}"
        )

    def request_body(self, texts: Sequence[str]) -> dict[str, Any]:
        """Return the embeddings request for ``texts``.

        Args:
            texts: The chunk texts, in the order their vectors are wanted.

        Returns:
            A JSON-ready request body.

        Raises:
            ValueError: If ``texts`` is empty. An embeddings call with no input
                is a billed round trip that returns nothing.
        """
        if not texts:
            raise ValueError("an embeddings request needs at least one text")
        return {"input": list(texts), "dimensions": self.dimensions}

    def vectorizer(self, name: str, api_key: str | None = None) -> dict[str, Any]:
        """Return the index's ``vectorizer`` declaration.

        This is the query-time half. With it on the index, a caller issues a
        vector query as ``{"kind": "text", "text": "..."}`` and the service
        embeds — so #49's retriever never holds a model name, and cannot drift
        from the one the documents were embedded with.

        Args:
            name: The name the vector profile refers to it by.
            api_key: A key on the Foundry account, or ``None`` to authenticate
                as the search service's own identity.

        Returns:
            A JSON-ready vectorizer definition.

        Raises:
            EmbeddingError: If ``api_key`` is ``None``. **On this estate that
                is a real failure rather than a fallback.** A vectorizer with no
                key runs as the search service's managed identity, and the Free
                tier does not give a search service one, so the index would be
                created and every query-time vectorization would then fail at
                the point of use. Refusing here turns that into a build-time
                error with a name. :func:`chip_chat.search.schema.index` takes
                the key as an explicit argument and will build a
                vectorizer-less index when told to, which is the honest way to
                say "not yet".
        """
        if api_key is None:
            raise EmbeddingError(
                "a vectorizer needs an API key on this estate: the Free search "
                "tier gives the service no managed identity, so a keyless "
                "vectorizer is accepted at creation and fails at every query. "
                "Put the Foundry key in Key Vault and pass it, or build the "
                "index without a vectorizer deliberately."
            )
        return {
            "name": name,
            "kind": "azureOpenAI",
            "azureOpenAIParameters": {
                "resourceUri": self.endpoint.rstrip("/"),
                "deploymentId": self.deployment,
                "modelName": self.model,
                "apiKey": api_key,
            },
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EmbeddingDeployment":
        """Read the deployment out of the environment.

        Args:
            env: The environment. Defaults to ``os.environ``.

        Returns:
            The deployment.

        Raises:
            EmbeddingError: If the endpoint is absent, or if the dimensions
                are set to something that is not a positive integer.
        """
        source = os.environ if env is None else env
        endpoint = source.get("CHIP_CHAT_FOUNDRY_ENDPOINT", "").strip()
        if not endpoint:
            raise EmbeddingError(
                "CHIP_CHAT_FOUNDRY_ENDPOINT is not set. It is the Foundry "
                "account endpoint; `make infra-output` prints it."
            )
        raw = source.get("CHIP_CHAT_FOUNDRY_EMBEDDING_DIMENSIONS", "").strip()
        if raw:
            try:
                dimensions = int(raw)
            except ValueError:
                raise EmbeddingError(
                    f"CHIP_CHAT_FOUNDRY_EMBEDDING_DIMENSIONS is {raw!r}, "
                    f"which is not a number"
                ) from None
            if dimensions <= 0:
                raise EmbeddingError(
                    f"CHIP_CHAT_FOUNDRY_EMBEDDING_DIMENSIONS is {dimensions}; "
                    f"a vector has a positive length"
                )
        else:
            dimensions = _DEFAULT_DIMENSIONS
        return cls(
            endpoint=endpoint,
            deployment=source.get(
                "CHIP_CHAT_FOUNDRY_EMBEDDING_DEPLOYMENT", DEFAULT_DEPLOYMENT
            ).strip()
            or DEFAULT_DEPLOYMENT,
            dimensions=dimensions,
            api_version=source.get(
                "CHIP_CHAT_FOUNDRY_EMBEDDING_API_VERSION", DEFAULT_API_VERSION
            ).strip()
            or DEFAULT_API_VERSION,
        )


class Embedder(Protocol):
    """Turns chunk texts into vectors.

    A protocol so the build can be tested end to end without a model call. A
    fake that returns fixed vectors is enough to assert everything the build
    does *around* the embedding, which is all of the alias behaviour.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, in the order given."""
        ...


class TokenSource(Protocol):
    """Returns a bearer token for a data plane."""

    def token(self) -> str:
        """Return a currently valid bearer token."""
        ...


class HttpEmbedder:
    """The real :class:`Embedder`, over the Azure OpenAI data plane."""

    def __init__(
        self,
        deployment: EmbeddingDeployment,
        client: Any,
        token: TokenSource,
    ) -> None:
        """Initialise the embedder.

        Args:
            deployment: Which deployment to call and at what length.
            client: An ``httpx.Client``. Injected rather than created so that
                one connection pool serves the whole build.
            token: Returns a bearer token for the Foundry data plane.
        """
        self._deployment = deployment
        self._client = client
        self._token = token

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text.

        Args:
            texts: The chunk texts.

        Returns:
            The vectors, in the order the texts were given — re-sorted by the
            response's own ``index`` rather than trusted to arrive in order,
            because a vector attached to the wrong chunk is the one error in
            this module that nothing downstream can detect.

        Raises:
            EmbeddingError: If the deployment refuses, or answers with a
                different number of vectors than it was given texts.
        """
        response = self._client.post(
            self._deployment.embeddings_url,
            json=self._deployment.request_body(texts),
            headers={"Authorization": f"Bearer {self._token.token()}"},
        )
        if response.status_code != 200:
            raise EmbeddingError(
                f"embeddings call returned {response.status_code}: {response.text[:400]}"
            )
        data = response.json().get("data", [])
        if len(data) != len(texts):
            raise EmbeddingError(f"asked for {len(texts)} vectors and got {len(data)}")
        ordered: list[list[float]] = [[] for _ in texts]
        for entry in data:
            ordered[int(entry["index"])] = [float(value) for value in entry["embedding"]]
        return ordered


def batched(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    """Yield ``items`` in slices of at most ``size``.

    Args:
        items: What to batch.
        size: The largest batch.

    Yields:
        Slices, in order.

    Raises:
        ValueError: If ``size`` is not positive.
    """
    if size <= 0:
        raise ValueError(f"batch size must be positive, got {size}")
    for start in range(0, len(items), size):
        yield items[start : start + size]
