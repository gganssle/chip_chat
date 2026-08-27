"""The arithmetic: what averages, what counts, and what stays ``None``.

Two properties are load-bearing and both are about refusing to produce a number.
A gate with an unmeasured part is ``None`` rather than ``True``, because a gate
nobody measured has not passed. And the allergen and dietary category never
produces a percentage at all, because a rate over a safety property says how
often the promise held.
"""

from dataclasses import replace
from typing import Any

from chip_chat.agent.envelope import ClaimClass
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.grounding.evidence import read_evidence
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Turn
from chip_chat.eval.grounding.scoring import Category, score
from chip_chat.eval.grounding.testing import ScriptedJudge, turn_spans
from chip_chat.eval.grounding.verdicts import Finding

_PASSAGE = ({"id": "menu-chicken-bowl", "content": "Chicken bowl, 630 cal."},)
_ALL = frozenset(Signal)
_NO_CITATIONS = frozenset({Signal.TOOLS, Signal.CARD, Signal.WRITES})


def _question(entry_id: str, **overrides: Any) -> Question:
    base = Question(
        entry_id=entry_id,
        lane=Lane.KNOWLEDGE,
        answer_owed=True,
        citation_owed=True,
        message=f"a question about {entry_id}",
    )
    return replace(base, **overrides)


def _turn(entry_id: str, **overrides: Any) -> Turn:
    base = Turn(
        entry_id=entry_id,
        reply="an answer",
        citations=("menu-chicken-bowl",),
        claim_class=ClaimClass.FOOD.value,
        evidence=read_evidence(entry_id, turn_spans(_PASSAGE)),
        reports=_ALL,
    )
    return replace(base, **overrides)


def test_the_two_categories_are_both_kept_even_when_one_is_empty() -> None:
    """A category that lost its rows is an empty row, never an absence."""
    scores = score([_question("a")], [_turn("a")])

    assert [category.category for category in scores.categories] == list(Category)
    assert scores.category(Category.DIETARY).total == 0


def test_an_allergen_row_is_reported_in_its_own_category() -> None:
    """#75's second acceptance criterion, as the arithmetic."""
    scores = score(
        [_question("a"), _question("b", dietary=True)],
        [_turn("a"), _turn("b", citations=())],
    )

    assert scores.category(Category.DIETARY).uncited_claims == 1
    assert scores.category(Category.ORDINARY).uncited_claims == 0
    assert scores.uncited_claims == 1


def test_an_uncited_claim_is_a_count_and_never_a_rate() -> None:
    """PRD section 05 is explicit that zero means zero."""
    scores = score(
        [_question("a"), _question("b")], [_turn("a"), _turn("b", citations=())]
    )

    assert scores.uncited_claims == 1
    assert scores.citation_gate is False


def test_a_gate_nobody_measured_has_not_passed() -> None:
    """``None``, which is neither ``True`` nor ``False``, and the report says so."""
    scores = score([_question("a")], [_turn("a", citations=(), reports=_NO_CITATIONS)])

    assert scores.uncited_claims is None
    assert scores.citation_gate is None
    assert scores.dietary_gate is None


def test_the_dietary_gate_is_a_boolean_over_counts() -> None:
    """Every way this category can be wrong, at zero, or it does not pass."""
    judge = ScriptedJudge(groundings={"a": True, "b": True}, refusals={"a": False})
    clean = score([_question("a", dietary=True)], [_turn("a")], judge=judge)
    breached = score(
        [_question("b", dietary=True)], [_turn("b", citations=())], judge=judge
    )

    assert clean.dietary_gate is True
    assert breached.dietary_gate is False
    breaches = breached.category(Category.DIETARY).gate_breaches
    assert breaches is not None
    assert breaches["uncited claims"] == 1


def test_an_over_refusal_is_outside_the_dietary_gate_and_still_counted() -> None:
    """Hedging about an allergy is the safe mistake, and gating it would reward
    the unsafe one. It is reported at the same size as the other direction."""
    scores = score(
        [_question("a", dietary=True)],
        [_turn("a")],
        judge=ScriptedJudge(groundings={"a": True}, refusals={"a": True}),
    )

    assert scores.over_refusals == 1
    assert scores.category(Category.DIETARY).over_refusals == 1
    assert scores.dietary_gate is True


def test_an_under_refusal_breaches_the_dietary_gate() -> None:
    """Answering an allergen question the corpus does not support."""
    scores = score(
        [_question("a", dietary=True, answer_owed=False, refusal_owed=True)],
        [_turn("a")],
        judge=ScriptedJudge(groundings={"a": True}, refusals={"a": False}),
    )

    assert scores.under_refusals == 1
    assert scores.dietary_gate is False


def test_groundedness_is_a_rate_over_what_could_be_judged() -> None:
    """Three claims judged, one of them unsupported."""
    judge = ScriptedJudge(groundings={"a": True, "b": True, "c": False})
    scores = score(
        [_question(name) for name in "abc"],
        [_turn(name) for name in "abc"],
        judge=judge,
    )

    assert scores.groundedness == 2 / 3
    assert scores.meets_target is False
    assert scores.gap is not None
    assert scores.gap > 0


def test_groundedness_is_none_rather_than_zero_when_nothing_was_judged() -> None:
    """An unscored rate has no value, and a nought reads as everything wrong."""
    scores = score([_question("a")], [_turn("a")])

    assert scores.groundedness is None
    assert scores.meets_target is None


def test_a_row_with_no_turn_is_skipped_rather_than_failed() -> None:
    """It was not run. A partial run is a normal thing to score."""
    scores = score([_question("a"), _question("b")], [_turn("a")])

    assert scores.total == 1


def test_turns_are_matched_by_id_and_not_by_position() -> None:
    """A partial run scored positionally would score the wrong rows silently."""
    scores = score(
        [_question("a"), _question("b")], [_turn("b", citations=()), _turn("a")]
    )

    assert scores.judgements[0].question.entry_id == "a"
    assert scores.judgements[0].verdicts[Finding.CITED].value == "pass"
    assert scores.judgements[1].verdicts[Finding.CITED].value == "fail"


def test_an_outage_is_counted_apart_from_a_wrong_answer() -> None:
    """``errors`` is where it belongs, and no rate has it in a denominator."""
    scores = score(
        [_question("a"), _question("b")],
        [_turn("a"), Turn(entry_id="b", error="RuntimeError: gone")],
    )

    assert scores.errors == ("b",)
    assert scores.scored(Finding.SUPPORTED) == 1


def test_a_split_trace_is_counted_and_names_103() -> None:
    """The propagation counter, which means more here than it does to #74."""
    scores = score(
        [_question("a")],
        [_turn("a", evidence=read_evidence("a", turn_spans(_PASSAGE, split=True)))],
    )

    assert scores.split_traces == 1
    assert scores.unreadable_traces == 1
    assert scores.scored(Finding.SUPPORTED) == 0
