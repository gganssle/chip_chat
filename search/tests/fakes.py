"""A search service and an embedding deployment, in memory.

The fake is not a mock. It keeps indexes, documents and aliases in dicts and
enforces the four service behaviours the build depends on, so that a test which
passes against it is saying something about the build rather than about a
recording:

* an index name must be free before it can be created, and an index that does
  not exist cannot be written to;
* a document whose key is empty fails the **whole request** with an
  ``InvalidName`` error, and the documents beside it in that request do not
  land. That is not what the first draft of this fake did and it is not what the
  obvious reading of the API suggests -- it was measured on 2026-08-27, when
  ``make search-verify`` returned
  ``400 ... actions : 30: Document key cannot be missing or empty``. Documents
  in *earlier* requests are already in, which is how a build fails halfway;
* other rejections are reported per document, with HTTP 207 and a status per
  key, which is what :class:`~chip_chat.search.client.UploadError` carries.
  ``reject`` models that half;
* an alias write replaces the whole target list in one call and never leaves the
  alias unset;
* an alias may not point at an index that does not exist.

Everything the fake refuses, the real service refuses — ``make search-verify``
is the run that says so. What the fake deliberately does *not* model is the
propagation delay after an alias write, because a model of a delay would only
ever confirm the number that was typed into it.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from chip_chat.search.client import ServiceError, UploadError
from chip_chat.search.embedding import EmbeddingDeployment

__all__ = ["FakeEmbedder", "FakeSearchService", "chunk", "chunk_set"]


class FakeSearchService:
    """An in-memory :class:`~chip_chat.search.client.SearchService`."""

    def __init__(self, batch: int = 1000, reject: set[str] | None = None) -> None:
        """Initialise the service.

        Args:
            batch: Documents per upload request, so a test can produce a load
                that fails after some of the corpus is already in.
            reject: Keys the service reports as failed **per document**, which
                is the 207 half of its behaviour.
        """
        self.indexes: dict[str, Mapping[str, Any]] = {}
        self.docs: dict[str, dict[str, Mapping[str, Any]]] = {}
        self.aliases: dict[str, str] = {}
        self.calls: list[str] = []
        self.batch = max(1, batch)
        self.reject = reject or set()

    def index_names(self) -> list[str]:
        self.calls.append("index_names")
        return sorted(self.indexes)

    def create_index(self, definition: Mapping[str, Any]) -> None:
        name = str(definition["name"])
        self.calls.append(f"create_index:{name}")
        if name in self.indexes:
            raise ServiceError(f"index {name} already exists")
        self.indexes[name] = definition
        self.docs[name] = {}

    def delete_index(self, name: str) -> None:
        self.calls.append(f"delete_index:{name}")
        self.indexes.pop(name, None)
        self.docs.pop(name, None)

    def upload(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        self.calls.append(f"upload:{index}:{len(documents)}")
        if index not in self.indexes:
            raise ServiceError(f"no index called {index}")
        for start in range(0, len(documents), self.batch):
            request = documents[start : start + self.batch]
            for position, document in enumerate(request):
                if not str(document.get("chunk_id", "")):
                    raise ServiceError(
                        f"POST /indexes/{index}/docs/index returned 400: "
                        f"actions : {start + position}: Document key cannot be "
                        f"missing or empty."
                    )
            failures = [
                (str(document["chunk_id"]), "the service declined this document")
                for document in request
                if str(document["chunk_id"]) in self.reject
            ]
            for document in request:
                if str(document["chunk_id"]) not in self.reject:
                    self.docs[index][str(document["chunk_id"])] = document
            if failures:
                raise UploadError(failures)

    def document_count(self, index: str) -> int:
        self.calls.append(f"document_count:{index}")
        target = self.aliases.get(index, index)
        if target not in self.docs:
            raise ServiceError(f"no index called {target}")
        return len(self.docs[target])

    def alias_target(self, alias: str) -> str | None:
        self.calls.append(f"alias_target:{alias}")
        return self.aliases.get(alias)

    def set_alias(self, alias: str, index: str) -> None:
        self.calls.append(f"set_alias:{alias}:{index}")
        if index not in self.indexes:
            raise ServiceError(f"cannot point {alias} at {index}: it does not exist")
        self.aliases[alias] = index

    def delete_alias(self, alias: str) -> None:
        self.calls.append(f"delete_alias:{alias}")
        self.aliases.pop(alias, None)

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(f"search:{target}")
        index = self.aliases.get(target, target)
        if index not in self.docs:
            raise ServiceError(f"no index called {index}")
        hits = list(self.docs[index].values())
        return {
            "@odata.count": len(hits),
            "value": hits[: int(query.get("top", 50))],
        }


class FakeEmbedder:
    """Returns a deterministic vector per text, of the declared length.

    Deterministic because a test that asserts a vector reached the right
    document needs to be able to say which vector it was, and because a random
    one would make a failure unreproducible.
    """

    def __init__(self, deployment: EmbeddingDeployment) -> None:
        self._dimensions = deployment.dimensions
        self.batches: list[int] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [
            [float(len(text) % 7) + position / 1000.0] * self._dimensions
            for position, text in enumerate(texts)
        ]


def chunk(chunk_id: str, **overrides: Any) -> dict[str, Any]:
    """Return one plausible ``MENU_ITEM`` chunk row.

    Args:
        chunk_id: The chunk's identity.
        **overrides: Fields to replace or, with ``None``, to leave unset.

    Returns:
        A chunk row, as the gold export writes one.
    """
    row: dict[str, Any] = {
        "chunk_id": chunk_id,
        "kind": "MENU_ITEM",
        "text": f"Chicken Bowl {chunk_id}. 630 calories. Contains dairy.",
        "heading": "Chicken Bowl",
        "item_id": "CMG-101",
        "category": "Entree",
        "item_type": "Bowl",
        "primary_filling": "Chicken",
        "allergens": ["DAIRY"],
        "allergen_disclosure": "PUBLISHED",
        "calories": "630.00",
        "is_composed": True,
        "source_url": "https://www.chipotle.com/nutrition-calculator",
        "harvested_at": "2026-08-26T19:58:46+00:00",
        "character_count": 52,
        "chunked_at": "2026-08-26T21:00:00+00:00",
    }
    row.update(overrides)
    return {name: value for name, value in row.items() if value is not None}


def chunk_set(count: int, run_id: str = "20260826T195844Z") -> Any:
    """Return a :class:`~chip_chat.search.corpus.ChunkSet` of ``count`` chunks."""
    from chip_chat.search.corpus import ChunkSet

    return ChunkSet(
        run_id=run_id,
        rows=tuple(chunk(f"{position:064x}") for position in range(count)),
        origin="fake",
    )
