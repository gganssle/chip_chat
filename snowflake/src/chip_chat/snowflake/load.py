"""Put JSONL tables into the serving layer, one table at a time, atomically.

Issue #42's third acceptance criterion is that the schema is "loaded with
published data and queryable", and this is what loads it. It is the developer
path and it is deliberately not the nightly one: #39 publishes out of
Databricks on a schedule, from silver, with an alert attached to a failed run.
This reads the same tables in the same shape from a directory of ``.jsonl``
files -- which is what ``chip_chat.catalog`` and ``chip_chat.data_gen`` write
into a landing zone -- and puts them somewhere a query can reach them.

    python -m chip_chat.snowflake.load catalog/tests/fixtures/catalog
    python -m chip_chat.snowflake.load landing/catalog landing/accounts/synthetic
    python -m chip_chat.snowflake.load --plan landing/catalog

`make snowflake-load-sample` is the first of those with the path already right.

**A table is replaced, never appended to.** ``TRUNCATE`` and ``COPY INTO`` run
in one transaction per table, so a reader either sees the generation that was
there before or the one being landed, and never half of either. That is #39's
"idempotent and atomic per table" requirement, met here for the same reason it
is required there: a conversation querying mid-load must not see a catalogue
with no burritos in it.

**Column names are matched, not positions.** ``MATCH_BY_COLUMN_NAME`` means a
file carrying columns the serving table does not have -- and every one of these
files does, because CHIP_CHAT.CATALOGUE is a projection of silver rather than a
copy -- loads the columns that match and ignores the rest. A column the table
has and the file does not arrives null, which is why `verify` counts rows
afterwards rather than trusting a ``LOADED`` status.

**The account tables come from silver, not from the generator.**
``chip_chat.data_gen`` writes ``order_items`` without a ``demo_id`` -- the
generator keys a line by its order and leaves it at that -- and the silver
layer is what carries the visitor down onto the line
(``databricks.silver._ORDER_REFERENCE``). The serving table requires the
column, because a row access policy cannot follow a join to find out whose row
it is looking at. So this refuses the raw generator output for that one table,
by name, before it uploads anything: :func:`unfillable` is the check, and the
refusal is the schema doing its job rather than a gap in it.

**It runs on the publish warehouse, not the serving one.** A load is a batch,
and the two warehouses exist so that a batch cannot queue in front of a
conversation. It is also the only one whose statement timeout is long enough:
the serving warehouse cancels anything still running after sixty seconds,
because a turn that has not answered in a minute has already failed as a
conversation. Fifty thousand order lines is a minute of work, and the first
version of this module found that out by having a ``TRUNCATE`` cancelled.

**It loads as CHIP_CHAT_ADMIN.** Not as CHIP_CHAT_PUBLISH, which is the
identity #39 will use: the publish role cannot see ACCOUNTS at all, on purpose,
so it cannot land the synthetic account tables and was never meant to. The
account data reaches Snowflake by another route in the deployed system, and on
a laptop it reaches it through the role that owns the database.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from chip_chat.snowflake import account, schema, snow

__all__ = ["STAGE", "Loaded", "load", "main", "sources", "unfillable"]

STAGE = "_LOAD_STAGE"
"""The internal stage each schema gets, created on demand and left in place.

