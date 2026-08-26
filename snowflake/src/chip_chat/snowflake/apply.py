"""Rebuild the Snowflake account from `snowflake/sql/`.

Issue #41's fourth acceptance criterion: "entire account rebuildable from
`snowflake/` in one run". This is the one run. It executes the numbered files in
`snowflake/sql/` in order and stops at the first statement that fails.

Files in `snowflake/sql/optional/` are never run by an apply. There are two of
them and both want a decision: `network_policy.sql` needs egress addresses that
nobody can guess, and `reset.sql` drops the database. ``--reset`` is how you ask
for the second one on purpose.

Every numbered file is re-runnable. Nothing uses ``CREATE OR REPLACE`` on an
object that holds either data or a credential -- roles keep their grants,
warehouses keep their ``USAGE`` grants, the database keeps its tables, and users
keep the key pairs an operator attached. What re-running *does* do is re-assert
every warehouse property, so a setting somebody widened in the UI is narrowed
again by the next apply. That asymmetry is the design: an apply may tighten and
may create, and may not destroy.

    python -m chip_chat.snowflake.apply             # create or re-assert
    python -m chip_chat.snowflake.apply --plan      # print the order, run nothing
    python -m chip_chat.snowflake.apply --reset --yes   # tear down, then build

`make snowflake-apply` and `make snowflake-rebuild` are the same two commands
with the arguments already right.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from chip_chat.snowflake import snow

__all__ = ["SQL_DIRECTORY", "apply", "main", "ordered_files", "reset"]

SQL_DIRECTORY = Path(__file__).resolve().parents[3] / "sql"
"""`snowflake/sql/`, found from this file rather than from the caller's cwd."""

RESET_FILE = SQL_DIRECTORY / "optional" / "reset.sql"


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
