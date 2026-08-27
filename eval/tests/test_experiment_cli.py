"""``python -m chip_chat.eval.experiment``: the free modes, and what they gate.

``--check`` exits non-zero on fewer than two arms, because with one arm there is
nothing to compare and #73's demo criterion cannot be met. ``--ceiling`` is free
and must say, above its own table, that the routing oracle never read the prompt.
"""

import json
from pathlib import Path

import pytest

from chip_chat.eval.experiment.__main__ import main


def test_check_is_free_and_prints_every_arms_fingerprint(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo_root)

    status = main(["--check"])

    out = capsys.readouterr().out
    assert status == 0
    assert "shipped" in out
    assert "lean-lanes" in out
    assert "prompt v" in out


def test_check_fails_on_a_manifest_with_only_one_arm(
    repo_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(repo_root)
    manifest = tmp_path / "one.json"
    manifest.write_text(
        json.dumps(
            {"configurations": [{"name": "only", "why": "the only arm there is"}]}
        ),
        encoding="utf-8",
    )

    status = main(["--check", "--configurations", str(manifest)])

    assert status == 1
    assert "nothing to compare" in capsys.readouterr().out


def test_check_fails_on_a_manifest_that_cannot_be_believed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "broken.json"
    manifest.write_text("not json", encoding="utf-8")

    status = main(["--check", "--configurations", str(manifest)])

    assert status == 1
    assert "error:" in capsys.readouterr().err


def test_the_ceiling_run_writes_a_document_with_both_breakdowns(
    repo_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(repo_root)
    out = tmp_path / "result.md"

    status = main(["--ceiling", "--run", "shipped", "--out", str(out)])

    capsys.readouterr()
    document = out.read_text(encoding="utf-8")
    assert status == 0
    assert "## By lane" in document
    assert "## By requirement" in document
    assert "Recorded and not applied" in document
    assert document.index("not a score for the prompt") < document.index("## By lane")


def test_the_ceiling_run_records_a_machine_readable_result(
    repo_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(repo_root)
    record = tmp_path / "shipped.json"

    status = main(
        [
            "--ceiling",
            "--run",
            "shipped",
            "--out",
            str(tmp_path / "x.md"),
            "--record",
            str(record),
        ]
    )

    capsys.readouterr()
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert status == 0
    assert payload["experiment"] == "shipped"
    assert payload["dataset_version"]
    assert len(payload["lanes"]) >= 5


def test_a_run_can_write_a_capture_for_the_promotion_path(
    repo_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#77's input, produced by #73's runner. One live run serves both."""
    monkeypatch.chdir(repo_root)
    capture = tmp_path / "capture.json"

    status = main(
        [
            "--ceiling",
            "--run",
            "shipped",
            "--out",
            str(tmp_path / "x.md"),
            "--capture",
            str(capture),
        ]
    )

    capsys.readouterr()
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert status == 0
    assert payload["turns"]
    first = payload["turns"][0]
    assert first["message"]
    assert any(span["name"] == "chat.turn" for span in first["spans"])


def test_two_recorded_results_can_be_compared_without_running_anything(
    repo_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """How a candidate is checked against a baseline recorded weeks ago."""
    monkeypatch.chdir(repo_root)
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    for name, path in (("shipped", first), ("lean-lanes", second)):
        main(
            [
                "--ceiling",
                "--run",
                name,
                "--out",
                str(tmp_path / f"{name}.md"),
                "--record",
                str(path),
            ]
        )
    capsys.readouterr()
    out = tmp_path / "comparison.md"

    status = main(["--compare-recorded", str(first), str(second), "--out", str(out)])

    capsys.readouterr()
    document = out.read_text(encoding="utf-8")
    assert status == 0
    assert "shipped → lean-lanes" in document
    assert "## By requirement" in document


def test_comparing_a_result_written_by_another_schema_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A silently-tolerated schema change compares two differently-measured things."""
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"schema": 0, "experiment": "x"}), encoding="utf-8")

    status = main(["--compare-recorded", str(old), str(old)])

    assert status == 1
    assert "schema" in capsys.readouterr().err
