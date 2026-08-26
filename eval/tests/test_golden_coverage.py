"""Whether the set is the set issue #29 asked for.

The scorer says how well a deployment did on the cases it was given. Nothing in
it can notice that those cases were twelve easy knowledge questions -- that set
scores beautifully and says nothing about the five lanes, and the failure is
invisible to any pass rate. These are the checks that see it.
"""

import json
from pathlib import Path

from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.coverage import SHAPES, coverage
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.requirements import DELEGATIONS, OUT_OF_SCOPE, REQUIREMENTS
from chip_chat.otel.schema import ToolName


def _without(tmp_path: Path, golden: GoldenSet, case_id: str) -> GoldenSet:
    """The shipped set with one case removed."""
    manifest = json.loads(golden.source.read_text(encoding="utf-8"))
    manifest["cases"] = [entry for entry in manifest["cases"] if entry["id"] != case_id]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return GoldenSet.load(path)


def test_the_shipped_set_covers_every_requirement(golden: GoldenSet) -> None:
    """#29's first acceptance criterion."""
    cover = coverage(golden)

    assert cover.uncovered == ()
    assert cover.every_requirement_covered


def test_the_shipped_set_expects_every_one_of_the_eleven_tools(
    golden: GoldenSet,
) -> None:
    """Tool selection is the metric, so a tool no case reaches for is a hole."""
    assert coverage(golden).tools_without_a_case == ()


def test_the_shipped_set_meets_every_shape_clause(golden: GoldenSet) -> None:
    cover = coverage(golden)

    assert cover.unmet == ()
    assert cover.complete


def test_a_delegated_requirement_is_neither_uncovered_nor_a_case(
    golden: GoldenSet,
) -> None:
    """Three outcomes, and the middle one is the whole point of the register.

    V4 -- the not-Chipotle photograph -- has no golden case and is not a gap:
    it is measured over the labeled photo set's frames, and the delegation says
    so with its target.
    """
    cover = coverage(golden)
    delegated = {item.id for item, _ in cover.delegated}
    covered = {item.id for item, _ in cover.covered}

    assert "V4" in delegated
    assert "V4" not in covered
    assert "V4" not in {item.id for item in cover.uncovered}


def test_a_requirement_can_be_both_covered_and_delegated(golden: GoldenSet) -> None:
    """V2 is routed here and scored there, and hiding either half would mislead."""
    cover = coverage(golden)

    assert "V2" in {item.id for item, _ in cover.covered}
    assert "V2" in {item.id for item, _ in cover.delegated}


def test_removing_a_case_shows_up_as_an_uncovered_requirement(
    tmp_path: Path, golden: GoldenSet
) -> None:
    """T3 has exactly one case, which is what makes it a usable probe here."""
    reduced = _without(tmp_path, golden, "t3-modify-draft")
    cover = coverage(reduced)

    assert "T3" in {item.id for item in cover.uncovered}
    assert not cover.complete


def test_removing_a_case_shows_up_as_a_tool_with_no_case(
    tmp_path: Path, golden: GoldenSet
) -> None:
    reduced = _without(tmp_path, golden, "t1-cancel-order")

    assert ToolName.CANCEL_ORDER in coverage(reduced).tools_without_a_case


def test_every_shape_clause_names_the_document_that_asks_for_it() -> None:
    """A clause whose reason is elsewhere is one somebody deletes as arbitrary."""
    assert all(shape.source.strip() for shape in SHAPES)
    assert all(shape.minimum > 0 for shape in SHAPES)


def test_every_delegation_names_a_target_and_an_argument() -> None:
    """A delegation with no argument is a gap somebody labeled to go green."""
    for delegation in DELEGATIONS:
        assert delegation.target.strip()
        assert delegation.reason.strip()
        assert delegation.requirement_id in {item.id for item in REQUIREMENTS}


def test_the_entry_requirements_are_excluded_out_loud() -> None:
    """A requirement nobody scores and nobody mentions looks like an oversight."""
    assert set(OUT_OF_SCOPE) == {f"E{index}" for index in range(1, 8)}
    assert all(reason.strip() for reason in OUT_OF_SCOPE.values())
    assert not {item.id for item in REQUIREMENTS} & set(OUT_OF_SCOPE)


def test_every_requirement_belongs_to_a_lane_or_to_none_of_them() -> None:
    for item in REQUIREMENTS:
        assert isinstance(item.lane, Lane)
        assert item.text.strip()
