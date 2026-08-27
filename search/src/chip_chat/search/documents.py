"""One chunk row becomes one index document, or the build stops.

The interesting content of this module is what it refuses. #48's second
acceptance criterion is that *every document carries a resolvable ``source_url``
and a ``harvested_at``* — not most, not the ones that happen to have them — and
the only place that can be made true is here, before the document is uploaded.
RFC-001 §08 is the reason it is worth being strict about: citations are part of
the payload rather than reconstructed afterwards, so a chunk that reaches the
index without a source is a passage the agent can quote and nobody can check.
The response envelope's D9 rule — the renderer drops any id the retriever did
not return — protects against a *minted* citation. Nothing downstream protects
against a real citation that points nowhere, so this does.

Three refusals, each of which has caught something real in a fixture:

* a field the chunk schema does not declare. Extra keys are how a rename gets
  halfway: the new name arrives, the old one is still in the index definition,
  and every filter on it silently matches nothing.
* a naive ``harvested_at``. Azure AI Search stores ``Edm.DateTimeOffset`` in
  UTC, and a timestamp with no offset is one the service will *guess* at.
  "How old is this answer" is rendered beside allergen claims without the
  visitor asking, so an hour of guessing is not free.
* a ``source_url`` that is not ``http`` or ``https``. "Resolvable" is the
  criterion's own word.

Everything else is coercion, and it is deliberately dull.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from chip_chat.search import chunks
from chip_chat.search.errors import SearchError
from chip_chat.search.schema import VECTOR_FIELD

__all__ = ["ACTION", "REQUIRED", "DocumentError", "document", "documents"]

ACTION: Final = "@search.action"
"""The per-document verb. Always ``upload`` here; see :func:`document`."""

_RESOLVABLE_SCHEMES: Final = ("http://", "https://")

REQUIRED: Final[tuple[str, ...]] = (
    chunks.CHUNK_ID,
    chunks.KIND,
    chunks.TEXT,
    chunks.SOURCE_URL,
    chunks.HARVESTED_AT,
)
"""The five fields a document may not arrive without.

Not the same list as ``ChunkField.universal``, and the difference is worth
stating because the obvious implementation gets it wrong. *Universal* is a claim
about the **table**: every kind of chunk has this column, so a query may select
it without knowing what it is reading. *Required* is a claim about the
**document**: this value has to be there or the chunk is not usable.

``heading`` is where the two part company. Every kind has one — a menu item's is
its name, a FAQ entry's is its question — and plenty of published sections have
none, which is a real fact about the source rather than a gap in the harvest.
Refusing those would refuse a third of the policy corpus for tidiness.

The five that are here each fail something concrete if absent: no ``chunk_id``
and the response envelope has nothing to cite (RFC-001 D9); no ``kind`` and the
retriever cannot weight; no ``text`` and there is nothing to embed; and
``source_url`` and ``harvested_at`` are #48's second acceptance criterion in as
many words.
"""


class DocumentError(SearchError):
    """A chunk cannot become an index document."""


def _timestamp(value: Any, *, field: str, chunk_id: str) -> str:
    """Return ``value`` as an offset-aware ISO 8601 string."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            raise DocumentError(
                f"chunk {chunk_id}: {field} is {value!r}, which is not a timestamp"
            ) from None
    if moment.tzinfo is None:
        raise DocumentError(
            f"chunk {chunk_id}: {field} is {value!r}, which carries no UTC "
            f"offset. The service would have to guess one, and this timestamp "
            f"is rendered to a visitor beside a published allergen claim."
        )
    return moment.isoformat()


