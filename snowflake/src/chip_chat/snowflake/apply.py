"""Rebuild the Snowflake account from `snowflake/sql/`.

Issue #41's fourth acceptance criterion: "entire account rebuildable from
`snowflake/` in one run". This is the one run. It executes the numbered files in
`snowflake/sql/` in order and stops at the first statement that fails.

Files in `snowflake/sql/optional/` are never run by an apply. There are three of
them and all three want a decision: `network_policy.sql` needs egress addresses
that nobody can guess, `trial_credit_cap.sql` needs a credit quota read off the
remaining balance, and `reset.sql` drops the database. ``--cap`` and ``--reset``
are how you ask for the last two on purpose.

Every numbered file is re-runnable. Nothing uses ``CREATE OR REPLACE`` on an
object that holds either data or a credential -- roles keep their grants,
warehouses keep their ``USAGE`` grants, the database keeps its tables, and users
keep the key pairs an operator attached. What re-running *does* do is re-assert
every warehouse property, so a setting somebody widened in the UI is narrowed
again by the next apply. That asymmetry is the design: an apply may tighten and
may create, and may not destroy.

    python -m chip_chat.snowflake.apply             # create or re-assert
    python -m chip_chat.snowflake.apply --plan      # print the order, run nothing
    python -m chip_chat.snowflake.apply --cap 60    # cap the whole trial at 60 credits
    python -m chip_chat.snowflake.apply --reset --yes   # tear down, then build

`make snowflake-apply`, `make snowflake-cap` and `make snowflake-rebuild` are the
same commands with the arguments already right.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chip_chat.snowflake import account, snow

__all__ = [
    "SQL_DIRECTORY",
    "apply",
    "cap",
    "credits_used",
    "main",
    "monitor_credits_used",
    "ordered_files",
    "reset",
]

SQL_DIRECTORY = Path(__file__).resolve().parents[3] / "sql"
"""`snowflake/sql/`, found from this file rather than from the caller's cwd."""

RESET_FILE = SQL_DIRECTORY / "optional" / "reset.sql"
CAP_FILE = SQL_DIRECTORY / "optional" / "trial_credit_cap.sql"

CREDITS_USED_QUERY = (
    "SELECT ROUND(SUM(credits_used), 1) AS USED "
    "FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY;"
)
"""Every credit the account has spent since the trial opened.

ACCOUNT_USAGE lags by up to two hours, so this is a floor rather than a figure.
A floor is all :func:`cap` needs: it is looking for a quota that is already
behind reality, and a lagging number can only make that check more permissive,
never falsely alarming.
"""


def ordered_files() -> list[Path]:
    """Return the numbered SQL files in the order an apply runs them.

    The numeric prefixes are load-bearing rather than decorative: roles have to
    exist before a warehouse can be granted to one, and the database before its
    grants. Sorting by filename is what encodes that, so a new file joins the
    sequence by being named into it.

    Raises:
        FileNotFoundError: If `snowflake/sql/` is missing, which means this is
            running from a tree that does not contain the account definition.
    """
    if not SQL_DIRECTORY.is_dir():
        raise FileNotFoundError(f"{SQL_DIRECTORY} does not exist")
    return sorted(path for path in SQL_DIRECTORY.glob("*.sql") if path.is_file())


def apply(files: list[Path]) -> None:
    """Run each of ``files`` in turn, printing what it did.

    Raises:
        snow.SnowError: On the first statement that fails. Earlier files have
            already been applied, and re-running is safe.
    """
    for path in files:
        print(f"→ {path.name}")
        snow.run_file(path)
        print(f"  applied {path.name}")


def credits_used() -> float | None:
    """Return the credits the account has spent to date, or None if unreadable.

    Read from ``SNOWFLAKE.ACCOUNT_USAGE``, which needs a role that can see the
    shared SNOWFLAKE database -- ACCOUNTADMIN does, and an operator's user holds
    it. A trial account that has not yet been billed for anything returns null
    rather than zero, and a view that cannot be read at all is not a reason to
    refuse to set a cap: both come back as None and :func:`cap` says so instead
    of guessing.
    """
    try:
        statements = snow.query(f"USE ROLE ACCOUNTADMIN;\n{CREDITS_USED_QUERY}")
    except snow.SnowError:
        return None
    rows = statements[-1] if statements else []
    if not rows:
        return None
    value = rows[0].get("USED")
    return None if value is None else float(value)


