"""The week-one slice, answered and recorded, and what it honestly cannot report.

This is the adapter #75 ships with, and the property that matters most about it
is a negative one: it declares that it cannot report citations, so the citation
rule comes back unscored rather than failed on every row. A source that filled
in an empty tuple instead would produce a baseline saying the agent never cites
its sources, which is a claim about a missing caller (bead ``cc-bap``) dressed
as a claim about a model.
"""

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.golden.testing import RoutingOracle
from chip_chat.eval.grounding.questions import Question, questions
from chip_chat.eval.grounding.scoring import score
from chip_chat.eval.grounding.slice import SliceTurnSource
from chip_chat.eval.grounding.testing import ceiling
from chip_chat.eval.grounding.verdicts import Finding, Verdict


def _one(asked: tuple[Question, ...], entry_id: str) -> Question:
    for question in asked:
        if question.entry_id == entry_id:
            return question
    raise AssertionError(f"{entry_id} is not in the register")


def test_the_slice_declares_that_it_cannot_report_citations(
    golden: GoldenSet,
) -> None:
    """Four signals, and the missing one is the interesting one."""
    source = SliceTurnSource(golden=golden, model=RoutingOracle(golden))

    assert Signal.CITATIONS not in source.reports


def test_a_knowledge_turn_carries_the_passages_it_retrieved(
    golden: GoldenSet, asked: tuple[Question, ...]
) -> None:
    """The evidence comes off the trace, not off the loop's own return value."""
    source = SliceTurnSource(golden=golden, model=RoutingOracle(golden))

    turn = source.turn(_one(asked, "golden/k1-bowl-ingredients"))

    assert turn.error is None
    assert turn.evidence is not None
    assert turn.evidence.readable
    assert turn.evidence.searches == 1
    assert turn.evidence.retrieved


def test_a_row_with_no_case_in_the_set_is_an_error_rather_than_a_failure(
    golden: GoldenSet,
) -> None:
    """The join is by id, and a row that misses it says so."""
    source = SliceTurnSource(golden=golden, model=RoutingOracle(golden))

    turn = source.turn(Question(entry_id="golden/nothing-like-this", lane=Lane.NONE))

    assert turn.error is not None
    assert "no case in" in turn.error


def test_the_ceiling_leaves_every_citation_finding_unscored(
    golden: GoldenSet, shipped: Dataset
) -> None:
    """Nothing here is a score for the agent, and two findings are not scores at all."""
    rows = questions(shipped)

    scores = score(rows, ceiling(golden, shipped))

    assert scores.uncited_claims is None
    assert scores.minted_citations is None
    assert scores.citation_gate is None
    assert all(
        judgement.verdicts[Finding.CITED] is Verdict.UNSCORED
        for judgement in scores.judgements
    )


def test_the_ceiling_finds_claims_the_corpus_cannot_support(
    golden: GoldenSet, shipped: Dataset
) -> None:
    """The one number a free run produces, and it is about the wiring.

    The slice's menu is three hardcoded items with no published policy pages, so
    a policy question routed to the knowledge lane retrieves nothing. That is a
    property no prompt work can move, which is exactly what the ceiling is for.
    """
    rows = questions(shipped)

    scores = score(rows, ceiling(golden, shipped))

    assert scores.unsupported_claims is not None
    assert scores.unsupported_claims > 0
    failed = {j.question.entry_id for j in scores.failures()}
    assert "golden/k1-refund-policy" in failed
