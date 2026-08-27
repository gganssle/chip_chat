"""The index is rebuilt, never patched — and what happens when a rebuild dies.

#48's third and fourth acceptance criteria are behaviour under failure, which
is what a live service is worst at demonstrating on demand. So they are asserted
here against ``fakes.FakeSearchService``, every run, for free;
``make search-verify`` then runs the same two shapes once against the real
service to show that the fake's model of it is right.
"""

import pytest
from fakes import FakeEmbedder, FakeSearchService, chunk, chunk_set

from chip_chat.search import schema
from chip_chat.search.build import (
    INDEX_BUDGET,
    BuildError,
    BuildReport,
    build,
    next_index_name,
    retirable,
    rollback,
    statistics,
)
from chip_chat.search.corpus import ChunkSet
from chip_chat.search.embedding import EmbeddingDeployment

DEPLOYMENT = EmbeddingDeployment(
    endpoint="https://aif-example.cognitiveservices.azure.com/", dimensions=8
)


def run(
    service: FakeSearchService,
    chunks: ChunkSet,
    *,
    swap: bool = True,
    keep_failed: bool = False,
) -> BuildReport:
    """Build ``chunks`` into ``service``, with a vectorizer and a fake model.

    ``settle=0`` because the fake counts what it holds the moment it holds it.
    The real service does not, which is what :func:`chip_chat.search.build._settle`
    is for -- and waiting sixty seconds here to model a lag the fake does not
    have would be a minute spent confirming a number that was typed in.
    """
    return build(
        service,
        chunks,
        DEPLOYMENT,
        FakeEmbedder(DEPLOYMENT),
        vectorizer_key="a-key",
        swap=swap,
        keep_failed=keep_failed,
        settle=0.0,
    )


# --- The happy path ----------------------------------------------------------


def test_a_build_creates_an_index_named_after_the_release_and_swaps_to_it() -> None:
    service = FakeSearchService()
    report = run(service, chunk_set(5))
    assert report.index == "corpus-20260826t195844z"
    assert service.aliases[schema.ALIAS] == report.index
    assert report.documents == 5
    assert report.previous is None


def test_the_application_only_ever_needs_the_alias() -> None:
    service = FakeSearchService()
    run(service, chunk_set(3))
    assert service.search(schema.ALIAS, {"top": 10})["@odata.count"] == 3


def test_nothing_is_created_until_every_chunk_has_a_vector() -> None:
    # An index nothing could fill still has to be cleaned up, and on a
    # three-index budget that cleanup is not free -- so the embedding call
    # comes first and a deployment out of quota costs a refusal and nothing
    # else. test_a_chunk_with_no_text_is_refused asserts the other half.
    service = FakeSearchService()
    run(service, chunk_set(2))
    created = service.calls.index("create_index:corpus-20260826t195844z")
    uploaded = service.calls.index("upload:corpus-20260826t195844z:2")
    assert created < uploaded


def test_a_second_build_of_the_same_release_takes_an_ordinal() -> None:
    # A schema change or a new embedding model rebuilds without re-harvesting.
    service = FakeSearchService()
    first = run(service, chunk_set(4))
    second = run(service, chunk_set(4))
    assert second.index == f"{first.index}-2"
    assert schema.run_id_of(second.index) is not None


def test_not_swapping_leaves_the_alias_where_it_was() -> None:
    service = FakeSearchService()
    live = run(service, chunk_set(4)).index
    report = run(service, chunk_set(6), swap=False)
    assert service.aliases[schema.ALIAS] == live
    assert report.swapped is False
    assert report.index in service.indexes


# --- #48.3, in the shape a fake can prove ------------------------------------


def test_the_alias_moves_in_one_call_after_everything_else() -> None:
    # "Atomic from the application's point of view" is a property of the order
    # of operations: there is exactly one alias write and it is last.
    service = FakeSearchService()
    run(service, chunk_set(4))
    service.calls.clear()
    run(service, chunk_set(6))
    swaps = [call for call in service.calls if call.startswith("set_alias:")]
    assert len(swaps) == 1
    assert service.calls[-1] == swaps[0]


def test_the_live_index_is_untouched_while_the_new_one_is_built() -> None:
    service = FakeSearchService()
    live = run(service, chunk_set(4)).index
    before = dict(service.docs[live])
    run(service, chunk_set(6))
    assert service.docs[live] == before


# --- #48.4, the deliberately failed build ------------------------------------


def poisoned(count: int) -> ChunkSet:
    """A corpus whose last chunk carries a key the service will reject."""
    rows = [chunk(f"{position:064x}") for position in range(count - 1)]
    rows.append(chunk(""))
    return ChunkSet(run_id="20260827T120000Z", rows=tuple(rows), origin="poisoned")


