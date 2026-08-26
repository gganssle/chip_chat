"""The weekly schedule lives in three files, so a test keeps them agreeing.

`.github/workflows/reharvest.yml` calls `make reharvest` with variables the
`Makefile` declares defaults for, and the staleness threshold it passes has to
be the one `chip_chat.harvest.freshness` calls default. Nothing connects the
three but numbers typed more than once, and the failure when they drift is the
worst kind: the job stays green, and the thing it exists to check is not being
checked. That is the same failure `test_local_stack.py` exists to prevent, for
the same reason.

It lives in `infra/` because a schedule is infrastructure, even though this one
is a workflow file rather than Terraform.
"""

import re
from pathlib import Path

from chip_chat.harvest.freshness import DEFAULT_MAX_AGE_DAYS
from chip_chat.harvest.sources.chipotle.reharvest import (
    DEFAULT_STORE_COUNT,
    build_parser,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "reharvest.yml"


def _make_default(variable: str) -> str:
    """Return the ``VAR ?= value`` default the Makefile declares for ``variable``."""
    match = re.search(rf"^{variable}\s*\?= *(.*)$", MAKEFILE.read_text(), re.MULTILINE)
    assert match is not None, f"{variable} has no default in the Makefile"
    return match.group(1).strip()


def test_the_workflow_is_where_the_schedule_is_supposed_to_be() -> None:
    assert WORKFLOW.is_file()


def test_it_runs_once_a_week() -> None:
    """Weekly is the whole ticket. A cron that drifted to daily would quadruple
    what a third party's servers are asked for and nothing else would say so."""
    crons = re.findall(r'- cron: "(.+)"', WORKFLOW.read_text())

    assert len(crons) == 1
    minute, hour, day_of_month, month, day_of_week = crons[0].split()
    assert day_of_month == "*"
    assert month == "*"
    assert day_of_week.isdigit(), "a weekly schedule names one day of the week"
    assert minute.isdigit()
    assert hour.isdigit()


def test_it_calls_the_makefile_target_rather_than_the_module() -> None:
    """One command, so the flags the schedule uses are the flags a developer
    reproduces a failure with."""
    assert "make reharvest" in WORKFLOW.read_text()
    assert "reharvest:" in MAKEFILE.read_text()


def test_the_variables_the_workflow_passes_are_ones_the_makefile_declares() -> None:
    workflow = WORKFLOW.read_text()

    for variable in ("LANDING", "STORES", "MAX_AGE_DAYS", "REPORT"):
        assert f"{variable}=" in workflow, f"the workflow does not pass {variable}"
        _make_default(variable)


def test_the_weekly_defaults_are_the_commands_own_defaults() -> None:
    """The workflow's dispatch inputs are what a human sees when they run it by
    hand, so a number there that disagreed with the command would be a lie in
    the one place someone reads before overriding it."""
    defaults = build_parser().parse_args(["--landing", "landing"])
    workflow = WORKFLOW.read_text()

    assert defaults.stores == DEFAULT_STORE_COUNT
    assert defaults.max_age_days == DEFAULT_MAX_AGE_DAYS
    assert f'default: "{DEFAULT_STORE_COUNT}"' in workflow
    assert f'default: "{DEFAULT_MAX_AGE_DAYS}"' in workflow
    assert f"|| {DEFAULT_STORE_COUNT} " in workflow
    assert f"|| {DEFAULT_MAX_AGE_DAYS} " in workflow


def test_the_landing_zone_survives_between_runs() -> None:
    """Without a restored cache every run is a cold start: nothing to diff
    against, and every conditional request becomes a download."""
    workflow = WORKFLOW.read_text()

    assert "actions/cache@v4" in workflow
    # restore-keys is what makes it a rolling cache rather than a write-once
    # one -- the key is unique per run so every run saves, and the prefix match
    # is what lets the next run restore the newest previous save.
    assert "restore-keys:" in workflow


def test_the_report_is_published_even_when_the_run_fails() -> None:
    """A weekly job whose only artefact when it breaks is a log line is a job
    whose failures nobody can compare."""
    workflow = WORKFLOW.read_text()

    assert "GITHUB_STEP_SUMMARY" in workflow
    assert workflow.count("if: always()") == 2


def test_two_re_harvests_cannot_run_at_once() -> None:
    """Two runs would share one landing zone and would not share a politeness
    gate, which is the one way this workflow could be rude to the source."""
    workflow = WORKFLOW.read_text()

    assert "group: reharvest" in workflow
    assert "cancel-in-progress: false" in workflow
