"""The roster in the image and the roster in the account are one generation.

`api/src/chip_chat/api/fixtures/persona_fixtures.json` is a committed export of
`ACCOUNTS.persona_fixtures`, read by
:func:`~chip_chat.api.visitors.shipped_roster` and only on the ``connect is None``
path. `docs/decisions/shipped-persona-roster.md` argued for it against the
alternative of an empty roster, on the ground that these are not invented
accounts — *"the rows `data-gen` generated ... exported verbatim, `demo_id`
included, so a session bound from this file is bound to the same synthetic
customer a Snowflake-backed deployment would have bound it to"* — and it named
the one way that argument could stop being true:

> **The file goes stale if the population is regenerated.** #47's nightly load can
> change `persona_fixtures` underneath it. The cost of stale is a narrative that
> quotes a points balance the account no longer has — which is exactly the
> disagreement between the opening message and the account tool that `persona.py`
> is written to avoid.

It went stale. `chip-qvg` reloaded the account from the five-hundred customer
generation and committed that generation at `data-gen/roster/`; this export was
owned by another agent that wave and was left holding the sixty-customer
generation's twenty-eight rows, `demo-0004` through `demo-0058`. Two of those
`demo_id`s exist in the current generation naming different customers, with
different home stores, different order counts and different balances, so the
staleness was not even visible as a missing row — it was visible only as a
narrative disagreeing with the ledger the account lane sums.

This module is what makes that a red test rather than a demo nobody re-reads. It
is the api-side twin of `data-gen/tests/test_roster.py`, which holds
`data-gen/roster/` to what the shipped `population.toml` generates: that test
proves the committed roster is the generation, this one proves the shipped export
is the committed roster, and between them a retune that moves the population fails
`make ci` in two places instead of desynchronising a third copy in silence.

## Why it restates the projection instead of importing the exporter

`data-gen/tests/export_shipped_roster.py` writes the file and this checks it, so
the obvious thing is for the check to call the writer. It cannot: pytest puts a
test file's own directory on `sys.path` and nothing else, so `data-gen/tests` is
importable only when the data-gen suite is in the same invocation, and
``uv run pytest api/tests`` is a thing people run. Restating the transform in six
lines is the smaller cost — and it is not really duplication, because the two
statements are load-bearing in opposite directions: the exporter says how the file
is produced and this says what the file has to be, which is the same relationship
`test_roster.py` has with the generator it regenerates from.
"""

import json
from pathlib import Path
from typing import Any

from chip_chat.api.visitors import (
    ROSTER_COLUMNS,
    SHIPPED_ROSTER_PATH,
    PersonaFixture,
    shipped_roster,
)

REPOSITORY = Path(__file__).resolve().parents[2]
"""The checkout. Both halves of the comparison are addressed from it."""

COMMITTED = REPOSITORY / "data-gen" / "roster" / "persona_fixtures.jsonl"
"""The authoritative copy: the rows `make snowflake-load-roster` loads.

Held to the shipped ``population.toml`` by `data-gen/tests/test_roster.py`, which
is what makes it authoritative rather than merely first.
"""

REGENERATE = "`uv run python data-gen/tests/export_shipped_roster.py`"
"""What to run when the two have parted company."""


def committed_rows() -> list[dict[str, Any]]:
    """Return the committed roster, one dictionary per line."""
    text = COMMITTED.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def expected_export(rows: list[dict[str, Any]]) -> str:
    """Return the export those rows require, byte for byte.

    The projection onto :data:`~chip_chat.api.visitors.ROSTER_COLUMNS` and the
    ``ORDER BY persona_id, rank`` are both copied from the query
    :class:`~chip_chat.api.visitors.SnowflakeRoster` runs, because the file exists
    to stand in for that query's result. Order is part of it and not cosmetic: the
    roster is a sequence the entry flow assigns from, so a file sorted differently
    hands the fourth visitor a different customer than Snowflake would.
    """
    ordered = sorted(rows, key=lambda row: (row["persona_id"], row["rank"]))
    projected = [{column: row[column] for column in ROSTER_COLUMNS} for row in ordered]
    return json.dumps(projected, indent=2) + "\n"


def test_the_shipped_export_is_the_committed_rosters_fixtures() -> None:
    """Byte for byte, against the projection the committed roster requires."""
    actual = SHIPPED_ROSTER_PATH.read_text(encoding="utf-8")

    assert actual == expected_export(committed_rows()), (
        "api/src/chip_chat/api/fixtures/persona_fixtures.json is not the "
        "projection of data-gen/roster/persona_fixtures.jsonl. Either the roster "
        "was re-exported and this was not, or this was edited by hand. Re-run "
        f"{REGENERATE}: a shipped roster the account does not hold is a visitor "
        "whose opening message and whose points balance describe two different "
        "customers"
    )


