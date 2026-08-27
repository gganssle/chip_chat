"""The index is rebuilt, never patched, and one write makes the new one live.

RFC-001 §08 states the rule and ``docs/corpus-freshness.md`` already applies it
to the corpus: a run writes everything under its own prefix and, only if it
completed, writes one small pointer naming itself. This is the same rule at the
index. A build creates a **new** index named after the corpus release it is
loading, fills it, checks it, and only then points the alias at it. The
application knows the alias and has never been told an index name.

Four properties follow from the order of operations rather than from any error
handling, which is the point of writing it this way:

*a failed build cannot half-update the corpus*
    nothing it does touches the live index or the alias. The alias moves in one
    call, after every document is in and counted.

*a failed build cleans up after itself, which is not what the harvest does*
    ``docs/corpus-freshness.md`` leaves a failed harvest run on disk on purpose:
    *"a failure you can read is worth more than a failure you rolled back."* A
    blob store has no cap and a run under its own prefix costs nothing. Three
    indexes is a cap, and a partial index that outlives its build is the
    **newest index the alias is not pointing at** — which is precisely what
    :func:`rollback` would choose. So the same generosity that is free upstream
    is a correctness bug here, and the default is to delete the partial index
    and put the diagnosis in the error instead. ``--keep-failed`` buys the
    generosity back for the run where somebody actually wants to query the
    wreckage, and spends the third index on it knowingly.

*the previous index survives its own retirement*
    an alias change takes up to ten seconds to propagate through the service, so
    an in-flight query may still be reading the old index after the swap
    returns. Deleting it in the same build is the one way to turn an atomic swap
    into a 404, so the *next* build deletes it. That also leaves a rollback that
    is a single alias write rather than a rebuild — see :func:`rollback`.

*two indexes coexist, and only two*
    which is the whole reason :data:`INDEX_BUDGET` is a constant with a comment
    rather than a number. The Free tier allows three indexes on the service. The
    alias pattern spends two of them by construction — the live one and the one
    being built — and the third is headroom, for a ``--keep-failed`` run or a
    hand-built experiment. It is not a second feature.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from chip_chat.search import documents as documents_module
from chip_chat.search import schema
from chip_chat.search.chunks import TEXT
from chip_chat.search.client import SearchService
from chip_chat.search.corpus import ChunkSet
from chip_chat.search.embedding import Embedder, EmbeddingDeployment, batched
from chip_chat.search.errors import SearchError

__all__ = [
    "EMBED_BATCH",
    "INDEX_BUDGET",
    "SETTLE_SECONDS",
    "BuildError",
    "BuildReport",
    "build",
    "next_index_name",
    "retirable",
    "rollback",
    "statistics",
]

INDEX_BUDGET: Final = 3
"""How many indexes may exist on the service at once.

The Free tier's limit, and the reason this pattern was worth building now rather
than retrofitting. An alias swap needs the live index and the rebuilding one
resident together — two of three — so there is exactly one spare. Anything that
wants a second index for a second purpose does not fit, and would be asking for
the Basic tier rather than for a change here.
"""

SETTLE_SECONDS: Final = 60.0
"""How long to wait for a finished load to become countable.

See :func:`_settle`. Sixty seconds is far more than the lag measured on this
corpus, and the cost of being generous is that a genuinely short load takes a
minute to be reported rather than a second -- which is a build that was going to
fail either way.
"""

EMBED_BATCH: Final = 16
"""Chunks per embeddings call.

