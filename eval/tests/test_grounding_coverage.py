"""Can the shipped rows carry #75's claims? The check that runs before the run.

The clause worth reading twice is the first one. *Over-refusals: 0* over a set
holding no question the corpus can answer prints exactly like a system that
never hedges, and nothing in the arithmetic can tell the difference.
"""

from dataclasses import replace

from chip_chat.eval.grounding.coverage import CLAUSES, coverage
from chip_chat.eval.grounding.questions import Question


def test_the_shipped_rows_meet_every_scope_clause(asked: tuple[Question, ...]) -> None:
    """#75's scope, as something this repository is held to rather than intends."""
    cover = coverage(asked)

    assert cover.complete, [clause.name for clause, _ in cover.unmet]
    assert cover.rows == len(asked)
    assert cover.dietary >= 4


def test_every_clause_names_what_asks_for_it() -> None:
    """A clause with no source behind it is a rule nobody can retire."""
    assert all(clause.source for clause in CLAUSES)
    assert all(clause.minimum >= 1 for clause in CLAUSES)


def test_a_set_with_no_answerable_question_cannot_show_an_over_refusal(
    asked: tuple[Question, ...],
) -> None:
    """The failure the ticket calls out by name, caught before anything is run."""
    hedging = tuple(replace(question, answer_owed=False) for question in asked)

    cover = coverage(hedging)

    assert not cover.complete
    unmet = {clause.name for clause, _ in cover.unmet}
    assert "questions the published data answers -- over-refusal is observable" in unmet


def test_a_category_with_only_one_direction_is_half_a_gate(
    asked: tuple[Question, ...],
) -> None:
    """An allergen row the corpus answers and one it does not are two findings."""
    one_way = tuple(
        replace(question, refusal_owed=False) if question.dietary else question
        for question in asked
    )

    cover = coverage(one_way)

    assert not cover.complete
    assert any("ones it does not" in clause.name for clause, _ in cover.unmet)


def test_a_thin_category_is_reported_without_being_refused(
    asked: tuple[Question, ...],
) -> None:
    """It is held to counts, so a small denominator does not move its verdict.

    It does move what a reader should make of the counts, and the report says
    so above them rather than letting one row read as a category.
    """
    thin = tuple(
        replace(question, dietary=question.entry_id.endswith("k1-allergen-cheese"))
        for question in asked
    )

    cover = coverage(thin)

    assert cover.thin_category
    assert cover.dietary == 1