A stage holds no rows and costs nothing to keep; recreating it per run would
mean a ``CREATE`` in every load and a stage nobody could look inside afterwards
to see what was actually uploaded.
"""


@dataclass(frozen=True, slots=True)
class Loaded:
    """What one table's load did.

    Attributes:
        table: The table, database-qualified.
        path: The file it was loaded from.
        rows: How many rows the table holds afterwards. Counted rather than
            read off the ``COPY`` result, because a file whose column names do
            not match the table loads successfully and lands nothing.
    """

    table: str
    path: Path
    rows: int


def sources(directories: list[Path]) -> list[tuple[schema.Table, Path]]:
    """Return the ``(table, file)`` pairs to load, in :data:`schema.TABLES` order.

    A directory may hold files for tables in more than one schema, and usually
    does not hold files for all of them: `catalog/tests/fixtures/catalog`
    carries the four catalogue tables and none of the account ones.

    Args:
        directories: Where to look for ``<table>.jsonl``.

    Returns:
        One pair per table that has a file, ordered so that a table is loaded
        after anything its foreign keys point at.

    Raises:
        FileNotFoundError: If a directory does not exist, or if none of them
            holds a file for any known table -- which is almost always a path
            typed wrong rather than an empty landing zone.
    """
    for directory in directories:
        if not directory.is_dir():
            raise FileNotFoundError(f"{directory} does not exist")

    found: list[tuple[schema.Table, Path]] = []
    for table in schema.TABLES:
        for directory in directories:
            path = directory / f"{table.name}.jsonl"
            if path.is_file():
                found.append((table, path))
                break
    if not found:
        raise FileNotFoundError(
            f"no <table>.jsonl for any table of {account.DATABASE} under "
            f"{', '.join(str(directory) for directory in directories)}"
        )
    return found


def unfillable(table: schema.Table, path: Path) -> list[str]:
    """Return the ``NOT NULL`` columns of ``table`` that ``path`` does not carry.

    Read off the first line rather than the whole file: these are JSONL exports
    of a dataclass, so every line has the same keys, and the interesting failure
    is a file in the wrong *shape* rather than one bad row among fifty thousand.

    Without this, a file missing a required column uploads, copies, and fails
    with ``100072 (22000): NULL result in a non-nullable column`` -- which does
    not say which column, which table, or which file.

    Args:
        table: The declared table.
        path: The ``.jsonl`` file.

    Returns:
        The missing required column names, sorted. Empty if the file has them
        all, and empty for an empty file, which loads to nothing and says so.
    """
    with path.open(encoding="utf-8") as handle:
        first = handle.readline().strip()
    if not first:
        return []
    keys = {str(key).lower() for key in json.loads(first)}
    return sorted(
        column.name
        for column in table.columns
        if column.required and column.name.lower() not in keys
    )


def _count(table: schema.Table) -> int:
    """Return how many rows ``table`` holds."""
    rows = snow.query(
        f"USE ROLE {account.ADMIN_ROLE};\n"
        f"USE WAREHOUSE {account.PUBLISH_WAREHOUSE};\n"
        f"SELECT COUNT(*) AS n FROM {table.qualified()};"
    )[-1]
    return int(next(iter(rows[0].values()))) if rows else 0


def load(table: schema.Table, path: Path) -> Loaded:
    """Replace ``table``'s contents with ``path``'s rows.

    Args:
        table: The declared table.
        path: A ``.jsonl`` file, one JSON object per line.

    Returns:
        What the table holds afterwards.

    Raises:
        snow.SnowError: If the upload or the copy failed. The transaction is
            what makes that safe: a failed ``COPY`` rolls the ``TRUNCATE`` back
            with it, so a failed load leaves the previous generation in place
            rather than an empty table.
    """
    missing = unfillable(table, path)
    if missing:
        raise snow.SnowError(
            f"{path} cannot fill {table.qualified()}",
            f"it carries no {', '.join(missing)}, and the table requires "
            "them.\nIf this is a data-gen export, order_items is the expected "
            "one: the generator\nleaves demo_id off an order line and the "
            "silver layer carries it down from the\norder. The serving layer "
            "needs it on the line itself, because #43's row access\npolicy "
            "filters one table against a session variable and cannot follow a "
            "join.\nLoad the conformed tables rather than the raw ones.",
        )

    stage = f"{account.schema(table.schema)}.{STAGE}"
    result = snow.run_statements(
        f"USE ROLE {account.ADMIN_ROLE};\n"
        f"USE WAREHOUSE {account.PUBLISH_WAREHOUSE};\n"
        f"CREATE STAGE IF NOT EXISTS {stage} FILE_FORMAT = (TYPE = JSON);\n"
        f"PUT file://{path.resolve()} @{stage}/{table.name}/ "
        "AUTO_COMPRESS = TRUE OVERWRITE = TRUE;\n"
        "BEGIN;\n"
        f"TRUNCATE TABLE {table.qualified()};\n"
        f"COPY INTO {table.qualified()} FROM @{stage}/{table.name}/\n"
        "  FILE_FORMAT = (TYPE = JSON)\n"
        "  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE\n"
        "  ON_ERROR = ABORT_STATEMENT;\n"
        "COMMIT;"
    )
    if not result.ok:
        raise snow.SnowError(f"loading {table.name} failed", result.output)
    return Loaded(table=table.qualified(), path=path, rows=_count(table))


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.snowflake.load",
        description="Load JSONL tables into the Chip Chat serving layer.",
    )
    parser.add_argument(
        "directories",
        nargs="+",
        type=Path,
        help="directories holding <table>.jsonl, e.g. landing/catalog",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="print which file would land in which table, and load nothing",
    )
    arguments = parser.parse_args(argv)

    try:
        pairs = sources(arguments.directories)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2

    if arguments.plan:
        for table, path in pairs:
            print(f"{path} -> {table.qualified()}")
        return 0

    loaded: list[Loaded] = []
    try:
        for table, path in pairs:
            print(f"→ {table.qualified()}")
            outcome = load(table, path)
            loaded.append(outcome)
            print(f"  {outcome.rows} rows from {path.name}")
    except snow.SnowError as error:
        print(error, file=sys.stderr)
        return 1

    empty = [outcome.table for outcome in loaded if outcome.rows == 0]
    print(f"\n{len(loaded)} tables loaded, {sum(o.rows for o in loaded)} rows.")
    if empty:
        print(
            "these landed nothing, which usually means the file's column names "
            "are not the table's:\n  " + "\n  ".join(empty),
            file=sys.stderr,
        )
        return 1
    print("`make snowflake-verify` re-runs #42's checks against what is there now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