def test_the_export_names_the_same_customers_the_account_holds() -> None:
    """The `demo_id` claim the decision record rests on, asserted on the files.

    A byte comparison already implies this, and it is worth its own test anyway:
    when the export goes stale the byte comparison says *something* moved, and
    this says the thing that moved was who the visitor is. On 2026-08-27 the two
    sets overlapped in two identifiers out of twenty-eight, and those two were the
    dangerous ones — the same `demo_id` naming a different customer is the one
    kind of staleness that does not look like an error anywhere downstream.
    """
    committed = committed_rows()
    shipped = json.loads(SHIPPED_ROSTER_PATH.read_text(encoding="utf-8"))

    assert len(shipped) == len(committed)
    assert {row["demo_id"] for row in shipped} == {row["demo_id"] for row in committed}


def test_every_shipped_row_carries_exactly_the_columns_the_reader_reads() -> None:
    """Key for key, in order, because a missing key is read as ``None``.

    :func:`~chip_chat.api.visitors.shipped_roster` builds each row as
    ``tuple(record.get(column) for column in ROSTER_COLUMNS)``, so a key this file
    does not carry does not raise — it arrives as ``None``, and a ``None``
    ``points_balance`` makes the fixture unpopulated, which
    :class:`~chip_chat.api.visitors.StaticRoster` drops at construction. The
    failure mode of a mistyped key is therefore a smaller roster rather than an
    error, which is exactly the shape of bug a test has to catch instead of a
    reviewer. Order is asserted too: it costs nothing and it keeps the file
    readable as the query's result.
    """
    shipped = json.loads(SHIPPED_ROSTER_PATH.read_text(encoding="utf-8"))

    for row in shipped:
        assert tuple(row) == ROSTER_COLUMNS


def test_the_shipped_roster_hands_out_every_row_in_the_file() -> None:
    """Loaded through the reader itself, not re-parsed beside it.

    ``StaticRoster`` discards fixtures that fail
    :attr:`~chip_chat.api.visitors.PersonaFixture.populated` — no orders, no home
    store or no points balance — because issue #66's whole subject is a visitor
    arriving at an empty account. Every fixture in this generation clears that by
    construction, so a roster shorter than the file means a column arrived as the
    wrong type or not at all, and the deployment would serve the difference as
    unbound visitors without saying anything.
    """
    committed = committed_rows()

    roster = shipped_roster()

    assert len(roster.fixtures()) == len(committed)
    assert all(fixture.populated for fixture in roster.fixtures())


def test_the_shipped_narratives_quote_the_balances_beside_them() -> None:
    """The disagreement the staleness actually produced, asserted on the export.

    `data-gen/tests/test_roster.py` holds this on the committed roster; this holds
    it on the copy a visitor's opening message is composed from. They are the same
    assertion in two places on purpose, because the failure it names is what a
    visitor *sees*: the opening message quotes the narrative while
    ``get_points_balance`` sums the ledger, so a narrative carrying last
    generation's number puts both inside one conversation.
    """
    shipped = json.loads(SHIPPED_ROSTER_PATH.read_text(encoding="utf-8"))

    quoting = [
        row
        for row in shipped
        if "point" in row["narrative"] and any(ch.isdigit() for ch in row["narrative"])
    ]

    assert quoting, "no shipped narrative quotes a number of points any more"
    for row in quoting:
        assert f"{row['points_balance']:,}" in row["narrative"], (
            f"{row['demo_id']}'s narrative talks about points and does not quote "
            f"{row['points_balance']:,}: {row['narrative']}"
        )


def test_every_shipped_row_survives_the_reader_it_was_written_for() -> None:
    """The types, checked where they are consumed rather than where they are written.

    ``lifetime_spend`` is a string in both files because that is how a Snowflake
    ``NUMBER`` arrives and how the generator writes a :class:`~decimal.Decimal`;
    :meth:`~chip_chat.api.visitors.PersonaFixture.from_row` is what turns it into a
    number, and this is the only place that conversion is exercised on the bytes
    that actually ship.
    """
    shipped = json.loads(SHIPPED_ROSTER_PATH.read_text(encoding="utf-8"))

    for row in shipped:
        fixture = PersonaFixture.from_row(tuple(row[column] for column in ROSTER_COLUMNS))
        assert fixture.demo_id == row["demo_id"]
        assert fixture.rank >= 1
        assert fixture.lifetime_spend == float(row["lifetime_spend"])
        assert fixture.order_count > 0
