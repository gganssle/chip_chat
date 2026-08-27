"""The document: what a reader must not be able to get wrong.

Three of the five findings are unmeasured on every run this repository can make,
and the whole risk of this report is that somebody quotes a blank as a result or
a fixture's ceiling as an agent's groundedness. Every test here is about a
sentence that has to be present for that not to happen.
"""

from dataclasses import replace

from chip_chat.agent.envelope import ClaimClass
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.grounding.evidence import read_evidence
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.report import build_report, render
from chip_chat.eval.grounding.run import Judge, Turn
from chip_chat.eval.grounding.testing import CEILING_CAVEAT, ScriptedJudge, turn_spans

_PASSAGE = ({"id": "menu-chicken-bowl", "content": "Chicken bowl, 630 cal."},)
_ALL = frozenset(Signal)
_NO_CITATIONS = frozenset({Signal.TOOLS, Signal.CARD, Signal.WRITES})

_QUESTION = Question(
    entry_id="golden/k1-nutrition-calories",
    lane=Lane.KNOWLEDGE,
    answer_owed=True,
    citation_owed=True,
    message="how many calories are in a chicken bowl",
    why="A number off the published nutrition data.",
)
_TURN = Turn(
    entry_id="golden/k1-nutrition-calories",
    reply="About 630 calories.",
    citations=("menu-chicken-bowl",),
    claim_class=ClaimClass.FOOD.value,
    evidence=read_evidence("golden/k1-nutrition-calories", turn_spans(_PASSAGE)),
    reports=_ALL,
)


def _render(
    *pairs: tuple[Question, Turn],
    judge: Judge | None = None,
    caveat: str = "",
) -> str:
    report = build_report(
        [question for question, _ in pairs],
        [turn for _, turn in pairs],
        source="a fixture",
        dataset="cilantro-golden-set",
        version="abcdef123456",
        judge=judge,
        caveat=caveat,
    )
    return render(report)


def test_the_document_names_the_dataset_version() -> None:
    """Two scores are comparable only if this string is the same."""
    assert "abcdef123456" in _render((_QUESTION, _TURN))


def test_an_unmeasured_gate_says_so_rather_than_passing() -> None:
    """The sentence that stops a blank being read as a clean bill of health."""
    document = _render((_QUESTION, replace(_TURN, reports=_NO_CITATIONS)))

    assert "Citation gate: unmeasured." in document
    assert "A gate nobody measured has not passed." in document


def test_what_could_not_be_measured_comes_before_what_could() -> None:
    """A reader who meets the table first reads a missing wire as a bad model."""
    document = _render((_QUESTION, replace(_TURN, reports=_NO_CITATIONS)))

    assert document.index("could not measure") < document.index("The two metrics")
    assert "cc-bap" in document


def test_a_rate_that_does_not_exist_prints_as_an_em_dash() -> None:
    """Never zero. A nought in that cell reads as everything wrong."""
    document = _render((_QUESTION, _TURN))

    assert "| **Groundedness** | **--** |" in document


def test_both_refusal_directions_are_in_the_same_table() -> None:
    """Putting over-refusal in a footnote would be the hedging system's report."""
    document = _render(
        (_QUESTION, _TURN), judge=ScriptedJudge(refusals={_QUESTION.entry_id: True})
    )

    assert "**Over-refusal** | 1" in document
    assert "**Under-refusal** | 0" in document
    assert "hedges everything and scores beautifully" in document


def test_the_allergen_section_prints_counts_and_argues_for_them() -> None:
    """No percentage, and the reason it is absent is printed where it is absent."""
    document = _render((replace(_QUESTION, dietary=True), _TURN))

    assert "## Allergen and dietary questions" in document
    assert "Held to counts, not to a rate." in document
    assert "how often the promise held" in document


def test_a_failure_arrives_with_the_argument_for_the_row() -> None:
    """A case nobody can explain is a case nobody can fix."""
    document = _render((_QUESTION, replace(_TURN, citations=())))

    assert "### `golden/k1-nutrition-calories` — cited" in document
    assert "A number off the published nutrition data." in document
    assert "1 passage(s) across 1 search(es)" in document


def test_the_ceiling_caveat_is_rendered_above_everything() -> None:
    """A reader who arrives at the table without it reads a fixture as an agent."""
    document = _render((_QUESTION, _TURN), caveat=CEILING_CAVEAT)

    assert "not a score for the agent" in document
    assert document.index("not a score for the agent") < document.index("## Coverage")


def test_a_split_trace_is_reported_with_103_named() -> None:
    """Every number below it is over a subset nobody chose."""
    split = replace(
        _TURN,
        evidence=read_evidence(_TURN.entry_id, turn_spans(_PASSAGE, split=True)),
    )

    document = _render((_QUESTION, split))

    assert "split trace" in document
    assert "#103" in document
    assert "make trace-boundary" in document
