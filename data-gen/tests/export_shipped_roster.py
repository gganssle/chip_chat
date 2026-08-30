"""Rewrite the roster the API image ships from the roster the account is loaded from.

    uv run python data-gen/tests/export_shipped_roster.py

`data-gen/roster/persona_fixtures.jsonl` is the copy of `ACCOUNTS.persona_fixtures`
that `make snowflake-load-roster` puts into Snowflake, and `test_roster.py` holds
it to what the shipped `population.toml` generates from `roster/inputs/`. There is
a *third* copy: `api/src/chip_chat/api/fixtures/persona_fixtures.json`, which the
API image carries and `chip_chat.api.visitors.shipped_roster` reads on the
``connect is None`` path. `docs/decisions/shipped-persona-roster.md` is why that
copy exists and why it is a stopgap.

Three copies of a table need two things kept true, and the decision record
predicted exactly how the second one would come apart:

> **The file goes stale if the population is regenerated.** #47's nightly load can
> change `persona_fixtures` underneath it. The cost of stale is a narrative that
> quotes a points balance the account no longer has.

That is what happened. `chip-qvg` reloaded the account from the five-hundred
customer generation and committed it at `data-gen/roster/`; the shipped export was
owned by another agent that wave and kept the sixty-customer generation's
twenty-eight rows. Two of its `demo_id`s survive into the current generation
naming different customers, which is worse than none surviving, because a stale
export is not visibly stale.

So the export is a program rather than a paste. It reads the committed roster,
projects each row onto :data:`~chip_chat.api.visitors.ROSTER_COLUMNS` — imported
rather than retyped, so a column renamed in `visitors.py` moves this file with it
instead of silently dropping a key the reader then reads as ``None`` — orders the
rows the way ``_ROSTER_QUERY`` orders them, and writes the JSON the reader parses.
Nothing is computed here and nothing is invented: every value is the value on the
JSONL line, because the whole point is that the two files are one generation.

Run it after re-exporting `data-gen/roster/`, which is the only thing that can
make it necessary, and commit the two together. `api/tests/test_shipped_roster.py`
regenerates this projection on every `make ci` and fails if the files have parted
company, so forgetting to run it is a red test rather than a stale demo.
"""

import json
import sys
from pathlib import Path
from typing import Any

from chip_chat.api.visitors import ROSTER_COLUMNS, SHIPPED_ROSTER_PATH

REPOSITORY = Path(__file__).resolve().parents[2]
"""The checkout, which both files are addressed from."""

ROSTER = REPOSITORY / "data-gen" / "roster" / "persona_fixtures.jsonl"
"""The authoritative copy: what `make snowflake-load-roster` loads."""

REGENERATE = "`uv run python data-gen/tests/export_shipped_roster.py`"
"""What to run when the two have parted company. Quoted by the test that checks."""


def committed_rows() -> list[dict[str, Any]]:
    """Return the committed roster's rows, in the file's own order.

    Returns:
        One dictionary per line of `data-gen/roster/persona_fixtures.jsonl`,
        carrying every column the generator writes rather than only the eleven
        the entry flow reads.
    """
    text = ROSTER.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def shipped_export(rows: list[dict[str, Any]]) -> str:
    """Render the JSON `chip_chat.api.visitors.shipped_roster` parses.

    The projection is the whole transform. ``ROSTER_COLUMNS`` is both the column
    list of the query the Snowflake-backed roster runs and the key list the
    file-backed one looks up, so a row rendered under these keys in this order is
    the row a deployment with a connection factory would have read — which is the
    claim the decision record makes about ``demo_id`` and has to hold of every
    other column too, or the two deployments describe the same customer
    differently.

    Ordering is ``ORDER BY persona_id, rank``, copied from ``_ROSTER_QUERY``
    rather than left as the generator's, because the roster is a sequence the
    entry flow assigns from and not a set: a file in a different order hands the
    fourth visitor a different customer than Snowflake would.

    Args:
        rows: The committed roster's rows, as :func:`committed_rows` returns them.

    Returns:
        The file's text, two-space indented with a trailing newline, which is how
        the export has been formatted since it was first committed.

    Raises:
        KeyError: If a row is missing a column the reader expects. The generator
            writes every one of them, so this means the two packages have drifted
            and a silent ``None`` in a visitor's opening message is the failure it
            would otherwise become.
    """
    ordered = sorted(rows, key=lambda row: (row["persona_id"], row["rank"]))
    projected = [{column: row[column] for column in ROSTER_COLUMNS} for row in ordered]
    return json.dumps(projected, indent=2) + "\n"


def main() -> int:
    """Write the export and say what was written."""
    rows = committed_rows()
    text = shipped_export(rows)
    SHIPPED_ROSTER_PATH.write_text(text, encoding="utf-8")
    print(
        f"wrote {len(rows)} fixture(s) from {ROSTER.relative_to(REPOSITORY)} to "
        f"{SHIPPED_ROSTER_PATH.relative_to(REPOSITORY)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
