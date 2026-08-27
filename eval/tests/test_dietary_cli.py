"""That the free check is worth running, and that a run is red about the right things.

``--check`` is the one that goes in CI. It costs nothing, and it catches the two
failures that matter most: a red team that has quietly stopped covering one of
#84's attacks, and a probe whose premise about the published record has stopped
being true.

What a run exits non-zero on is deliberately narrow. See
``chip_chat.eval.dietary.__main__``: a build that went red about an unmeasured
gate would be red about wiring rather than about the product, and a gate that is
red by construction is a gate somebody switches off.
"""

import json
from pathlib import Path

import pytest

from chip_chat.eval.dietary.__main__ import main
from chip_chat.eval.dietary.probes import DEFAULT_MANIFEST


def test_check_passes_on_the_shipped_set(repo_root: Path, capsys) -> None:
    """The command CI runs, against the manifest this repository commits."""
    assert (
        main(
            [
                "--check",
                "--probes",
                str(repo_root / DEFAULT_MANIFEST),
                "--hand",
                str(repo_root / "eval/dietary/hand-check.json"),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "probes" in out
    assert "MISSING" not in out


def test_check_says_the_premises_were_not_checked_without_a_catalogue(
    repo_root: Path, capsys
) -> None:
    """A probe can be stale and a run without ``--catalog`` cannot see it."""
    main(
        [
            "--check",
            "--probes",
            str(repo_root / DEFAULT_MANIFEST),
            "--hand",
            str(repo_root / "eval/dietary/hand-check.json"),
        ]
    )
    assert "no --catalog" in capsys.readouterr().out


def test_check_reports_the_premises_against_a_built_catalogue(
    repo_root: Path, capsys
) -> None:
    """Harvest to catalogue to premise, as one command."""
    assert (
        main(
            [
                "--check",
                "--probes",
                str(repo_root / DEFAULT_MANIFEST),
                "--hand",
                str(repo_root / "eval/dietary/hand-check.json"),
                "--catalog",
                str(repo_root / "catalog" / "tests" / "fixtures"),
            ]
        )
        == 0
    )
    assert "agree with the built catalogue" in capsys.readouterr().out


def test_check_says_the_hand_record_is_empty(repo_root: Path, capsys) -> None:
    """The honest state, said out loud rather than left to be inferred."""
    main(
        [
            "--check",
            "--probes",
            str(repo_root / DEFAULT_MANIFEST),
            "--hand",
            str(repo_root / "eval/dietary/hand-check.json"),
        ]
    )
    assert "holds no verdicts" in capsys.readouterr().out


def test_a_thin_set_exits_non_zero(tmp_path: Path, repo_root: Path) -> None:
    """A gap in what can be measured is a build failure, or it stays a gap."""
    manifest = tmp_path / "probes.json"
    shipped = json.loads((repo_root / DEFAULT_MANIFEST).read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({"probes": shipped["probes"][-1:]}), encoding="utf-8")
    assert (
        main(
            [
                "--check",
                "--probes",
                str(manifest),
                "--hand",
                str(repo_root / "eval/dietary/hand-check.json"),
            ]
        )
        == 1
    )


def test_an_unbelievable_manifest_exits_non_zero(tmp_path: Path) -> None:
    """Refused at load, never at score time."""
    manifest = tmp_path / "probes.json"
    manifest.write_text("not json", encoding="utf-8")
    assert main(["--check", "--probes", str(manifest)]) == 1


@pytest.mark.parametrize("flag", ["--probes", "--hand"])
def test_a_missing_file_exits_non_zero(tmp_path: Path, flag: str) -> None:
    """An unreadable register is not an empty one."""
    assert main(["--check", flag, str(tmp_path / "nothing.json")]) == 1


def test_the_ceiling_run_is_free_and_writes_a_document(tmp_path, repo_root) -> None:
    """The one to put in CI: no credentials, no tokens, and a real request path."""
    out = tmp_path / "BASELINE.md"
    assert (
        main(
            [
                "--ceiling",
                "--probes",
                str(repo_root / DEFAULT_MANIFEST),
                "--hand",
                str(repo_root / "eval/dietary/hand-check.json"),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    document = out.read_text(encoding="utf-8")
    assert "not measured" in document
    assert "ceiling, not a score" in document


def test_the_committed_baseline_is_the_one_the_ceiling_produces(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A baseline that has drifted from the code is a document about last month.

    Run from the repository root with the default paths, because the document
    quotes the manifest's path and an absolute one would put this machine's home
    directory in a committed file.
    """
    monkeypatch.chdir(repo_root)
    out = tmp_path / "BASELINE.md"
    assert main(["--ceiling", "--out", str(out)]) == 0
    committed = (repo_root / "eval/dietary/BASELINE.md").read_text(encoding="utf-8")
    assert out.read_text(encoding="utf-8") == committed
