"""The five findings, one at a time, and the difference between unscored and failed.

Every turn here is built by hand, because what is under test is the rules rather
than a deployment. The rule that costs the most to get wrong is not any of the
five: it is that a finding nobody could observe comes back ``unscored``, never
``pass``. A citation check that read a deployment's empty tuple as *"it cited
nothing"* would produce a report saying the agent never cites its sources, which
is a claim about wiring dressed as a claim about a model.
"""

from dataclasses import replace
from typing import Any

from chip_chat.agent.envelope import ClaimClass
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.grounding.evidence import read_evidence
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Turn
from chip_chat.eval.grounding.testing import ScriptedJudge, turn_spans
from chip_chat.eval.grounding.verdicts import Finding, Refusal, Verdict, assess

_ENTRY = "golden/k1-nutrition-calories"
_PASSAGE = ({"id": "menu-chicken-bowl", "content": "Chicken bowl, 630 cal."},)
_REPORTS_ALL = frozenset(Signal)
_REPORTS_NO_CITATIONS = frozenset({Signal.TOOLS, Signal.CARD, Signal.WRITES})


_QUESTION = Question(
    entry_id=_ENTRY,
    lane=Lane.KNOWLEDGE,
    answer_owed=True,
    citation_owed=True,
    message="how many calories are in a chicken bowl",
)

_TURN = Turn(
    entry_id=_ENTRY,
    reply="A chicken bowl is about 630 calories.",
    citations=("menu-chicken-bowl",),
    claim_class=ClaimClass.FOOD.value,
    evidence=read_evidence(_ENTRY, turn_spans(_PASSAGE)),
    reports=_REPORTS_ALL,
)


def _question(**overrides: Any) -> Question:
    """The plain knowledge row, with one thing about it changed."""
    return replace(_QUESTION, **overrides)


def _turn(**overrides: Any) -> Turn:
    """A cited, grounded answer to it, with one thing about it changed."""
    return replace(_TURN, **overrides)


# --- cited ------------------------------------------------------------------


def test_a_cited_food_claim_passes() -> None:
    """The shape the other citation tests are defined against."""
    assert assess(_question(), _turn()).verdicts[Finding.CITED] is Verdict.PASS


def test_an_uncited_food_claim_fails() -> None:
    """PRD K2's target is zero, and D9 made this a rule rather than a judgement."""
    judgement = assess(_question(), _turn(citations=()))

    assert judgement.verdicts[Finding.CITED] is Verdict.FAIL
    assert "PRD K2" in judgement.details[Finding.CITED]


def test_a_deployment_that_cannot_report_citations_leaves_it_unscored() -> None:
    """The one that matters. Unscored, never failed -- and it names the bead."""
    judgement = assess(_question(), _turn(citations=(), reports=_REPORTS_NO_CITATIONS))

    assert judgement.verdicts[Finding.CITED] is Verdict.UNSCORED
    assert "cc-bap" in judgement.details[Finding.CITED]


def test_an_account_claim_is_not_asked_for_a_citation() -> None:
    """*You have 1,250 points* is grounded in Snowflake; a source link is decoration."""
    judgement = assess(
        _question(answer_owed=False, citation_owed=False),
        _turn(citations=(), claim_class=ClaimClass.ACCOUNT.value),
    )

    assert judgement.verdicts[Finding.CITED] is Verdict.NOT_ASKED


def test_the_row_can_owe_a_citation_the_response_did_not_declare() -> None:
    """A claim made in prose without a class is still a claim the set asked for."""
    judgement = assess(
        _question(), _turn(citations=(), claim_class=ClaimClass.NONE.value)
    )

    assert judgement.verdicts[Finding.CITED] is Verdict.FAIL


# --- minted -----------------------------------------------------------------


def test_a_passage_the_retriever_never_returned_is_a_minted_source() -> None:
    """``dropped_citation_ids`` is what the envelope's docstring says #75 counts."""
    judgement = assess(_question(), _turn(dropped_citations=("menu-invented",)))

    assert judgement.verdicts[Finding.MINTED] is Verdict.FAIL
    assert judgement.minted_ids == ("menu-invented",)


def test_minted_is_unscored_where_citations_are_unreported() -> None:
    """A zero here would be the most flattering possible way to write *unmeasured*."""
    judgement = assess(
        _question(),
        _turn(dropped_citations=("menu-invented",), reports=_REPORTS_NO_CITATIONS),
    )

    assert judgement.verdicts[Finding.MINTED] is Verdict.UNSCORED
    assert judgement.minted_ids == ()


# --- supported --------------------------------------------------------------


def test_a_claim_on_a_turn_that_retrieved_nothing_is_unsupported() -> None:
    """The floor under groundedness, and the one finding a free run produces."""
    judgement = assess(
        _question(), _turn(evidence=read_evidence(_ENTRY, turn_spans(searches=0)))
    )

    assert judgement.verdicts[Finding.SUPPORTED] is Verdict.FAIL
    assert "made no retrieval at all" in judgement.details[Finding.SUPPORTED]


