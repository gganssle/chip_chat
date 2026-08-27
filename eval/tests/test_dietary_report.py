"""That the document says the two things a reader cannot read a count without.

Coverage above the outcomes, and the settler beside every judgement. Both are
orderings rather than decorations: a reader who meets the gate first has already
decided what the document says, and a verdict with no settler beside it cannot
be checked against #84's *verified by hand, not only by a judge* at all.
"""

from chip_chat.eval.dietary.coverage import coverage
from chip_chat.eval.dietary.probes import ProbeSet
from chip_chat.eval.dietary.report import build_report, render
from chip_chat.eval.dietary.run import run_probes
from chip_chat.eval.dietary.testing import (
    CarefulTarget,
    DerivingTarget,
    ObliviousTarget,
    ScriptedJudge,
    hand_check,
)


def _document(probes: ProbeSet, target, **kwargs) -> str:
    turns = run_probes(probes.probes, target)
    return render(
        build_report(probes, turns, coverage(probes), source=target.name, **kwargs)
    )


def test_the_scope_is_printed_above_the_outcomes(probes: ProbeSet) -> None:
    """A thin red team and a careful product produce the same counts."""
    document = _document(probes, CarefulTarget())
    assert document.index("Is this the red team") < document.index("The counts")


def test_an_unmeasured_gate_is_not_a_pass(probes: ProbeSet) -> None:
    """*We could not tell* and *nothing got past* are the same shade of green."""
    document = _document(probes, CarefulTarget())
    assert "not measured" in document
    assert "could not be settled" in document


def test_a_breach_names_the_probe_and_prints_its_argument(probes: ProbeSet) -> None:
    """An attack nobody can explain is one nobody will maintain."""
    turns = run_probes(probes.probes, DerivingTarget())
    judge = ScriptedJudge(
        refusals={turn.probe_id: False for turn in turns},
        derivations={
            turn.probe_id: "so those are dairy-free" in turn.reply for turn in turns
        },
    )
    document = render(
        build_report(
            probes,
            turns,
            coverage(probes),
            source="scripted",
            judge=judge,
        )
    )
    assert "BREACHED" in document
    assert "reasoned past the published record" in document
    assert any(probe.why[:40] in document for probe in probes)


def test_over_refusal_is_in_the_same_table_and_marked_ungated(
    probes: ProbeSet,
) -> None:
    """Reported at the same size as the unsafe mistake, and not gated."""
    turns = run_probes(probes.probes, ObliviousTarget())
    judge = ScriptedJudge(refusals={turn.probe_id: True for turn in turns})
    document = render(
        build_report(probes, turns, coverage(probes), source="scripted", judge=judge)
    )
    assert "Over-refusals" in document
    assert "measured, not gated" in document


def test_the_settler_is_printed_beside_the_judgement(probes: ProbeSet) -> None:
    """#84's second criterion, as something a reader can check the document against."""
    turns = run_probes(probes.probes, CarefulTarget())
    document = render(
        build_report(
            probes,
            turns,
            coverage(probes),
            source="scripted",
            hand=hand_check(turns, derived=False, refused=True),
        )
    )
    assert "Settled by:" in document
    assert "hand" in document


def test_an_empty_hand_record_says_so_and_names_the_procedure(
    probes: ProbeSet, hand
) -> None:
    """The gate is held shut by it, so the document has to say what would open it."""
    document = _document(probes, CarefulTarget(), hand=hand)
    assert "HAND-CHECK.md" in document
