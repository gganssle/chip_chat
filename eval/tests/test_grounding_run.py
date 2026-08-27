"""The seam: what a source is, what a judge is, and what happens when one breaks.

One row's failure is one row's failure. And the seam #75's fourth acceptance
criterion depends on is not on the source at all -- it is that the scorer takes
two matched sequences and has never been told where either came from, which is
what lets an online runner over live traces produce a number that means the same
thing as this one.
"""

from chip_chat.agent.envelope import ClaimClass
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.grounding.evidence import read_evidence
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Turn, run_turns
from chip_chat.eval.grounding.scoring import score
from chip_chat.eval.grounding.testing import ScriptedSource, turn_spans
from chip_chat.eval.grounding.verdicts import Finding, Verdict

_PASSAGE = ({"id": "menu-chicken-bowl", "content": "Chicken bowl, 630 cal."},)


class _BrokenSource:
    """A source that raises on one row and answers the rest."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    @property
    def name(self) -> str:
        return "a source that fails on one row"

    @property
    def reports(self) -> frozenset[Signal]:
        return frozenset(Signal)

    def turn(self, question: Question) -> Turn:
        if question.entry_id == self._entry_id:
            raise RuntimeError("the deployment went away")
        return Turn(
            entry_id=question.entry_id,
            reply="an answer",
            citations=("menu-chicken-bowl",),
            claim_class=ClaimClass.FOOD.value,
            evidence=read_evidence(question.entry_id, turn_spans(_PASSAGE)),
            reports=self.reports,
        )


def test_a_source_that_raises_costs_one_row(asked: tuple[Question, ...]) -> None:
    """The error is recorded against the row and the run continues."""
    broken = asked[0].entry_id

    turns = run_turns(asked, _BrokenSource(broken))

    assert len(turns) == len(asked)
    assert turns[0].error == "RuntimeError: the deployment went away"
    assert all(turn.error is None for turn in turns[1:])


def test_an_outage_is_unscored_rather_than_an_uncited_claim(
    asked: tuple[Question, ...],
) -> None:
    """An outage is not a model being wrong, and the two must not share a column."""
    turns = run_turns(asked, _BrokenSource(asked[0].entry_id))
    scores = score(asked, turns)

    assert scores.errors == (asked[0].entry_id,)
    assert scores.judgements[0].verdicts[Finding.CITED] is Verdict.UNSCORED
    assert not scores.judgements[0].breached


def test_only_runs_the_rows_it_was_given(asked: tuple[Question, ...]) -> None:
    """Iterating on one row should not cost the other thirty-three."""
    turns = run_turns(asked, ScriptedSource(), only=[asked[0].entry_id])

    assert len(turns) == 1
    assert turns[0].entry_id == asked[0].entry_id


def test_a_source_that_reads_no_spans_says_so_rather_than_reporting_nothing(
    asked: tuple[Question, ...],
) -> None:
    """``evidence=None`` and *an empty retrieval* are two different findings.

    The first is a source that cannot see the trace; the second is a turn that
    answered without looking anything up. Only one of them is about the agent.
    """
    blind = Turn(entry_id=asked[0].entry_id, reply="an answer", evidence=None)
    looked = ScriptedSource(spans={asked[0].entry_id: turn_spans(searches=0)}).turn(
        asked[0]
    )

    assert blind.evidence is None
    assert looked.evidence is not None
    assert looked.evidence.readable
    assert not looked.evidence.retrieved