def test_a_failed_build_leaves_the_alias_on_the_previous_good_index() -> None:
    service = FakeSearchService()
    good = run(service, chunk_set(5)).index
    with pytest.raises(BuildError, match="Document key cannot be"):
        run(service, poisoned(4))
    assert service.aliases[schema.ALIAS] == good
    assert service.document_count(schema.ALIAS) == 5


def test_a_failed_build_takes_its_partial_index_with_it() -> None:
    # The one place the corpus's "leave the failure on disk" rule is inverted.
    # A blob store has no cap; three indexes is a cap, and a partial index that
    # survives is the newest index the alias is not pointing at -- which is
    # what rollback() would choose.
    service = FakeSearchService(batch=2)
    run(service, chunk_set(5))
    with pytest.raises(BuildError, match="was deleted"):
        run(service, poisoned(4))
    assert "corpus-20260827t120000z" not in service.indexes
    assert len(service.indexes) == 1


def test_keeping_the_wreckage_keeps_a_genuinely_half_loaded_index() -> None:
    # batch=2 so the first request lands and the second one dies, which is what
    # "a harvest that failed with two thirds of the corpus in" looks like.
    service = FakeSearchService(batch=2)
    run(service, chunk_set(5))
    with pytest.raises(BuildError, match="rollback` would choose it"):
        run(service, poisoned(4), keep_failed=True)
    remains = "corpus-20260827t120000z"
    assert remains in service.indexes
    assert len(service.docs[remains]) == 2


def test_a_per_document_rejection_fails_the_whole_build_too() -> None:
    # The service's other refusal shape: HTTP 207, most of the batch indexed,
    # a status per key. Most of a corpus is not a corpus, so the build treats it
    # exactly as it treats a 400.
    service = FakeSearchService()
    run(service, chunk_set(5))
    service.reject = {f"{2:064x}"}
    with pytest.raises(BuildError, match="were not indexed"):
        run(service, chunk_set(4, run_id="20260827T120000Z"))
    assert service.aliases[schema.ALIAS] == "corpus-20260826t195844z"


def test_a_rebuild_after_a_failure_reuses_the_name_the_failure_had() -> None:
    # Because the partial index is gone, the re-run of the same release is the
    # base name again rather than an ordinal -- there is nothing to disambiguate
    # it from.
    service = FakeSearchService()
    run(service, chunk_set(5))
    with pytest.raises(BuildError):
        run(service, poisoned(4))
    report = run(service, chunk_set(4, run_id="20260827T120000Z"))
    assert report.index == "corpus-20260827t120000z"


def test_a_short_load_never_reaches_the_alias() -> None:
    # The count check, rather than the upload, is what catches a service that
    # accepted every document and kept fewer.
    service = FakeSearchService()
    good = run(service, chunk_set(5)).index
    duplicated = chunk_set(4)
    same_key = ChunkSet(
        run_id="20260827T130000Z",
        rows=tuple(chunk("f" * 64) for _ in duplicated.rows),
        origin="four chunks that are all the same chunk",
    )
    with pytest.raises(BuildError, match="holds 1 documents"):
        run(service, same_key)
    assert service.aliases[schema.ALIAS] == good


class LaggingService(FakeSearchService):
    """Counts fewer documents than it holds, until it has been asked enough.

    Azure AI Search acknowledges an indexing request when it has accepted the
    documents, not when they are queryable. On 2026-08-27 a 31-chunk load in
    ten-document requests counted 10 immediately after the last one and 31 a
    second later -- which read exactly like a load that had lost two thirds of
    the corpus, and was not.
    """

    def __init__(self, lag: int) -> None:
        super().__init__()
        self.lag = lag

    def document_count(self, index: str) -> int:
        counted = super().document_count(index)
        if self.lag > 0:
            self.lag -= 1
            return counted // 2
        return counted


def test_a_load_that_has_not_caught_up_yet_is_waited_for() -> None:
    service = LaggingService(lag=2)
    report = build(
        service,
        chunk_set(6),
        DEPLOYMENT,
        FakeEmbedder(DEPLOYMENT),
        vectorizer_key="a-key",
        settle=5.0,
    )
    assert report.documents == 6
    assert service.aliases[schema.ALIAS] == report.index


def test_a_load_that_never_catches_up_never_reaches_the_alias() -> None:
    service = LaggingService(lag=1000)
    with pytest.raises(BuildError, match="holds 3 documents"):
        build(
            service,
            chunk_set(6),
            DEPLOYMENT,
            FakeEmbedder(DEPLOYMENT),
            vectorizer_key="a-key",
            settle=0.0,
        )
    assert schema.ALIAS not in service.aliases


