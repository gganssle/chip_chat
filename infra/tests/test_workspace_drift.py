"""The drift check, asserted without a workspace.

Everything here runs offline. The half that reads the Terraform is exercised
against the real ``infra/terraform`` — the same approach ``test_registry.py``
and ``test_local_stack.py`` take, and for the same reason: the configuration is
the thing worth checking before an apply rather than after one. The half that
talks to Databricks is exercised against fixtures, with ``export`` stubbed, so
that the diff logic and the exit codes are covered by ``make ci`` even though
the target itself never can be.

What is deliberately *not* covered here is the live run: ``export`` really
shelling out to ``databricks workspace export`` against the real workspace needs
a credential, and CLAUDE.md's rule is that nothing needing one is in the gate.
``docs/workspace-drift.md`` §5 records what the live run did when it was
performed by hand, and that record is the only evidence for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chip_chat.infra.workspace_drift import (
    DEFAULT_BASE,
    EXIT_CLEAN,
    EXIT_DRIFTED,
    EXIT_ERROR,
    EXPECTED_NOTEBOOKS,
    EXPECTED_WORKSPACE_FILES,
    Comparison,
    DriftCheckError,
    ManagedPath,
    check_base_assumption,
    check_counts,
    compare,
    main,
    managed_files,
    terraform_locals,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = REPO_ROOT / "infra" / "terraform"
MAKEFILE = REPO_ROOT / "Makefile"


@pytest.fixture
def managed() -> list[ManagedPath]:
    return managed_files(TERRAFORM, DEFAULT_BASE)


# --- The list comes from the Terraform, not from a constant -------------------


def test_the_lib_path_locals_resolve_through_their_aliases() -> None:
    # Five files declare a `*_lib_path` local and four of them are aliases of
    # `bronze_lib_path`. If the aliases stopped resolving, the modules would be
    # looked for under a path with a literal `${local.…}` in it and every one of
    # them would report as not deployed.
    scope = terraform_locals(TERRAFORM, DEFAULT_BASE)
    lib_paths = {key: value for key, value in scope.items() if key.endswith("_lib_path")}
    assert lib_paths, "no *_lib_path locals were resolved at all"
    assert set(lib_paths.values()) == {f"/Shared/{DEFAULT_BASE}/lib"}


def test_every_managed_resource_in_the_terraform_is_found(
    managed: list[ManagedPath],
) -> None:
    kinds = [m.kind for m in managed]
    assert kinds.count("databricks_workspace_file") == EXPECTED_WORKSPACE_FILES
    assert kinds.count("databricks_notebook") == EXPECTED_NOTEBOOKS


def test_the_module_that_caused_the_bug_is_one_of_them(
    managed: list[ManagedPath],
) -> None:
    # cc-rxs: /Shared/chip-chat/lib/publish.py sat 37 lines behind the repository
    # and the nightly publish failed with a message about a row access policy.
    # This is the path whose absence from the list would make the whole check a
    # decoration.
    publish = [m for m in managed if m.workspace_path.endswith("/lib/publish.py")]
    assert len(publish) == 1
    assert publish[0].address == "databricks_workspace_file.publish_module"
    assert publish[0].repo_path == (
        REPO_ROOT / "databricks" / "src" / "chip_chat" / "databricks" / "publish.py"
    )


def test_the_notebook_that_matched_the_stale_module_is_too(
    managed: list[ManagedPath],
) -> None:
    notebook = [m for m in managed if m.workspace_path.endswith("/snowflake_publish")]
    assert len(notebook) == 1
    assert notebook[0].kind == "databricks_notebook"
    assert (
        notebook[0].repo_path
        == REPO_ROOT / "databricks" / "notebooks" / "snowflake_publish.py"
    )


def test_every_derived_source_file_is_actually_in_the_checkout(
    managed: list[ManagedPath],
) -> None:
    # A `source` pointing at a file nobody has committed is an apply that fails,
    # and it is the one class of drift this check would otherwise only discover
    # against a live workspace.
    missing = [m.address for m in managed if not m.repo_path.is_file()]
    assert not missing, f"managed resources point at files that do not exist: {missing}"


def test_no_two_resources_claim_the_same_workspace_path(
    managed: list[ManagedPath],
) -> None:
    paths = [m.workspace_path for m in managed]
    assert len(paths) == len(set(paths))


def test_the_base_prefix_is_still_what_locals_computes() -> None:
    # The one value not derived from the source. This is the check that keeps it
    # honest; see the module docstring.
    assert check_base_assumption(TERRAFORM, DEFAULT_BASE) is None


def test_a_non_default_base_is_not_second_guessed() -> None:
    assert check_base_assumption(TERRAFORM, "chip-chat-scratch") is None


def test_the_base_flows_into_every_derived_path() -> None:
    scratch = managed_files(TERRAFORM, "chip-chat-scratch")
    assert scratch, "no resources were derived for a non-default stack"
    assert all(m.workspace_path.startswith("/Shared/chip-chat-scratch/") for m in scratch)


# --- The tripwire on the count ------------------------------------------------


def test_the_real_terraform_passes_its_own_tripwire(managed: list[ManagedPath]) -> None:
    check_counts(managed)


def test_a_short_list_is_a_hard_failure_rather_than_a_clean_run(
    managed: list[ManagedPath],
) -> None:
    # The failure mode the tripwire exists for: a parser that quietly stops
    # matching reports no drift, and no drift is what a healthy workspace
    # reports. The two must not look the same.
    with pytest.raises(DriftCheckError, match="does not declare the number"):
        check_counts(managed[:3])


def test_the_failure_names_both_explanations(managed: list[ManagedPath]) -> None:
    with pytest.raises(DriftCheckError) as caught:
        check_counts(managed, expect_workspace_files=99)
    message = str(caught.value)
    assert "EXPECTED_WORKSPACE_FILES" in message
    assert "docs/workspace-drift.md" in message
    assert "stopped recognising" in message


# --- What the parser refuses rather than skips --------------------------------


def _terraform_fixture(tmp_path: Path, body: str) -> Path:
    directory = tmp_path / "terraform"
    directory.mkdir()
    (directory / "databricks_fixture.tf").write_text(body, encoding="utf-8")
    return directory


def test_a_count_meta_argument_stops_the_run(tmp_path: Path) -> None:
    # A resource behind `count` manages files this parser cannot name. Skipping
    # it would mean checking fewer paths than the operator believes; the run
    # stops instead.
    directory = _terraform_fixture(
        tmp_path,
        'resource "databricks_notebook" "conditional" {\n'
        "  count    = var.enabled ? 1 : 0\n"
        '  path     = "/Shared/x/y"\n'
        '  source   = "${path.module}/../../databricks/notebooks/y.py"\n'
        "}\n",
    )
    with pytest.raises(DriftCheckError, match="uses `count`"):
        managed_files(directory)


def test_an_interpolation_it_cannot_evaluate_stops_the_run(tmp_path: Path) -> None:
    directory = _terraform_fixture(
        tmp_path,
        'resource "databricks_notebook" "varied" {\n'
        '  path     = "/Shared/${var.environment}/y"\n'
        '  source   = "${path.module}/y.py"\n'
        "}\n",
    )
    with pytest.raises(DriftCheckError, match="cannot evaluate"):
        managed_files(directory)


def test_an_unresolved_local_stops_the_run(tmp_path: Path) -> None:
    directory = _terraform_fixture(
        tmp_path,
        'resource "databricks_notebook" "elsewhere" {\n'
        '  path     = "${local.somewhere_else}/y"\n'
        '  source   = "${path.module}/y.py"\n'
        "}\n",
    )
    with pytest.raises(DriftCheckError, match=r"local\.somewhere_else"):
        managed_files(directory)


def test_a_directory_with_no_databricks_terraform_stops_the_run(tmp_path: Path) -> None:
    empty = tmp_path / "terraform"
    empty.mkdir()
    with pytest.raises(DriftCheckError, match=r"no databricks_\*\.tf files"):
        managed_files(empty)


# --- The diff -----------------------------------------------------------------


def _managed(
    repo_path: Path, workspace_path: str = "/Shared/chip-chat/lib/publish.py"
) -> ManagedPath:
    return ManagedPath(
        kind="databricks_workspace_file",
        address="databricks_workspace_file.publish_module",
        declared_in="databricks_publish.tf",
        workspace_path=workspace_path,
        repo_path=repo_path,
    )


def test_identical_content_is_not_drift(tmp_path: Path) -> None:
    source = tmp_path / "publish.py"
    source.write_text("def row_count() -> str:\n    return 'x'\n", encoding="utf-8")
    result = compare(_managed(source), source.read_text(encoding="utf-8"), tmp_path)
    assert not result.drifted
    assert result.diff == ""


def test_the_bug_this_exists_for_is_reported_with_a_real_diff(tmp_path: Path) -> None:
    # cc-rxs in miniature: the deployed module lacks the function the committed
    # one added, and the operator needs to see which function.
    source = tmp_path / "publish.py"
    source.write_text(
        "def swap():\n    ...\n\n\ndef row_count():\n    ...\n", encoding="utf-8"
    )
    result = compare(_managed(source), "def swap():\n    ...\n", tmp_path)
    assert result.drifted
    assert "DRIFTED" in result.summary
    assert "deployed 2 lines, committed 6 lines" in result.summary
    assert "+def row_count():" in result.diff
    assert "deployed /Shared/chip-chat/lib/publish.py" in result.diff
    assert "committed publish.py" in result.diff


def test_a_path_the_workspace_does_not_have_is_drift_not_an_error(tmp_path: Path) -> None:
    # The apply never ran at all. This is the most consequential finding the
    # check can make and it must not be confused with the CLI failing.
    source = tmp_path / "publish.py"
    source.write_text("x = 1\n", encoding="utf-8")
    result = compare(_managed(source), None, tmp_path)
    assert result.drifted
    assert "NOT DEPLOYED" in result.summary


def test_a_source_file_missing_from_the_checkout_is_drift(tmp_path: Path) -> None:
    result = compare(_managed(tmp_path / "gone.py"), "x = 1\n", tmp_path)
    assert result.drifted
    assert "not in this checkout" in result.summary


def test_windows_line_endings_are_not_a_twenty_four_page_diff(tmp_path: Path) -> None:
    source = tmp_path / "publish.py"
    source.write_text("a = 1\nb = 2\n", encoding="utf-8")
    result = compare(_managed(source), "a = 1\r\nb = 2\r\n", tmp_path)
    assert not result.drifted


# --- The command --------------------------------------------------------------


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    """A fake workspace: path -> deployed content, or None for "not deployed"."""
    contents: dict[str, str | None] = {}

    def fake_export(
        workspace_path: str,
        databricks: str = "databricks",
        profile: str | None = None,
        timeout: float = 60.0,
    ) -> str | None:
        return contents[workspace_path]

    monkeypatch.setattr("chip_chat.infra.workspace_drift.export", fake_export)
    return contents


def _seed_from_the_checkout(contents: dict[str, str | None]) -> list[ManagedPath]:
    found = managed_files(TERRAFORM, DEFAULT_BASE)
    for entry in found:
        contents[entry.workspace_path] = entry.repo_path.read_text(encoding="utf-8")
    return found


def test_a_matching_workspace_exits_zero_and_says_one_line(
    workspace: dict[str, str | None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_from_the_checkout(workspace)
    assert main([]) == EXIT_CLEAN
    out = capsys.readouterr().out
    assert out.strip().startswith("24 Terraform-managed paths")
    assert len(out.strip().splitlines()) == 1


def test_quiet_means_quiet(
    workspace: dict[str, str | None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_from_the_checkout(workspace)
    assert main(["--quiet"]) == EXIT_CLEAN
    assert capsys.readouterr().out == ""


def test_one_stale_module_exits_one_and_prints_its_diff(
    workspace: dict[str, str | None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found = _seed_from_the_checkout(workspace)
    stale = next(m for m in found if m.workspace_path.endswith("/lib/publish.py"))
    committed = workspace[stale.workspace_path]
    assert committed is not None
    workspace[stale.workspace_path] = committed.replace(
        "def row_count", "def gone_row_count"
    )

    assert main([]) == EXIT_DRIFTED
    out = capsys.readouterr().out
    assert "/Shared/chip-chat/lib/publish.py: DRIFTED" in out
    assert "-def gone_row_count" in out
    assert "1 of 24 managed paths differ" in out
    # The operator is told how to repair it both ways, because the phone form
    # and the laptop form are different commands. docs/runbook.md §1.
    assert "make infra-apply" in out
    assert "databricks workspace import --overwrite" in out


def test_a_path_that_was_never_applied_exits_one(
    workspace: dict[str, str | None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found = _seed_from_the_checkout(workspace)
    absent = next(m for m in found if m.workspace_path.endswith("/snowflake_publish"))
    workspace[absent.workspace_path] = None

    assert main([]) == EXIT_DRIFTED
    assert "NOT DEPLOYED" in capsys.readouterr().out


def test_only_the_drifted_paths_are_printed(
    workspace: dict[str, str | None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    found = _seed_from_the_checkout(workspace)
    stale = next(m for m in found if m.workspace_path.endswith("/lib/publish.py"))
    committed = workspace[stale.workspace_path]
    assert committed is not None
    workspace[stale.workspace_path] = committed + "\n# stale\n"

    main([])
    out = capsys.readouterr().out
    assert "/lib/silver.py" not in out
    assert "/lib/publish.py" in out


def test_listing_the_managed_paths_needs_no_workspace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No `workspace` fixture here: --list must not reach for the CLI at all, so
    # a stubbed export is not in place and a real call would try to run it.
    assert main(["--list"]) == EXIT_CLEAN
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == EXPECTED_WORKSPACE_FILES + EXPECTED_NOTEBOOKS
    assert all(line.count("\t") == 2 for line in lines)


def test_a_broken_count_expectation_exits_two_before_any_export(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Exit 2 rather than 1: the check could not be performed, which is a
    # different thing from the workspace being wrong, and a caller that treats
    # them the same will eventually treat a broken check as a broken workspace.
    assert main(["--expect-notebooks", "99"]) == EXIT_ERROR
    assert "cannot check for drift" in capsys.readouterr().err


def test_a_terraform_directory_that_is_not_one_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--terraform-dir", str(tmp_path)]) == EXIT_ERROR
    assert "cannot check for drift" in capsys.readouterr().err


def test_the_three_exit_codes_are_distinct() -> None:
    assert len({EXIT_CLEAN, EXIT_DRIFTED, EXIT_ERROR}) == 3


# --- Where the target is allowed to live --------------------------------------


def test_the_makefile_has_the_target_and_it_is_not_in_the_gate() -> None:
    # CLAUDE.md: "Nothing that costs money or needs a credential is in `make ci`,
    # and that is a rule rather than an oversight." This one needs a Databricks
    # credential, so it belongs beside `infra-check-uploads` and nowhere near
    # `ci:`. Read as text, the way this package already reads the Terraform.
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "infra-check-databricks:" in text
    assert "chip_chat.infra.workspace_drift" in text

    ci_line = next(line for line in text.splitlines() if line.startswith("ci:"))
    prerequisites = ci_line.split("##")[0].removeprefix("ci:").split()
    assert "infra-check-databricks" not in prerequisites


def test_the_help_text_says_it_needs_a_credential() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    help_line = next(
        line for line in text.splitlines() if line.startswith("infra-check-databricks:")
    )
    assert "credential" in help_line.lower()


def test_the_runbook_writes_the_procedure_twice() -> None:
    # docs/runbook.md §1: every procedure is the raw command with the names
    # spelled out, and the `make` target beside it, because the raw form is the
    # one that works from a phone.
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    assert "make infra-check-databricks" in runbook
    assert "chip_chat.infra.workspace_drift" in runbook


def test_the_comparison_dataclass_carries_its_diff_only_when_it_has_one() -> None:
    empty = Comparison(
        managed=_managed(Path("/nowhere/publish.py")),
        drifted=False,
        summary="ok",
    )
    assert empty.diff == ""
