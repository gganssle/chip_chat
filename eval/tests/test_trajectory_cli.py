"""``python -m chip_chat.eval.trajectory``: the two free modes, and what they gate.

The exit statuses are the interesting part. ``--check`` fails on a scope clause
the rows cannot meet, because a gap in what can be measured is a build failure
or it stays a gap. ``--ceiling`` fails on a split trace and on nothing else --
the accuracy is not gated there, because the slice implements six of the eleven
tools and a gate that is red by construction is a gate somebody switches off.
"""

from pathlib import Path

import pytest

from chip_chat.eval.trajectory.__main__ import main


def test_check_is_free_and_reports_the_lanes(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No model, no credentials, and the per-lane row counts printed."""
    monkeypatch.chdir(repo_root)

    status = main(["--check"])

    out = capsys.readouterr().out
    assert status == 0
    assert "rows that score routing" in out
    assert "knowledge" in out
    assert "(thin)" in out


def test_check_fails_when_a_manifest_cannot_be_believed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused while the register is built, with the reason on stderr."""
    manifest = tmp_path / "cases.json"
    manifest.write_text("not json", encoding="utf-8")

    status = main(["--check", "--golden", str(manifest)])

    assert status == 1
    assert "error:" in capsys.readouterr().err


def test_the_ceiling_run_writes_a_report_and_passes_the_trace_gate(
    repo_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mode CI runs: free, and green while every turn is one trace.

    The number in the document is well under the target and that is not what is
    being asserted -- see the module docstring, and
    ``chip_chat.eval.trajectory.testing`` on what a run against an oracle is
    worth at all.
    """
    monkeypatch.chdir(repo_root)
    out = tmp_path / "BASELINE.md"

    status = main(["--ceiling", "--out", str(out)])

    document = out.read_text(encoding="utf-8")
    assert status == 0
    assert "Trajectory and tool-selection baseline" in document
    assert "This is not a score for the agent" in document
    assert str(out) in capsys.readouterr().out


def test_only_runs_the_rows_it_was_given(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Iterating on one row should not cost the other thirty-three."""
    monkeypatch.chdir(repo_root)

    status = main(["--ceiling", "--only", "golden/k1-bowl-ingredients"])

    assert status == 0
    assert "| Rows run | 1 |" in capsys.readouterr().out