def test_an_outage_is_not_an_unsupported_claim() -> None:
    """RFC-001 §10's declining lane. Unscored, so nobody goes to read a prompt."""
    judgement = assess(
        _question(),
        _turn(evidence=read_evidence(_ENTRY, turn_spans(declined=True))),
    )

    assert judgement.verdicts[Finding.SUPPORTED] is Verdict.UNSCORED
    assert "outage" in judgement.details[Finding.SUPPORTED]


def test_a_source_that_reads_no_spans_leaves_support_unscored() -> None:
    """Not observable is not the same as nothing retrieved."""
    judgement = assess(_question(), _turn(evidence=None))

    assert judgement.verdicts[Finding.SUPPORTED] is Verdict.UNSCORED


def test_a_split_trace_leaves_support_unscored() -> None:
    """The passages exist and cannot be shown to belong to the answer. #103."""
    judgement = assess(
        _question(),
        _turn(evidence=read_evidence(_ENTRY, turn_spans(_PASSAGE, split=True))),
    )

    assert judgement.verdicts[Finding.SUPPORTED] is Verdict.UNSCORED
    assert "#103" in judgement.details[Finding.SUPPORTED]


# --- grounded ---------------------------------------------------------------


def test_groundedness_is_unscored_without_a_judge() -> None:
    """Three of the five need a model, and a keyword rule would measure itself."""
    judgement = assess(_question(), _turn())

    assert judgement.verdicts[Finding.GROUNDED] is Verdict.UNSCORED
    assert judgement.details[Finding.GROUNDED] == "no judge was supplied"


def test_a_judge_settles_groundedness() -> None:
    """And a judge that will not say leaves it unscored rather than passed."""
    supported = ScriptedJudge(groundings={_ENTRY: True})
    unsupported = ScriptedJudge(groundings={_ENTRY: False})

    assert (
        assess(_question(), _turn(), judge=supported).verdicts[Finding.GROUNDED]
        is Verdict.PASS
    )
    assert (
        assess(_question(), _turn(), judge=unsupported).verdicts[Finding.GROUNDED]
        is Verdict.FAIL
    )
    assert (
        assess(_question(), _turn(), judge=ScriptedJudge()).verdicts[Finding.GROUNDED]
        is Verdict.UNSCORED
    )


def test_a_judge_is_never_asked_without_the_retrieval() -> None:
    """#75: the judge scores against what the system really had.

    Handed the corpus instead, it would score a system that never opened it as
    grounded -- which is the exact failure the ticket puts the evidence on the
    ``retriever.search`` span to prevent.
    """
    judgement = assess(
        _question(), _turn(evidence=None), judge=ScriptedJudge(groundings={_ENTRY: True})
    )

    assert judgement.verdicts[Finding.GROUNDED] is Verdict.UNSCORED
    assert "nothing to score the claims against" in judgement.details[Finding.GROUNDED]


# --- refusal ----------------------------------------------------------------


def test_declining_a_question_the_corpus_answers_is_an_over_refusal() -> None:
    """The failure a scorer measuring only groundedness rewards."""
    judgement = assess(_question(), _turn(), judge=ScriptedJudge(refusals={_ENTRY: True}))

    assert judgement.refusal is Refusal.OVER_REFUSAL
    assert judgement.breached


def test_answering_a_question_it_does_not_is_an_under_refusal() -> None:
    """PRD section 10's launch-blocking one, on an allergen question."""
    judgement = assess(
        _question(answer_owed=False, refusal_owed=True, dietary=True),
        _turn(),
        judge=ScriptedJudge(refusals={_ENTRY: False}),
    )

    assert judgement.refusal is Refusal.UNDER_REFUSAL
    assert judgement.dietary


def test_both_directions_can_be_right() -> None:
    """Answering what is answerable, and declining what is not."""
    answered = assess(_question(), _turn(), judge=ScriptedJudge(refusals={_ENTRY: False}))
    declined = assess(
        _question(answer_owed=False, refusal_owed=True),
        _turn(),
        judge=ScriptedJudge(refusals={_ENTRY: True}),
    )

    assert answered.refusal is Refusal.CORRECT
    assert declined.refusal is Refusal.CORRECT


def test_a_row_stating_neither_direction_is_not_asked() -> None:
    """A rate whose denominator holds the set's silence is measuring the silence."""
    judgement = assess(
        _question(answer_owed=False, citation_owed=False),
        _turn(),
        judge=ScriptedJudge(refusals={_ENTRY: True}),
    )

    assert judgement.refusal is Refusal.NOT_ASKED


# --- the outage path --------------------------------------------------------


def test_a_row_that_never_ran_fails_nothing() -> None:
    """An outage is not a model being wrong, and it is in no numerator."""
    judgement = assess(
        _question(),
        Turn(entry_id=_ENTRY, error="RuntimeError: the deployment went away"),
        judge=ScriptedJudge(groundings={_ENTRY: False}, refusals={_ENTRY: True}),
    )

    assert not judgement.failed
    assert not judgement.breached
    assert judgement.verdicts[Finding.GROUNDED] is Verdict.UNSCORED
    assert judgement.refusal is Refusal.UNSCORED
