"""What a free run against the week-one slice is worth, which is one sentence.

Not a score. The model is a stub that opens the knowledge lane and reads back
what came out of it, so the replies measure the corpus rather than a model --
``chip_chat.eval.dietary.testing`` makes the argument and
``chip_chat.eval.golden.testing`` made it first.

What it is worth is the line at the top of the document: this deployment serves
no published allergen record, so most of #84 could not be asked of it at all.
The tests here hold that claim to the code rather than to the prose, because a
document asserting an unmeasured gate while the harness quietly scored something
is the one failure a reader has no way to notice.
"""

from chip_chat.eval.dietary.probes import Capability, ProbeSet
from chip_chat.eval.dietary.scoring import score
from chip_chat.eval.dietary.slice import SLICE_CAPABILITIES
from chip_chat.eval.dietary.testing import ceiling


def test_the_slice_declares_no_capability(probes: ProbeSet) -> None:
    """An invented three-item menu is not the published allergen record.

    Overstating this is the one error that turns an unasked question into a
    boundary that held, so the constant is empty and the report says which
    wiring would fill it.
    """
    assert not SLICE_CAPABILITIES


def test_every_probe_reaches_the_knowledge_lane(probes: ProbeSet) -> None:
    """The probes travel the real request path, which is what makes this free run real."""
    turns = ceiling(probes.probes)
    assert len(turns) == len(probes)
    assert all(turn.error is None for turn in turns)
    assert all("search_menu_knowledge" in turn.tools for turn in turns)


def test_the_retrieval_is_read_back_off_the_span_tree(probes: ProbeSet) -> None:
    """``retriever.search`` spans, the same ones ``eval/grounding`` reads."""
    turns = ceiling(probes.probes)
    evidence = [turn.evidence for turn in turns]
    assert all(item is not None for item in evidence)
    assert any(item.retrieved for item in evidence if item is not None)


def test_the_gate_is_unmeasured_and_that_is_the_headline(probes: ProbeSet) -> None:
    """A gate nobody measured has not passed, and here nobody could."""
    scores = score(probes.probes, ceiling(probes.probes))
    assert scores.gate is None
    assert scores.breaches == 0
    assert scores.unscored == len(probes)


def test_the_probes_that_lean_on_the_record_are_unscored_for_that_reason(
    probes: ProbeSet,
) -> None:
    """Not *the boundary held*, and not *the model was wrong*: never asked."""
    scores = score(probes.probes, ceiling(probes.probes))
    leaning = {
        probe.probe_id
        for probe in probes
        if Capability.PUBLISHED_ALLERGENS in probe.needs
    }
    blocked = {
        item.probe.probe_id
        for item in scores.unmeasured()
        if any("published_allergens" in line for line in _reasons(item))
    }
    assert leaning <= blocked


def _reasons(item) -> tuple[str, ...]:
    return (*item.assessment.details.values(), item.assessment.refusal_detail)
