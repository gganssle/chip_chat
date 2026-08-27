"""A trace to a dataset entry, and the two ways that path can quietly go wrong.

The first is promoting the bug: writing the tool the agent *actually reached
for* into the case as the expected one, which turns the golden set into a record
of what the product does. The second is provenance in the wrong place: a column
on the dataset entry would rebase every existing digest and move the version for
a reason that has nothing to do with the rows.

Both are tested here, because both produce a green build and a broken set.
"""

import json
from pathlib import Path

import pytest

from chip_chat.eval.adversarial.attacks import AdversarialSuite
from chip_chat.eval.dataset.build import build_dataset
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.online.testing import ungrounded_menu_claim
from chip_chat.eval.photos.labels import LabeledSet
from chip_chat.eval.promote.__main__ import ADVERSARIAL_SOURCE, main
from chip_chat.eval.promote.apply import PromotionError, apply_draft, traffic_entries
from chip_chat.eval.promote.candidates import NEEDS_A_HUMAN, draft, from_alerts
from chip_chat.eval.promote.ledger import (
    DEFAULT_LEDGER,
    LedgerError,
    PermanentSource,
    Promotion,
    Provenance,
    check,
    load,
    write,
)


def _labelled(body: dict[str, object]) -> dict[str, object]:
    """A draft with the three human fields filled in, as a person would."""
    return {
        **body,
        "tool": "search_menu_knowledge",
        "lane": "knowledge",
        "requirements": ["K1"],
        "checks": ["cites", "grounded"],
        "why": "A real visitor asked it and the monitor flagged the answer.",
    }


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "version": "1",
                "cases": [
                    {
                        "id": "k1-existing",
                        "message": "what is in a bowl",
                        "tool": "search_menu_knowledge",
                        "lane": "knowledge",
                        "requirements": ["K1"],
                        "checks": ["cites"],
                        "why": "an existing case",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_draft_fills_in_everything_the_trace_can_supply() -> None:
    turn = ungrounded_menu_claim().turn
    body = draft(from_alerts(turn, ()))

    assert body["message"] == turn.message
    assert str(body["id"]).startswith("live-")


def test_the_tool_the_agent_reached_for_is_never_written_in_as_the_label() -> None:
    """The interesting traces are the ones where what it did was wrong."""
    turn = ungrounded_menu_claim().turn
    body = draft(from_alerts(turn, ()))

    assert body["tool"] == NEEDS_A_HUMAN
    assert body["lane"] == NEEDS_A_HUMAN
    observed = body["_observed"]
    assert isinstance(observed, dict)
    assert observed["tool"] == "search_menu_knowledge"


def test_a_draft_still_carrying_a_placeholder_is_refused(tmp_path: Path) -> None:
    body = draft(from_alerts(ungrounded_menu_claim().turn, ()))

    with pytest.raises(PromotionError, match="TODO"):
        apply_draft(
            body, manifest=_manifest(tmp_path), ledger_path=tmp_path / "prov.json"
        )


def test_a_labelled_draft_is_appended_and_the_existing_cases_are_untouched(
    tmp_path: Path,
) -> None:
    """Appended rather than sorted: the dataset hashes entries in build order."""
    manifest = _manifest(tmp_path)
    before = json.loads(manifest.read_text(encoding="utf-8"))["cases"]
    body = _labelled(draft(from_alerts(ungrounded_menu_claim().turn, ())))

    case_id = apply_draft(body, manifest=manifest, ledger_path=tmp_path / "prov.json")

    after = json.loads(manifest.read_text(encoding="utf-8"))["cases"]
    assert after[: len(before)] == before
    assert after[-1]["id"] == case_id
    assert "_observed" not in after[-1]


def test_a_case_the_set_would_refuse_leaves_both_files_untouched(
    tmp_path: Path,
) -> None:
    """The loader's rules, not a second copy of them."""
    manifest = _manifest(tmp_path)
    original = manifest.read_text(encoding="utf-8")
    body = _labelled(draft(from_alerts(ungrounded_menu_claim().turn, ())))
    body["requirements"] = []

    with pytest.raises(PromotionError, match="would not load"):
        apply_draft(body, manifest=manifest, ledger_path=tmp_path / "prov.json")

    assert manifest.read_text(encoding="utf-8") == original


def test_reusing_an_id_is_refused_because_a_changed_question_is_a_new_question(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    body = _labelled(draft(from_alerts(ungrounded_menu_claim().turn, ())))
    body["id"] = "k1-existing"

    with pytest.raises(PromotionError, match="already holds a case"):
        apply_draft(body, manifest=manifest, ledger_path=tmp_path / "prov.json")


def test_the_provenance_row_records_the_trace_and_the_monitor(tmp_path: Path) -> None:
    drill = ungrounded_menu_claim()
    from chip_chat.eval.online.monitors import evaluate

    body = _labelled(draft(from_alerts(drill.turn, evaluate(drill.turn))))
    ledger = tmp_path / "prov.json"

    apply_draft(body, manifest=_manifest(tmp_path), ledger_path=ledger)

    provenance = load(ledger)
    assert traffic_entries(provenance) == 1
    row = provenance.promotions[0]
    assert row.trace_id == drill.turn.trace_id
    assert "ungrounded_menu_claim" in row.monitors


def test_two_provenance_rows_for_one_case_are_refused() -> None:
    """Two answers to *where did this come from*, and the second is always wrong."""
    provenance = Provenance().with_promotion(Promotion(case_id="x", source="production"))

    with pytest.raises(LedgerError, match="already has a provenance row"):
        provenance.with_promotion(Promotion(case_id="x", source="authored"))


def test_promoting_a_case_moves_the_dataset_version_and_nothing_else(
    tmp_path: Path, golden: GoldenSet, photos: LabeledSet
) -> None:
    """The whole argument for keeping provenance out of the entry's columns."""
    before = build_dataset(golden, photos)
    digests = {entry.entry_id: entry.digest for entry in before.entries}

    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "1",
                "cases": [_case_body(case) for case in golden],
            }
        ),
        encoding="utf-8",
    )
    body = _labelled(draft(from_alerts(ungrounded_menu_claim().turn, ())))
    apply_draft(body, manifest=manifest, ledger_path=tmp_path / "prov.json")

    after = build_dataset(GoldenSet.load(manifest), photos)

    assert after.version != before.version
    for entry in after.entries:
        if entry.entry_id in digests:
            assert entry.digest == digests[entry.entry_id]


