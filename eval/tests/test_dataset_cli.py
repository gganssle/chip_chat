"""The one command #72 asks for, and the gate it puts on the repository.

``--check`` is free and CI runs it, so what it exits with is a design decision
rather than an implementation detail: a stale committed build is a build failure
here, because a version that only moves when somebody remembers to regenerate a
file is not a version.
"""

import json
from pathlib import Path

from chip_chat.eval.dataset.__main__ import main
from chip_chat.eval.dataset.build import DEFAULT_BUILD, Dataset, document

_CATALOG = Path(__file__).resolve().parents[2] / "catalog" / "tests" / "fixtures"


def _shipped(repo_root: Path) -> list[str]:
    """The arguments that point the command at the committed manifests."""
    return [
        "--golden",
        str(repo_root / "eval" / "golden" / "cases.json"),
        "--photos",
        str(repo_root / "eval" / "photos" / "labels.json"),
        "--build",
        str(repo_root / DEFAULT_BUILD),
    ]


def test_check_passes_against_the_repository(repo_root: Path) -> None:
    assert main(["--check", *_shipped(repo_root)]) == 0


def test_check_passes_with_the_menu_terms_checked_too(repo_root: Path) -> None:
    """The staleness detector both sets already have, applied before an upload."""
    assert main(["--check", *_shipped(repo_root), "--catalog", str(_CATALOG)]) == 0


def test_a_stale_committed_build_fails_the_check(tmp_path: Path, repo_root: Path) -> None:
    """Adding a case without rebuilding is the failure this catches."""
    stale = tmp_path / "DATASET.json"
    stale.write_text('{"version": "0"}\n', encoding="utf-8")
    arguments = _shipped(repo_root)
    arguments[arguments.index("--build") + 1] = str(stale)

    assert main(["--check", *arguments]) == 1


def test_a_missing_committed_build_fails_the_check(
    tmp_path: Path, repo_root: Path
) -> None:
    arguments = _shipped(repo_root)
    arguments[arguments.index("--build") + 1] = str(tmp_path / "absent.json")

    assert main(["--check", *arguments]) == 1


def test_write_produces_the_committed_document(
    tmp_path: Path, repo_root: Path, shipped: Dataset
) -> None:
    """``make dataset`` and the test that holds the repository to it agree."""
    written = tmp_path / "nested" / "DATASET.json"
    arguments = _shipped(repo_root)
    arguments[arguments.index("--build") + 1] = str(written)

    assert main(["--write", *arguments]) == 0
    assert written.read_text(encoding="utf-8") == document(shipped)


def test_an_unreadable_manifest_fails(tmp_path: Path, repo_root: Path) -> None:
    arguments = _shipped(repo_root)
    arguments[arguments.index("--golden") + 1] = str(tmp_path / "absent.json")

    assert main(["--check", *arguments]) == 1


def test_a_manifest_leaning_on_a_term_the_menu_lost_fails(
    tmp_path: Path, repo_root: Path
) -> None:
    """A dataset uploaded against a menu that has moved is a dataset nobody can pass."""
    manifest = json.loads(
        (repo_root / "eval" / "golden" / "cases.json").read_text(encoding="utf-8")
    )
    manifest["cases"][0]["menu_terms"] = ["barbacoa poutine"]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    arguments = _shipped(repo_root)
    arguments[arguments.index("--golden") + 1] = str(path)

    assert main(["--check", *arguments, "--catalog", str(_CATALOG)]) == 1