def monitor_credits_used(name: str) -> float | None:
    """Return what ``name`` has counted so far, or None if it does not exist yet.

    The quota is counted from the moment the monitor is created, not from the
    start of the trial, so this -- and not :func:`credits_used` -- is the number
    a new quota has to clear on a re-run.
    """
    try:
        statements = snow.query(
            f"USE ROLE ACCOUNTADMIN;\nSHOW RESOURCE MONITORS LIKE '{name}';"
        )
    except snow.SnowError:
        return None
    rows = statements[-1] if statements else []
    if not rows:
        return None
    value = rows[0].get("used_credits")
    return None if value is None else float(value)


def cap(quota: int) -> None:
    """Cap the whole account at ``quota`` credits, counted from now.

    This is `sql/optional/trial_credit_cap.sql`, which the numbered apply does
    not run: the number comes from the remaining balance rather than from the
    shape of the workload, so it belongs to an operator. What this adds around
    the file is the one sanity check that file cannot make for itself -- a quota
    the monitor has already counted past suspends every warehouse in the account
    the instant it is set, in the middle of whatever was running.

    Args:
        quota: Credits. ``make snowflake-cap QUOTA=<credits>`` passes it through.

    Raises:
        ValueError: If ``quota`` is not positive, or is at or below what an
            existing CHIP_CHAT_TRIAL_MONITOR has already counted.
        snow.SnowError: If the file fails to apply.
    """
    if quota <= 0:
        raise ValueError(f"a credit quota of {quota} suspends the account at once")

    spent = credits_used()
    already = monitor_credits_used(account.TRIAL_MONITOR)
    if already is not None and quota <= already:
        raise ValueError(
            f"{account.TRIAL_MONITOR} has already counted {already:.1f} credits, "
            f"so a quota of {quota} would suspend every warehouse in the account "
            "the moment it is set -- including whatever conversation is running. "
            "Raise the quota, or drop the monitor to start the count again:\n"
            "  ALTER ACCOUNT UNSET RESOURCE_MONITOR;\n"
            f"  DROP RESOURCE MONITOR {account.TRIAL_MONITOR};"
        )

    print(f"→ {CAP_FILE.name} (account-wide, {quota} credits)")
    snow.run_file(CAP_FILE, {"trial_credit_quota": str(quota)})
    print(f"  the account suspends at {quota} credits, counted from now")
    if already is not None:
        print(f"  {account.TRIAL_MONITOR} has counted {already:.1f} of them so far")
    if spent is not None:
        print(f"  the trial has spent {spent:.1f} credits in total since it opened")
    else:
        print(
            "  SNOWFLAKE.ACCOUNT_USAGE could not be read, so the credits spent to "
            "date are unknown here. Snowsight -> Admin -> Cost Management has them"
        )


def reset() -> None:
    """Drop everything `snowflake/sql/` creates.

    Raises:
        snow.SnowError: If the teardown fails.
    """
    print(f"→ {RESET_FILE.name} (destructive)")
    snow.run_file(RESET_FILE)
    print("  the account is back to the morning of the trial")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.snowflake.apply",
        description="Build the Chip Chat Snowflake account from checked-in SQL.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="print the files in the order they would run, and run nothing",
    )
    parser.add_argument(
        "--cap",
        type=int,
        metavar="CREDITS",
        help=(
            "set the account-wide credit cap to CREDITS and run nothing else. "
            "The number comes from the remaining balance, which is why no file "
            "here has a default for it"
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop the database, roles, warehouses and users first. Destructive",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required with --reset. There is no prompt; this is the consent",
    )
    arguments = parser.parse_args(argv)

    files = ordered_files()

    if arguments.plan:
        for path in files:
            print(path.relative_to(SQL_DIRECTORY.parent))
        return 0

    if arguments.cap is not None:
        try:
            cap(arguments.cap)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        except snow.SnowError as error:
            print(error, file=sys.stderr)
            return 1
        return 0

    if arguments.reset and not arguments.yes:
        print(
            "--reset drops CHIP_CHAT and every table in it, including whatever "
            "demo data is\nthere now. Snowflake's DROP is a soft delete and "
            "UNDROP DATABASE CHIP_CHAT works\nfor one day on this account. Pass "
            "--yes if that is what you want.",
            file=sys.stderr,
        )
        return 2

    try:
        if arguments.reset:
            reset()
        apply(files)
    except snow.SnowError as error:
        print(error, file=sys.stderr)
        return 1

    print(
        f"\n{len(files)} files applied. "
        "`make snowflake-verify` checks the account against issue #41."
    )
    if arguments.reset:
        print(
            f"A reset dropped {account.TRIAL_MONITOR} and the numbered files do "
            "not put it back, so the account is uncapped until you re-run\n"
            "  make snowflake-cap QUOTA=<credits>\n"
            "with a number read off the remaining balance. Verify fails on it by "
            "name until you do."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