def test_an_empty_corpus_is_refused_before_anything_is_created() -> None:
    service = FakeSearchService()
    with pytest.raises(BuildError, match="answer every question with silence"):
        run(service, ChunkSet(run_id="20260827T140000Z", rows=(), origin="nothing"))
    assert service.indexes == {}


def test_a_chunk_with_no_text_is_refused() -> None:
    service = FakeSearchService()
    empty = ChunkSet(
        run_id="20260827T150000Z",
        rows=(chunk("a" * 64), chunk("b" * 64, text="   ")),
        origin="one blank chunk",
    )
    with pytest.raises(BuildError, match="near every query"):
        run(service, empty)
    assert service.indexes == {}


def test_a_deployment_that_answers_in_the_wrong_dimension_is_caught() -> None:
    service = FakeSearchService()
    other = EmbeddingDeployment(endpoint=DEPLOYMENT.endpoint, dimensions=4)
    with pytest.raises(BuildError, match="wrong space"):
        build(
            service,
            chunk_set(3),
            DEPLOYMENT,
            FakeEmbedder(other),
            vectorizer_key="a-key",
        )


# --- The three-index budget --------------------------------------------------


def test_a_steady_state_holds_two_indexes() -> None:
    service = FakeSearchService()
    for day, count in enumerate((4, 5, 6, 7, 8), start=1):
        run(service, chunk_set(count, run_id=f"2026090{day}T090000Z"))
        assert len(service.indexes) <= INDEX_BUDGET
    assert len(service.indexes) == 2


def test_the_previous_index_survives_the_swap_that_retired_it() -> None:
    # An alias write takes up to ten seconds to propagate, so an in-flight
    # query may still be reading the old index when the swap returns.
    service = FakeSearchService()
    first = run(service, chunk_set(4, run_id="20260901T090000Z")).index
    run(service, chunk_set(5, run_id="20260902T090000Z"))
    assert first in service.indexes


def test_the_live_index_is_never_a_candidate_for_retirement() -> None:
    names = ["corpus-a", "corpus-b", "corpus-c"]
    assert "corpus-c" not in retirable(names, "corpus-c", "corpus")


def test_retirement_spares_the_live_index_and_nothing_else() -> None:
    # The rollback target is whatever the swap is about to demote, and that is
    # live at prune time. Sparing one more would keep an index nothing would
    # choose and put the steady state at three of three.
    names = ["corpus-a", "corpus-b", "corpus-c", "corpus-d"]
    assert retirable(names, "corpus-d", "corpus") == (
        "corpus-a",
        "corpus-b",
        "corpus-c",
    )


def test_an_index_nobody_here_created_is_never_retired() -> None:
    service = FakeSearchService()
    service.indexes["someone-elses-index"] = {"name": "someone-elses-index"}
    service.docs["someone-elses-index"] = {}
    run(service, chunk_set(3))
    assert "someone-elses-index" in service.indexes


def test_a_service_with_no_room_says_so_rather_than_deleting_something() -> None:
    service = FakeSearchService()
    for name in ("other-one", "other-two", "other-three"):
        service.indexes[name] = {"name": name}
        service.docs[name] = {}
    with pytest.raises(BuildError, match="allows 3"):
        run(service, chunk_set(3))


def test_next_index_name_walks_past_what_is_taken() -> None:
    base = schema.index_name("20260826T195844Z")
    assert next_index_name([base, f"{base}-2"], "20260826T195844Z", "corpus") == (
        f"{base}-3"
    )


# --- Rollback ----------------------------------------------------------------


def test_rollback_is_one_alias_write() -> None:
    service = FakeSearchService()
    first = run(service, chunk_set(4, run_id="20260901T090000Z")).index
    second = run(service, chunk_set(9, run_id="20260902T090000Z")).index
    assert service.aliases[schema.ALIAS] == second
    assert rollback(service) == first
    assert service.document_count(schema.ALIAS) == 4


def test_there_is_nothing_to_roll_back_to_after_a_first_build() -> None:
    service = FakeSearchService()
    run(service, chunk_set(4))
    with pytest.raises(BuildError, match="nothing to roll"):
        rollback(service)


def test_statistics_report_what_is_live() -> None:
    service = FakeSearchService()
    report = run(service, chunk_set(6))
    assert statistics(service) == {
        "alias": schema.ALIAS,
        "live": report.index,
        "indexes": [report.index],
        "budget": INDEX_BUDGET,
        "documents": 6,
    }


# --- The report --------------------------------------------------------------


def test_a_build_without_a_vectorizer_says_so_where_somebody_will_read_it() -> None:
    service = FakeSearchService()
    report = build(
        service,
        chunk_set(3),
        DEPLOYMENT,
        FakeEmbedder(DEPLOYMENT),
        vectorizer_key=None,
    )
    assert report.vectorized is False
    assert "embed their own queries" in report.render()