def _case_body(case: object) -> dict[str, object]:
    """One shipped case, back in manifest shape, for the round-trip above."""
    body: dict[str, object] = {
        "id": case.case_id,  # type: ignore[attr-defined]
        "message": case.message,  # type: ignore[attr-defined]
        "lane": case.lane.value,  # type: ignore[attr-defined]
        "requirements": list(case.requirements),  # type: ignore[attr-defined]
        "checks": sorted(check.value for check in case.checks),  # type: ignore[attr-defined]
        "why": case.why,  # type: ignore[attr-defined]
    }
    if case.tool is not None:  # type: ignore[attr-defined]
        body["tool"] = case.tool.value  # type: ignore[attr-defined]
    if case.persona != "any":  # type: ignore[attr-defined]
        body["persona"] = case.persona  # type: ignore[attr-defined]
    if case.context:  # type: ignore[attr-defined]
        body["context"] = list(case.context)  # type: ignore[attr-defined]
    if case.confirmed:  # type: ignore[attr-defined]
        body["confirmed"] = True
    if case.dietary:  # type: ignore[attr-defined]
        body["dietary"] = True
    if case.forbidden_tools:  # type: ignore[attr-defined]
        body["forbidden_tools"] = sorted(
            tool.value
            for tool in case.forbidden_tools  # type: ignore[attr-defined]
        )
    if case.menu_terms:  # type: ignore[attr-defined]
        body["menu_terms"] = list(case.menu_terms)  # type: ignore[attr-defined]
    return body


def test_every_adversarial_attack_is_recorded_as_a_permanent_regression_entry(
    repo_root: Path, suite: AdversarialSuite
) -> None:
    """#77's third criterion, as a check that fails rather than a promise."""
    provenance = load(repo_root / DEFAULT_LEDGER)

    problems = check(
        provenance,
        {ADVERSARIAL_SOURCE: [attack.attack_id for attack in suite.attacks]},
    )

    assert problems == ()
    recorded = provenance.source(ADVERSARIAL_SOURCE)
    assert recorded is not None
    assert len(recorded.ids) == len(suite.attacks)
    assert recorded.runs_in == "make adversarial-redteam"


def test_an_attack_added_without_being_recorded_fails_the_check() -> None:
    provenance = Provenance(
        permanent=(
            PermanentSource(
                name="a-suite", manifest="m.json", runs_in="make x", ids=("one",)
            ),
        )
    )

    problems = check(provenance, {"a-suite": ["one", "two"]})

    assert len(problems) == 1
    assert "two" in problems[0]


def test_an_unrecorded_permanent_source_is_a_problem_rather_than_a_silence() -> None:
    problems = check(Provenance(), {"a-suite": ["one"]})

    assert "nothing promises" in problems[0]


def test_the_ledger_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "prov.json"
    original = Provenance(
        promotions=(Promotion(case_id="x", source="production", trace_id="t"),),
        permanent=(PermanentSource(name="s", manifest="m", runs_in="r", ids=("a", "b")),),
    )

    write(original, path)

    assert load(path).as_json() == original.as_json()


def test_a_missing_ledger_is_an_empty_one_rather_than_an_error(tmp_path: Path) -> None:
    """The first promotion must not be the hard one."""
    assert load(tmp_path / "nothing.json").promotions == ()


def test_the_cli_check_holds_the_shipped_suite_to_the_shipped_ledger(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo_root)

    status = main(["--check"])

    out = capsys.readouterr().out
    assert status == 0
    assert "permanent: adversarial-suite" in out
    assert "make adversarial-redteam" in out


def test_the_cli_drafts_only_the_flagged_turns_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#77's *selection driven by the monitors*, as a command."""
    from chip_chat.eval.online.testing import drills

    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "turns": [
                    {"message": "ordinary", "reply": "fine", "spans": []},
                    *(
                        {
                            "message": drill.turn.message,
                            "reply": drill.turn.reply,
                            "spans": [
                                {
                                    "name": "chat.turn",
                                    "span_id": "0" * 16,
                                    "parent_id": None,
                                    "trace_id": drill.turn.trace_id,
                                    "attributes": {
                                        "chip_chat.demo.id": "demo-0001",
                                        "chip_chat.tokens.total": 99_999,
                                    },
                                    "service": "chip-chat-api",
                                    "started": 0,
                                }
                            ],
                        }
                        for drill in drills()[:1]
                    ),
                ]
            }
        ),
        encoding="utf-8",
    )

    status = main(["--drafts", str(capture)])

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert len(payload["cases"]) == 1
