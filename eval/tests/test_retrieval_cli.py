"""``python -m chip_chat.eval.retrieval``, driven in process.

The exit status is the interesting part of this file. Three of the four modes
have a gate, and each of them exists to stop a different way of quietly not
measuring anything.
"""

from pathlib import Path

import pytest

from chip_chat.eval.retrieval.__main__ import main

FIXTURE = Path("search/tests/fixtures/chunks.jsonl")
RUN_ID = "20260827T053000Z"


def offline(*extra: str) -> list[str]:
    return ["--offline", "--chunks", str(FIXTURE), "--run-id", RUN_ID, *extra]


def test_the_free_check_passes_on_the_shipped_set(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo_root)
    assert main(["--check"]) == 0


def test_the_check_fails_on_a_set_that_misses_a_scope_clause(
    tmp_path: Path,
) -> None:
    # An under-covered set is a build failure, or it stays under-covered.
    manifest = tmp_path / "questions.json"
    manifest.write_text(
        '{"questions": [{"id": "q", "question": "x", "category": "allergens", '
        '"relevant": [{"kind": "MENU_ITEM", "item_id": "CMG-1", "why": "w"}], '
        '"why": "w"}]}',
        encoding="utf-8",
    )
    assert main(["--check", "--set", str(manifest)]) == 1


def test_a_manifest_that_cannot_be_believed_fails_before_anything_else(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "questions.json"
    manifest.write_text("not json", encoding="utf-8")
    assert main(["--check", "--set", str(manifest)]) == 1


def test_the_offline_sweep_is_free_and_green(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Green despite two labels naming nothing in the fixture: it is a slice of
    # the published pages and nobody promised otherwise. The note says so and
    # the report lists them.
    monkeypatch.chdir(repo_root)
    assert main(offline()) == 0
    captured = capsys.readouterr()
    assert "ing-barbacoa" in captured.err
    assert "nobody promised was complete" in captured.err


def test_a_corpus_declared_complete_fails_on_a_label_it_does_not_hold(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #50's fourth acceptance criterion, as an exit status. Against a corpus
    # somebody has said is the whole corpus, a label naming nothing in it names
    # nothing anywhere.
    monkeypatch.chdir(repo_root)
    assert main(offline("--complete")) == 1


def test_the_offline_sweep_writes_a_report(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo_root)
    out = tmp_path / "BASELINE.md"
    assert main(offline("--out", str(out))) == 0
    document = out.read_text(encoding="utf-8")
    assert document.startswith("# Retrieval eval — baseline")
    assert "were not measured against a retrieval service" in document


def test_the_committed_baseline_is_a_measured_one(repo_root: Path) -> None:
    # `make retrieval` is free and cannot produce this file. The committed
    # baseline came from `make retrieval-baseline` against the live alias, and
    # the difference matters enough that the report carries a paragraph about
    # it -- so this asserts the committed document is not the free one wearing
    # the same name.
    committed = (repo_root / "eval" / "retrieval" / "BASELINE.md").read_text(
        encoding="utf-8"
    )
    assert "come from a real retrieval service" in committed
    assert "were not measured against a retrieval service" not in committed


def test_the_committed_baseline_records_the_demo_bar(repo_root: Path) -> None:
    # #50's third acceptance criterion: the allergen number is *recorded as the
    # baseline the rest of the project is held to*. A report that stopped
    # printing that sentence would leave the criterion satisfied by nothing.
    committed = (repo_root / "eval" / "retrieval" / "BASELINE.md").read_text(
        encoding="utf-8"
    )
    assert "The demo bar: top-3 recall on the allergen questions" in committed
    assert "the rest of the project is held to" in committed


def test_an_offline_sweep_needs_a_corpus_to_build_an_index_from() -> None:
    assert main(["--offline"]) == 1


def test_a_measured_sweep_refuses_to_spend_the_allowance_unasked(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # It stops before it opens a connection, which is the point: the Free tier's
    # allowance is a hard stop rather than an overage.
    monkeypatch.chdir(repo_root)
    assert main(["--chunks", str(FIXTURE), "--run-id", RUN_ID]) == 1
    assert "of the month's 1,000 semantic requests" in capsys.readouterr().err
