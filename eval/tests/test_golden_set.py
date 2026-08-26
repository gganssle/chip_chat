"""The set this repository commits, held to its own rules.

Everything else in this directory tests the machinery on fixtures. This tests
the artefact: the thirty-odd cases in ``eval/golden/cases.json``, against the
catalogue build this repository also commits, through the command CI will run.
"""

import json
from pathlib import Path

import pytest

from chip_chat.catalog.records import MenuCatalog
from chip_chat.eval.golden.__main__ import main
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.lanes import LANE_OF, TOOLS_IN, Lane, lane_of
from chip_chat.otel.schema import ToolName

_CATALOG = Path("catalog/tests/fixtures")


def test_the_shipped_set_loads(golden: GoldenSet) -> None:
    assert len(golden) >= 30
    assert all(case.why for case in golden)


def test_every_menu_term_the_set_leans_on_is_published(
    golden: GoldenSet, catalog: MenuCatalog
) -> None:
    """The staleness detector, run against the build this repository commits.

    If this fails after a catalogue change, the set is asking about a menu the
    deployment no longer serves -- which is the failure ``cc-z1i`` says nothing
    else in the tree would notice.
    """
    golden.against(catalog)


def test_the_check_command_passes_on_the_shipped_set() -> None:
    assert main(["--check", "--catalog", str(_CATALOG)]) == 0


def test_the_check_command_fails_on_a_set_that_lost_a_case(tmp_path: Path) -> None:
    """An under-covered set is a build failure, not a warning, or it stays one."""
    manifest = json.loads(Path("eval/golden/cases.json").read_text(encoding="utf-8"))
    manifest["cases"] = [
        entry for entry in manifest["cases"] if entry["id"] != "t3-modify-draft"
    ]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert main(["--check", "--set", str(path)]) == 1


def test_the_check_command_refuses_a_term_the_build_does_not_publish(
    tmp_path: Path,
) -> None:
    manifest = json.loads(Path("eval/golden/cases.json").read_text(encoding="utf-8"))
    manifest["cases"][0]["menu_terms"] = ["sofritas"]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert main(["--check", "--set", str(path), "--catalog", str(_CATALOG)]) == 1


def test_a_manifest_that_is_not_there_exits_rather_than_traces(tmp_path: Path) -> None:
    assert main(["--check", "--set", str(tmp_path / "absent.json")]) == 1


@pytest.mark.parametrize("tool", list(ToolName))
def test_every_tool_has_a_lane(tool: ToolName) -> None:
    """A twelfth tool added without a lane would make every rate incomplete."""
    assert tool in LANE_OF
    assert lane_of(tool) is LANE_OF[tool]


def test_no_lane_holds_no_tools_and_the_five_between_them_hold_all_eleven() -> None:
    assert TOOLS_IN[Lane.NONE] == ()
    assert sum(len(TOOLS_IN[lane]) for lane in Lane) == len(list(ToolName))
    assert lane_of(None) is Lane.NONE


def test_the_set_is_written_in_the_register_a_visitor_types_in(
    golden: GoldenSet,
) -> None:
    """A set written in polished English measures a population that does not exist.

    Not a style rule: the retriever and the model both behave differently on
    *"What is contained within a burrito bowl?"* than on what somebody types,
    and a golden set that only ever sees the first one is measuring a visitor
    nobody has.
    """
    capitalised = [
        case.case_id
        for case in golden
        if case.message[:1].isupper() and not case.message.startswith("I")
    ]

    assert capitalised == []