Small on purpose. The Free search tier has no bearing on this — the ceiling is
the Foundry deployment's tokens-per-minute, set to 10 (thousand) in
``var.model_deployments`` as a spend control rather than a performance setting.
Sixteen chunks of published prose sit inside that comfortably, and a build of
this corpus is a few hundred calls, which is seconds.
"""


class BuildError(SearchError):
    """A build could not complete. The alias has not moved."""


@dataclass(frozen=True, slots=True)
class BuildReport:
    """What one build did, whether or not it swapped.

    Attributes:
        run_id: The corpus release that was loaded.
        index: The index that was created.
        documents: How many chunks were indexed.
        alias: The alias.
        previous: What the alias pointed at before, or ``None`` on a first
            build.
        swapped: Whether the alias now points at :attr:`index`.
        retired: Indexes this build deleted before it started.
        vectorized: Whether the index carries a query-time vectorizer.
    """

    run_id: str
    index: str
    documents: int
    alias: str
    previous: str | None
    swapped: bool
    retired: tuple[str, ...] = ()
    vectorized: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        """Return the report as the lines ``make search-build`` prints."""
        lines = [
            f"corpus release {self.run_id}",
            f"  index        {self.index}",
            f"  documents    {self.documents}",
            f"  vectorizer   {'yes' if self.vectorized else 'NO, see the note'}",
            f"  alias        {self.alias} -> "
            f"{self.index if self.swapped else self.previous or '(none)'}",
            f"  previous     {self.previous or '(none)'}",
        ]
        if self.retired:
            lines.append(f"  retired      {', '.join(self.retired)}")
        lines.extend(f"  note         {note}" for note in self.notes)
        return "\n".join(lines)


def next_index_name(existing: Sequence[str], run_id: str, alias: str) -> str:
    """Return a free index name for ``run_id``.

    Normally ``<alias>-<run_id>``. A second build of the *same* release — which
    is what a schema change or a new embedding model produces, since neither
    re-harvests anything — takes ``-2``, ``-3`` and so on, so that the release
    stays readable off the front of the name and the ordinal only appears when
    it means something.

    Args:
        existing: Index names already on the service.
        run_id: The corpus release being built.
        alias: The alias these indexes serve.

    Returns:
        A name no existing index holds.

    Raises:
        ValueError: If ``run_id`` cannot be part of an index name.
    """
    base = schema.index_name(run_id, alias)
    taken = set(existing)
    if base not in taken:
        return base
    ordinal = 2
    while f"{base}-{ordinal}" in taken:
        ordinal += 1
    return f"{base}-{ordinal}"


def retirable(existing: Sequence[str], live: str | None, alias: str) -> tuple[str, ...]:
    """Return the indexes a build should delete before it creates its own.

    Every index this package built that the alias is **not** pointing at, oldest
    first. Two things it is not:

    It is not "everything but the newest". The live index is exempt whatever the
    budget says — deleting the index the application is reading is not a way to
    make room — and it is the live one, rather than the newest one, because a
    ``--no-swap`` build leaves a newer index that nothing is serving.

    It does not spare a rollback target, and does not need to: the index this
    build is about to replace is *live* right now, so it is exempt right now,
    and it becomes the rollback target the moment the swap demotes it. Sparing
    one more would keep a two-builds-ago index that nothing would ever choose,
    and would put the steady state at three of three with no room for a
    ``--keep-failed`` run.

    Args:
        existing: Index names on the service.
        live: What the alias points at, or ``None``.
        alias: The alias whose indexes these are.

    Returns:
        Names to delete, in the order to delete them.
    """
    ours = [
        name
        for name in existing
        if name != live and schema.run_id_of(name, alias) is not None
    ]
    return tuple(sorted(ours))


def _vectors(
    chunk_set: ChunkSet, embedder: Embedder, deployment: EmbeddingDeployment
) -> list[list[float]]:
    """Return one vector per chunk, in the chunk set's order."""
    texts = [str(row.get(TEXT, "")) for row in chunk_set.rows]
    if any(not text.strip() for text in texts):
        empty = sum(1 for text in texts if not text.strip())
        raise BuildError(
            f"{empty} of {len(texts)} chunks have no text. An empty chunk "
            f"embeds to a vector that is near every query and answers none of "
            f"them, so this is a gold-layer failure rather than something to "
            f"index around."
        )
    vectors: list[list[float]] = []
    for batch in batched(texts, EMBED_BATCH):
        vectors.extend(embedder.embed(batch))
    if len(vectors) != len(texts):
        raise BuildError(f"{len(texts)} chunks produced {len(vectors)} vectors")
    if any(len(vector) != deployment.dimensions for vector in vectors):
        wrong = next(
            len(vector) for vector in vectors if len(vector) != deployment.dimensions
        )
        raise BuildError(
            f"the deployment returned {wrong}-dimensional vectors and the index "
            f"declares {deployment.dimensions}. A query-time vectorizer would "
            f"produce the declared length, so this index would answer every "
            f"query in the wrong space."
        )
    return vectors


