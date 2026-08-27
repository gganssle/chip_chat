"""That the set is the red team #84 asked for, and that the check would notice.

Coverage is the only thing standing between a thin red team and a document that
looks exactly like a careful product's. The scorer cannot help: four polite
questions produce zero breaches and a clean gate.
"""

from chip_chat.eval.dietary.coverage import CLAUSES, REQUIRED, coverage
from chip_chat.eval.dietary.probes import Capability, ProbeSet, Shape


def test_the_shipped_set_meets_every_clause(probes: ProbeSet) -> None:
    """#84's scope, as clauses, against the set this repository commits."""
    cover = coverage(probes)
    assert cover.complete, [clause.name for clause, _ in cover.unmet]


def test_every_clause_names_the_document_that_asks_for_it() -> None:
    """A minimum with no argument behind it is a number somebody will delete."""
    for clause in CLAUSES:
        assert clause.source.strip()
        assert clause.minimum > 0


def test_the_scope_check_notices_a_missing_attack(probes: ProbeSet) -> None:
    """Demonstrated rather than asserted: drop the photographs and it should show."""
    thinned = ProbeSet(
        probes=tuple(probe for probe in probes if probe.shape is not Shape.PHOTO),
        source=probes.source,
    )
    cover = coverage(thinned)
    assert not cover.complete
    assert Shape.PHOTO in cover.shapes_without_a_probe


def test_the_scope_check_notices_a_thinned_clause(probes: ProbeSet) -> None:
    """One unanswerable question is not evidence that the boundary holds."""
    kept = [probe for probe in probes if probe.shape is not Shape.UNANSWERABLE]
    kept.append(probes.by_shape(Shape.UNANSWERABLE)[0])
    cover = coverage(ProbeSet(probes=tuple(kept), source=probes.source))
    assert not cover.complete
    assert any("does not cover" in clause.name for clause, _ in cover.unmet)


def test_every_required_requirement_is_covered(probes: ProbeSet) -> None:
    """K3 is the boundary; K2 and K5 are what a refusal still owes."""
    cover = coverage(probes)
    assert not cover.uncovered
    assert {item.id for item, _ in cover.covered} == set(REQUIRED)


def test_the_report_can_say_which_wiring_would_move_which_probes(
    probes: ProbeSet,
) -> None:
    """*The gate is unmeasured* is a complaint; this is what makes it an instruction."""
    cover = coverage(probes)
    named = dict(cover.capabilities)
    assert named[Capability.PUBLISHED_ALLERGENS]
    assert named[Capability.PUBLISHED_CAVEATS]
    assert named[Capability.PHOTO_TURNS]