def _number(value: Any, *, field: str, chunk_id: str) -> float:
    """Return ``value`` as a float, from a Decimal, a string or a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise DocumentError(
            f"chunk {chunk_id}: {field} is {value!r}, which is not a number"
        ) from None


def _integer(value: Any, *, field: str, chunk_id: str) -> int:
    """Return ``value`` as an int, refusing a float that would round."""
    if isinstance(value, bool):
        raise DocumentError(f"chunk {chunk_id}: {field} is a boolean, not an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise DocumentError(
            f"chunk {chunk_id}: {field} is {value!r}, which is not an integer"
        ) from None
    if isinstance(value, float) and number != value:
        raise DocumentError(f"chunk {chunk_id}: {field} is {value!r}, not an integer")
    return number


def _strings(value: Any, *, field: str, chunk_id: str) -> list[str]:
    """Return ``value`` as a list of strings."""
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise DocumentError(
            f"chunk {chunk_id}: {field} is {value!r}, which is not a list"
        )
    return [str(item) for item in value]


def _citations(value: Any, *, chunk_id: str) -> list[dict[str, Any]]:
    """Return the citation collection, one entry per source that published it."""
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise DocumentError(
            f"chunk {chunk_id}: citations is {value!r}, which is not a list"
        )
    rendered: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise DocumentError(
                f"chunk {chunk_id}: a citation is {entry!r}, which is not an object"
            )
        url = str(entry.get("source_url", ""))
        _resolvable(url, chunk_id=chunk_id, field="citations.source_url")
        rendered.append(
            {
                "source_url": url,
                "harvested_at": _timestamp(
                    entry.get("harvested_at"),
                    field="citations.harvested_at",
                    chunk_id=chunk_id,
                ),
            }
        )
    return rendered


def _resolvable(url: str, *, chunk_id: str, field: str) -> str:
    """Return ``url`` if something could fetch it, else raise."""
    if not url.startswith(_RESOLVABLE_SCHEMES):
        raise DocumentError(
            f"chunk {chunk_id}: {field} is {url!r}. #48 asks for a *resolvable* "
            f"source, and a citation nobody can open is the failure this "
            f"criterion exists to catch."
        )
    return url


def _value(entry: chunks.ChunkField, raw: Any, chunk_id: str) -> Any:
    """Return ``raw`` in the shape the index field expects."""
    edm = chunks.edm_type_of(entry)
    if entry.name == chunks.CITATIONS:
        return _citations(raw, chunk_id=chunk_id)
    if entry.name == chunks.SOURCE_URL:
        return _resolvable(str(raw), chunk_id=chunk_id, field=chunks.SOURCE_URL)
    if edm == "Edm.DateTimeOffset":
        return _timestamp(raw, field=entry.name, chunk_id=chunk_id)
    if edm == "Edm.Double":
        return _number(raw, field=entry.name, chunk_id=chunk_id)
    if edm == "Edm.Int32":
        return _integer(raw, field=entry.name, chunk_id=chunk_id)
    if edm == "Edm.Boolean":
        if not isinstance(raw, bool):
            raise DocumentError(
                f"chunk {chunk_id}: {entry.name} is {raw!r}, which is not a boolean"
            )
        return raw
    if edm == "Collection(Edm.String)":
        return _strings(raw, field=entry.name, chunk_id=chunk_id)
    return str(raw)


def document(
    chunk: Mapping[str, Any], vector: Sequence[float] | None = None
) -> dict[str, Any]:
    """Return the index document for one chunk row.

    The action is always ``upload`` rather than ``mergeOrUpload``. A build
    writes into an index it created moments earlier, so there is nothing to
    merge with, and ``upload`` is the verb that says a document is *whole*:
    every field the corpus publishes for this chunk is in this request, and a
    field absent here is absent because the corpus does not publish it. That is
    the same claim ``rebuilt, never patched`` makes at the level of the index,
    made one level down at the document, and it is why a partial re-load cannot
    leave a chunk carrying half of last week's metadata.

    Args:
        chunk: One row of the chunk table, as JSON. Fields the row's kind does
            not populate may be absent or ``None``; both mean the same thing
            and neither is written.
        vector: The embedding of the chunk's text, or ``None`` when the caller
            is about to fill it in.

    Returns:
        A JSON-ready document.

    Raises:
        DocumentError: If the row carries a field the chunk schema does not
            declare, is missing one every chunk must have, or holds a value
            that cannot be the type the index declares.
    """
    chunk_id = str(chunk.get(chunks.CHUNK_ID, "<unidentified>"))
    if unknown := sorted(set(chunk) - set(chunks.names())):
        raise DocumentError(
            f"chunk {chunk_id} carries {unknown}, which the chunk schema does "
            f"not declare. Either the gold table grew a column and "
            f"chip_chat.search.chunks.FIELDS did not, or a name was changed on "
            f"one side only."
        )
    rendered: dict[str, Any] = {ACTION: "upload"}
    for entry in chunks.FIELDS:
        raw = chunk.get(entry.name)
        if raw is None:
            if entry.name in REQUIRED:
                raise DocumentError(
                    f"chunk {chunk_id} has no {entry.name}, and a chunk without "
                    f"one cannot be cited, retrieved or read back. See "
                    f"chip_chat.search.documents.REQUIRED."
                )
            continue
        rendered[entry.name] = _value(entry, raw, chunk_id)
    if vector is not None:
        rendered[VECTOR_FIELD] = [float(value) for value in vector]
    return rendered


def documents(
    rows: Sequence[Mapping[str, Any]], vectors: Sequence[Sequence[float]] | None = None
) -> list[dict[str, Any]]:
    """Return index documents for ``rows``.

    Args:
        rows: Chunk rows.
        vectors: One vector per row, in the same order, or ``None``.

    Returns:
        The documents.

    Raises:
        DocumentError: If any row cannot become a document, or if the number of
            vectors does not match the number of rows.
    """
    if vectors is not None and len(vectors) != len(rows):
        raise DocumentError(
            f"{len(rows)} chunks and {len(vectors)} vectors: a vector attached "
            f"to the wrong chunk is an error nothing downstream can see"
        )
    return [
        document(row, None if vectors is None else vectors[position])
        for position, row in enumerate(rows)
    ]