def build(
    service: SearchService,
    chunk_set: ChunkSet,
    deployment: EmbeddingDeployment,
    embedder: Embedder,
    *,
    alias: str = schema.ALIAS,
    vectorizer_key: str | None,
    swap: bool = True,
    keep_failed: bool = False,
    settle: float = SETTLE_SECONDS,
) -> BuildReport:
    """Build a new index from ``chunk_set`` and swap the alias onto it.

    Args:
        service: The search service.
        chunk_set: The chunks, and the release they came from.
        deployment: The embedding deployment.
        embedder: What turns chunk text into vectors.
        alias: The alias the application reads.
        vectorizer_key: A Foundry key for query-time vectorization, or ``None``
            to build an index without one. Passed through to
            :func:`chip_chat.search.schema.index`, which is where the argument
            for making it explicit lives.
        swap: Whether to point the alias at the finished index. ``False``
            builds and verifies without going live, which is what
            ``make search-build-only`` does before a risky change.
        keep_failed: Whether to leave a partial index behind when the load
            fails. Off by default; see the module docstring for why this is the
            one place the corpus's own "leave the failure on disk" rule is
            inverted.
        settle: How long to give the finished load to become countable. See
            :func:`_settle`; a fake service counts immediately, so a test that
            wants a short load reported passes zero rather than waiting.

    Returns:
        The report.

    Raises:
        BuildError: If the corpus is empty, or if the finished index does not
            hold the number of documents that went into it.
        chip_chat.search.errors.SearchError: If any call to the service or the
            embedding deployment fails. In every such case the alias has not
            moved and the live index is untouched.
    """
    if not chunk_set.rows:
        raise BuildError(
            f"release {chunk_set.run_id} has no chunks. An empty index would "
            f"swap into place looking exactly like a successful build and "
            f"answer every question with silence."
        )
    previous = service.alias_target(alias)
    existing = service.index_names()
    retired = retirable(existing, previous, alias)
    for name in retired:
        service.delete_index(name)
    remaining = [name for name in existing if name not in retired]
    if len(remaining) >= INDEX_BUDGET:
        raise BuildError(
            f"the service holds {len(remaining)} indexes and the tier allows "
            f"{INDEX_BUDGET}; a rebuild needs room for one more. Indexes not "
            f"named after this alias are not retired automatically: "
            f"{sorted(set(remaining) - {previous})}"
        )
    index_name = next_index_name(remaining, chunk_set.run_id, alias)

    definition = schema.index(index_name, deployment, vectorizer_key)
    # Embedding before creating the index, rather than after, so that a
    # deployment that is out of quota costs nothing but a refusal: an index
    # nothing can fill still has to be cleaned up, and on a three-index budget
    # that cleanup is not free.
    vectors = _vectors(chunk_set, embedder, deployment)
    payload = documents_module.documents(list(chunk_set.rows), vectors)
    service.create_index(definition)

    # Everything from here to the count is a failure that leaves an index
    # behind. `_abandon` is what stops that index becoming the newest thing the
    # alias is not pointing at, which is what `rollback` would choose.
    try:
        service.upload(index_name, payload)
        counted = _settle(service, index_name, len(chunk_set.rows), settle)
    except SearchError as error:
        raise _abandon(service, index_name, error, keep_failed) from error

    notes: list[str] = []
    if vectorizer_key is None:
        notes.append(
            "built with no query-time vectorizer: callers must embed their own "
            "queries, with the model and dimensions this index was built from"
        )
    if swap:
        service.set_alias(alias, index_name)
    else:
        notes.append(f"not swapped; {alias} still serves {previous or '(none)'}")
    return BuildReport(
        run_id=chunk_set.run_id,
        index=index_name,
        documents=counted,
        alias=alias,
        previous=previous,
        swapped=swap,
        retired=retired,
        vectorized=vectorizer_key is not None,
        notes=tuple(notes),
    )


