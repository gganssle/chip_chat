"""#50's scope, as clauses, and the set held to them.

The scorer says how well the retriever did on the questions it was given. This
file is about whether those were the questions the ticket asked for -- a
question a recall figure cannot answer and cannot even see.
"""

from dataclasses import replace

from chip_chat.eval.retrieval.coverage import (
    MINIMUM_QUESTIONS,
    REQUIREMENTS,
    coverage,
)
from chip_chat.eval.retrieval.questions import Category, RetrievalSet


def test_the_shipped_set_meets_every_clause(
    retrieval_questions: RetrievalSet,
) -> None:
    cover = coverage(retrieval_questions)
    assert cover.complete, [requirement.name for requirement, _ in cover.unmet]


def test_the_shipped_set_is_large_enough_for_a_rate_to_move_slowly(
    retrieval_questions: RetrievalSet,
) -> None:
    assert len(retrieval_questions) >= MINIMUM_QUESTIONS


def test_every_requirement_names_the_document_that_asks_for_it() -> None:
    # A requirement whose reason is in another document is one somebody will
    # otherwise delete as arbitrary.
    for requirement in REQUIREMENTS:
        assert requirement.source


def test_an_empty_set_meets_nothing_and_does_not_raise() -> None:
    # An incomplete set is a fact to report beside the numbers rather than a
    # reason to refuse to compute them.
    cover = coverage(RetrievalSet(questions=()))
    assert cover.met == ()
    assert len(cover.unmet) == len(REQUIREMENTS)
    assert not cover.complete


def test_dropping_the_allergen_questions_fails_the_demo_bar_clause(
    retrieval_questions: RetrievalSet,
) -> None:
    # The clause exists because the demo criterion is computed over that
    # category alone, so a set that met the overall count with two allergen
    # questions would produce a headline that moves in halves.
    thinned = replace(
        retrieval_questions,
        questions=tuple(
            q
            for q in retrieval_questions
            if q.category is not Category.ALLERGENS or not q.answerable
        ),
    )
    unmet = {requirement.name for requirement, _ in coverage(thinned).unmet}
    assert "answerable allergen questions" in unmet


def test_dropping_the_multi_place_questions_fails_its_clause(
    retrieval_questions: RetrievalSet,
) -> None:
    # Without them recall@3 and hit@3 are the same column twice, and the reason
    # for counting recall over labels at all disappears.
    thinned = replace(
        retrieval_questions,
        questions=tuple(q for q in retrieval_questions if len(q.relevant) <= 1),
    )
    unmet = {requirement.name for requirement, _ in coverage(thinned).unmet}
    assert "questions answered in more than one published place" in unmet


def test_a_set_of_only_menu_questions_fails_the_other_side_of_the_ablation(
    retrieval_questions: RetrievalSet,
) -> None:
    # The ablation needs both kinds of question or its three retrieval arms have
    # nothing to disagree about.
    thinned = replace(
        retrieval_questions,
        questions=tuple(q for q in retrieval_questions if q.under_menu_item),
    )
    unmet = {requirement.name for requirement, _ in coverage(thinned).unmet}
    assert "questions whose answer is in a published document" in unmet
