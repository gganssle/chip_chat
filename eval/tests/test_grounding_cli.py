"""``python -m chip_chat.eval.grounding``: the two free modes, and what they gate.

The exit statuses are the interesting part, and the narrowness is deliberate. A
run fails on a *measured* gate breach and on a split trace, and on nothing else.
It must not fail because the citation gate is unmeasured: PRD section 12 makes
that gate blocking, nothing in this repository can count it yet, and a build
that is red about a missing wire is a build somebody switches the check off in.
"""

from pathlib import Path

import pytest

from chip_chat.eval.grounding.__main__ import main


def test_check_is_free_and_reports_the_category(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No model, no credentials, and the allergen row count printed."""
    monkeypatch.chdir(repo_root)

    status = main(["--check"])

    out = capsys.readouterr().out
    assert status == 0
    assert "allergen or dietary" in out
    assert "over-refusal is observable" in out


def test_check_fails_when_a_manifest_cannot_be_believed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused while the register is built, with the reason on stderr."""
    manifest = tmp_path / "cases.json"
    manifest.write_text("not json", encoding="utf-8")

    status = main(["--check", "--golden", str(manifest)])

    assert status == 1
    assert "error:" in capsys.readouterr().err


def test_the_ceiling_run_writes_a_report_and_is_green_while_nothing_is_measured(
    repo_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mode CI runs. Green, and the document says why that is not a result."""
    monkeypatch.chdir(repo_root)
    out = tmp_path / "BASELINE.md"

    status = main(["--ceiling", "--out", str(out)])

    document = out.read_text(encoding="utf-8")
    assert status == 0
    assert "Groundedness and citation-presence baseline" in document
    assert "This is not a score for the agent" in document
    assert "Citation gate: unmeasured." in document
    assert str(out) in capsys.readouterr().out


def test_the_ceiling_run_finds_claims_with_nothing_behind_them(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one finding a free run produces, and it is a real one.

    The week-one slice's menu is three items and no published policy pages, so
    *what happens if my order is wrong* reaches the knowledge lane and comes
    back with nothing. No prompt work moves that; the corpus does.
    """
    monkeypatch.chdir(repo_root)

    status = main(["--ceiling", "--only", "golden/k1-refund-policy"])

    out = capsys.readouterr().out
    assert status == 0
    assert "| Claims with nothing retrieved | 1 |" in out
    assert "golden/k1-refund-policy` — supported" in out


def test_only_runs_the_rows_it_was_given(
    repo_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Iterating on one row should not cost the other thirty-three."""
    monkeypatch.chdir(repo_root)

    status = main(["--ceiling", "--only", "golden/k1-bowl-ingredients"])

    assert status == 0
    assert "| Turns run | 1 |" in capsys.readouterr().out