def _settle(
    service: SearchService,
    index: str,
    expected: int,
    timeout: float = SETTLE_SECONDS,
    interval: float = 0.5,
) -> int:
    """Wait until ``index`` really holds ``expected`` documents, then say so.

    Azure AI Search acknowledges an indexing request when it has *accepted* the
    documents, not when they are queryable — indexing is near-real-time and not
    synchronous. Counting immediately after the last upload returns whatever has
    caught up, which on 2026-08-27 was 10 of 31 and read exactly like a load
    that had lost two thirds of the corpus. It was not: the same index answered
    31 a second later.

    That distinction matters more here than it would in most places, because the
    count is the gate in front of the alias write. A build that treated the lag
    as a short load would refuse to swap a corpus that was in fact complete, and
    a build that skipped the count would swap one that was not.

    Args:
        service: The search service.
        index: The index just loaded.
        expected: How many documents the release published.
        timeout: How long to give it before calling the load short.
        interval: Seconds between counts.

    Returns:
        The document count, equal to ``expected``.

    Raises:
        BuildError: If the count has still not reached ``expected`` when the
            timeout runs out. The alias has not moved.
    """
    deadline = time.monotonic() + timeout
    counted = service.document_count(index)
    while counted < expected and time.monotonic() < deadline:
        time.sleep(interval)
        counted = service.document_count(index)
    if counted != expected:
        raise BuildError(
            f"{index} holds {counted} documents and the release published "
            f"{expected}, after {timeout:.0f}s. The alias has not moved."
        )
    return counted


def _abandon(
    service: SearchService, index: str, error: SearchError, keep: bool
) -> SearchError:
    """Delete the index a failed build created, and say so in the error.

    Args:
        service: The search service.
        index: The index the build created.
        error: What went wrong.
        keep: Whether to leave the index in place instead.

    Returns:
        An error carrying both the original failure and what became of the
        index, to be raised from the original. The alias has not moved either
        way — this is about the three-index budget, not about the corpus.
    """
    if keep:
        return BuildError(
            f"{error}\n  the partial index {index} was kept (--keep-failed). "
            f"It is now the newest index the alias does not point at, so "
            f"`rollback` would choose it: delete it before rolling back."
        )
    try:
        service.delete_index(index)
    except SearchError as cleanup:
        return BuildError(
            f"{error}\n  and the partial index {index} could not be deleted: "
            f"{cleanup}. Delete it by hand before the next build, which needs "
            f"the room."
        )
    return BuildError(f"{error}\n  the partial index {index} was deleted.")


def rollback(service: SearchService, alias: str = schema.ALIAS) -> str:
    """Point ``alias`` back at the newest index it is not already serving.

    The reason the previous index is kept for a build rather than deleted at the
    swap. Undoing a bad corpus is then one write and about ten seconds, instead
    of a re-harvest and a rebuild.

    Args:
        service: The search service.
        alias: The alias to move.

    Returns:
        The index the alias now points at.

    Raises:
        BuildError: If there is nothing to roll back to.
    """
    live = service.alias_target(alias)
    candidates = sorted(
        (
            name
            for name in service.index_names()
            if name != live and schema.run_id_of(name, alias) is not None
        ),
        reverse=True,
    )
    if not candidates:
        raise BuildError(
            f"nothing to roll {alias} back to: the only index it could serve is "
            f"{live or '(none)'}"
        )
    service.set_alias(alias, candidates[0])
    return candidates[0]


def statistics(service: SearchService, alias: str = schema.ALIAS) -> Mapping[str, Any]:
    """Return what the service currently holds, for a report.

    Args:
        service: The search service.
        alias: The alias to describe.

    Returns:
        The live index, every index, and the budget.
    """
    live = service.alias_target(alias)
    names = service.index_names()
    return {
        "alias": alias,
        "live": live,
        "indexes": sorted(names),
        "budget": INDEX_BUDGET,
        "documents": None if live is None else service.document_count(live),
    }
