"""The whole harness at full size, over the corpus this repository commits.

The numbers these produce are not evidence about retrieval —
:mod:`chip_chat.eval.retrieval.testing` says exactly why, at length. What is
under test here is that forty questions go through the real
:class:`~chip_chat.search.retrieve.Retriever` under four real configurations and
come back as something the scorer can read, that the arms really are four
different queries, and that the report says out loud what it does not know.
"""

from collections.abc import Sequence

from chip_chat.eval.retrieval.configurations import (
    ABLATION,
    HYBRID,
    KEYWORD,
    RERANKED,
    SERVING,
    VECTOR,
    Configuration,
    semantic_requests,
)
from chip_chat.eval.retrieval.corpus import resolve
from chip_chat.eval.retrieval.questions import Question, RetrievalSet
from chip_chat.eval.retrieval.report import build_report, render
from chip_chat.eval.retrieval.run import Answer, RetrieverSource, run_sweep
from chip_chat.eval.retrieval.testing import EVALUATES_FILTERS, OfflineIndex
from chip_chat.search.corpus import ChunkSet
from chip_chat.search.errors import SearchError
from chip_chat.search.retrieve import Retrieval, Retriever


def sweep(
    questions: RetrievalSet, corpus: ChunkSet, only: Sequence[str] | None = None
) -> tuple[OfflineIndex, tuple[Answer, ...]]:
    """Run the whole ablation against an in-memory index over ``corpus``."""
    index = OfflineIndex(corpus)
    source = RetrieverSource(Retriever(index), name="offline")
    return index, run_sweep(questions, source, only=only)


def test_every_question_runs_under_every_arm(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    _, answers = sweep(retrieval_questions, corpus_fixture)
    assert len(answers) == len(retrieval_questions) * len(ABLATION)
    assert all(answer.answered for answer in answers)


def test_the_arms_are_four_different_queries(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # The ablation is only an ablation if the four arms reach the service
    # differently. Four labels on one query would be a table of one column.
    index, _ = sweep(retrieval_questions, corpus_fixture, only=["alg-cheese-dairy"])
    keyword, vector, hybrid, reranked = index.queries
    assert "vectorQueries" not in keyword
    assert "search" not in vector
    assert "search" in hybrid
    assert "vectorQueries" in hybrid
    assert hybrid["queryType"] == "simple"
    assert reranked["queryType"] == "semantic"


def test_the_run_is_question_major(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # So that an interrupted sweep leaves four comparable arms over the
    # questions that ran, rather than one complete arm and three empty ones.
    _, answers = sweep(
        retrieval_questions, corpus_fixture, only=["alg-cheese-dairy", "ord-catering"]
    )
    assert [a.question_id for a in answers[:4]] == ["alg-cheese-dairy"] * 4
    assert [a.arm.name for a in answers[:4]] == [arm.name for arm in ABLATION]


def test_one_question_s_failure_costs_only_that_question(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    class Broken:
        name = "broken"
        seen = 0

        def retrieve(self, question: Question, arm: Configuration) -> Retrieval:
            Broken.seen += 1
            if question.question_id == "alg-cheese-dairy":
                raise SearchError("the service refused")
            return Retriever(OfflineIndex(corpus_fixture)).search(
                question.text, rerank=arm.rerank, halves=arm.halves
            )

    answers = run_sweep(retrieval_questions, Broken())
    failed = [a for a in answers if not a.answered]
    assert {a.question_id for a in failed} == {"alg-cheese-dairy"}
    assert len(failed) == len(ABLATION)
    assert len(answers) == len(retrieval_questions) * len(ABLATION)


def test_a_sweep_of_the_serving_arm_alone_spends_a_quarter_as_much(
    retrieval_questions: RetrievalSet,
) -> None:
    # The allowance is a hard stop rather than an overage, so the useful moment
    # to know a sweep costs 40 of 1,000 is before the sweep.
    assert semantic_requests(ABLATION, len(retrieval_questions)) == len(
        retrieval_questions
    )
    assert semantic_requests((SERVING,), len(retrieval_questions)) == len(
        retrieval_questions
    )
    assert semantic_requests((KEYWORD, VECTOR, HYBRID), 40) == 0


def test_only_the_reranked_arm_costs_the_allowance() -> None:
    assert [arm for arm in ABLATION if arm.costs_allowance] == [RERANKED]
    assert SERVING is RERANKED


def test_the_serving_arm_is_the_one_the_product_sends() -> None:
    # If this ever stops being true the report's headline sentence is a lie, and
    # nothing else in the package would notice.
    assert SERVING.halves.value == "hybrid"
    assert SERVING.rerank is True


# --- the report --------------------------------------------------------------


def report_text(questions: RetrievalSet, corpus: ChunkSet) -> str:
    _, answers = sweep(questions, corpus)
    return render(
        build_report(
            questions,
            resolve(questions, corpus),
            answers,
            ABLATION,
            source="offline",
            measured=False,
            floor=1.5,
            evaluates_filters=EVALUATES_FILTERS,
        )
    )


def test_an_unmeasured_report_says_so_before_its_first_table(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    document = report_text(retrieval_questions, corpus_fixture)
    warning = document.index("were not measured against a retrieval service")
    assert warning < document.index("| Configuration |")


def test_an_unmeasured_report_does_not_print_a_breach_count(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # The offline index does not evaluate a filter, so a violation count taken
    # against it would be a count of that omission wearing a safety number's
    # clothes.
    document = report_text(retrieval_questions, corpus_fixture)
    assert "The breach column is unscored" in document


def test_the_report_names_the_labels_the_corpus_does_not_hold(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    document = report_text(retrieval_questions, corpus_fixture)
    assert "ing-barbacoa" in document
    assert "alg-caveat" in document


def test_the_report_names_the_corpus_release(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # Two reports taken against two harvests are not comparable, and a run id is
    # the only thing that says which is which.
    document = report_text(retrieval_questions, corpus_fixture)
    assert corpus_fixture.run_id in document


def test_the_report_prints_the_demo_bar_above_the_full_grid(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    document = report_text(retrieval_questions, corpus_fixture)
    assert document.index("## The demo bar") < document.index("## The ablation")


def test_the_report_prints_coverage_above_every_score(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    document = report_text(retrieval_questions, corpus_fixture)
    assert document.index("Is this the set the ticket asked for") < document.index(
        "## The demo bar"
    )


def test_the_report_records_the_unmeasured_floor(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    document = report_text(retrieval_questions, corpus_fixture)
    assert "Reranker floor: `1.5`" in document
