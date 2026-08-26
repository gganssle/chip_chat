"""What the golden set refuses to be.

Every refusal here is a way a set of evaluation questions stops being evidence:
a case tied to a requirement the PRD does not have, a lane that does not hold
the tool beside it, a write case in which the confirmation card was optional.
The point of failing at load is that nobody gets to read a number computed over
one of those.
"""

import json
from pathlib import Path

import pytest

from chip_chat.catalog.records import MenuCatalog
from chip_chat.eval.golden.cases import (
    ANY_PERSONA,
    JUDGED,
    CaseError,
    Check,
    GoldenSet,
)
from chip_chat.eval.golden.lanes import Lane
from chip_chat.otel.schema import ToolName

_MINIMAL = {
    "id": "k1-example",
    "message": "is the barbacoa spicy",
    "tool": "search_menu_knowledge",
    "requirements": ["K1"],
    "why": "A case has to say what it is for.",
}


def _write(tmp_path: Path, *entries: dict[str, object]) -> Path:
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps({"cases": list(entries)}), encoding="utf-8")
    return manifest


def _load(tmp_path: Path, **overrides: object) -> GoldenSet:
    return GoldenSet.load(_write(tmp_path, {**_MINIMAL, **overrides}))


def test_a_minimal_case_loads_and_derives_its_lane(tmp_path: Path) -> None:
    (case,) = _load(tmp_path).cases

    assert case.tool is ToolName.SEARCH_MENU_KNOWLEDGE
    assert case.lane is Lane.KNOWLEDGE
    assert case.persona == ANY_PERSONA
    assert not case.confirmed


def test_a_declared_lane_that_does_not_hold_the_tool_is_refused(
    tmp_path: Path,
) -> None:
    """The manifest carries the lane so the JSON reads as the five-lane table.

    Redundancy is only worth having if a disagreement is an error.
    """
    with pytest.raises(CaseError, match="does not hold"):
        _load(tmp_path, lane="account")


def test_a_requirement_the_prd_does_not_have_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="no requirement K9"):
        _load(tmp_path, requirements=["K9"])


def test_an_entry_requirement_is_refused_with_its_reason(tmp_path: Path) -> None:
    """E1-E7 are properties of a screen, not answers to a question."""
    with pytest.raises(CaseError, match="Entry requirement"):
        _load(tmp_path, requirements=["E3"])


def test_a_case_covering_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="at least one requirement"):
        _load(tmp_path, requirements=[])


def test_a_case_that_does_not_say_what_it_is_for_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="must say what it is for"):
        _load(tmp_path, why="   ")


def test_a_write_case_must_check_for_a_card_first(tmp_path: Path) -> None:
    """PRD T2 has no exceptions, so neither does the manifest."""
    with pytest.raises(CaseError, match="T2 has no exceptions"):
        _load(
            tmp_path,
            tool="place_order",
            requirements=["T1"],
            context=["Place it?"],
            confirmed=True,
        )


def test_a_confirmed_draft_needs_the_turn_that_put_it_on_screen(
    tmp_path: Path,
) -> None:
    with pytest.raises(CaseError, match="in `context`"):
        _load(
            tmp_path,
            tool="place_order",
            requirements=["T1"],
            checks=["confirms_first"],
            confirmed=True,
        )


def test_only_an_action_turn_can_act_on_a_confirmed_draft(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="only an action turn"):
        _load(tmp_path, confirmed=True, context=["Place it?"])


def test_a_tool_that_is_both_expected_and_forbidden_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(CaseError, match="both expected and forbidden"):
        _load(tmp_path, forbidden_tools=["search_menu_knowledge"])


def test_a_case_expecting_no_tool_must_be_in_no_lane(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="does not hold no tool"):
        _load(tmp_path, tool=None, lane="knowledge")


def test_adjacent_placement_without_a_citation_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="placement of nothing"):
        _load(tmp_path, checks=["cites_adjacent"])


def test_duplicate_case_ids_are_refused(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="duplicate case id"):
        GoldenSet.load(_write(tmp_path, dict(_MINIMAL), dict(_MINIMAL)))


def test_a_manifest_that_is_not_a_manifest_is_refused(tmp_path: Path) -> None:
    manifest = tmp_path / "cases.json"
    manifest.write_text('{"nope": []}', encoding="utf-8")

    with pytest.raises(CaseError, match="`cases` array"):
        GoldenSet.load(manifest)


def test_a_missing_file_is_refused_rather_than_read_as_empty(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="could not read"):
        GoldenSet.load(tmp_path / "absent.json")


def test_judged_and_deterministic_checks_partition_the_case(tmp_path: Path) -> None:
    (case,) = _load(tmp_path, checks=["cites", "grounded"]).cases

    assert case.judged_checks == frozenset({Check.GROUNDED})
    assert case.deterministic_checks == frozenset({Check.CITES})
    assert case.judged_checks <= JUDGED


def test_a_term_the_catalogue_does_not_publish_refuses_the_whole_set(
    tmp_path: Path, catalog: MenuCatalog
) -> None:
    """The staleness detector, and why it refuses the set rather than the case.

    One term from another build means the manifest was written against a menu
    this deployment does not serve, and the rest of it is no more trustworthy.
    """
    golden = _load(tmp_path, menu_terms=["white rice", "carnitas"])

    with pytest.raises(CaseError, match="publishes no menu term 'carnitas'"):
        golden.against(catalog)


def test_a_term_is_matched_however_the_catalogue_spells_it(
    tmp_path: Path, catalog: MenuCatalog
) -> None:
    """A person writing "white rice" should not have to know it is ``white_rice``."""
    golden = _load(
        tmp_path, menu_terms=["White Rice", "white_rice", "black beans", "Chips"]
    )

    golden.against(catalog)


def test_by_lane_and_covering_read_the_set_back(tmp_path: Path) -> None:
    golden = GoldenSet.load(
        _write(
            tmp_path,
            dict(_MINIMAL),
            {
                **_MINIMAL,
                "id": "a1-points",
                "message": "how many points do i have",
                "tool": "get_points_balance",
                "requirements": ["A1"],
            },
        )
    )

    assert [case.case_id for case in golden.by_lane(Lane.ACCOUNT)] == ["a1-points"]
    assert [case.case_id for case in golden.covering("K1")] == ["k1-example"]
    assert len(golden) == 2
