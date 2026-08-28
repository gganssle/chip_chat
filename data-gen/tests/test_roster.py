"""The committed roster is the shipped population's, and stays that way.

`data-gen/roster/` holds three of the six tables the generator writes —
``personas``, ``persona_fixtures`` and ``demo_visitors`` — plus the manifest
that names the generation all six came from. It exists because those three are
the only tables that reach `CHIP_CHAT.ACCOUNTS` through
:mod:`chip_chat.snowflake.load`: issue #39's nightly publish writes ``orders``,
``order_items`` and ``loyalty_ledger`` out of silver and cannot see the roster
at all (docs/snowflake-schema.md §7). Two loaders, two halves, and nothing that
made them name the same generation.

On 2026-08-27 they did not. The account held orders and a ledger generated for
five hundred customers and a roster generated for sixty, so twenty-eight
`persona_fixtures` rows described customers whose histories were somebody
else's: `demo-0048`'s fixture said eighty orders and three hundred and
ninety-seven points while the tables said thirty-one and one thousand three
hundred and sixty-three. Nothing was wrong with the generator — it asserts the
two agree by construction, and ``test_referential_integrity.py`` holds it —
and nothing was wrong with either loader. What was missing was any copy of the
roster the account was supposed to be holding, so the drift could not be seen
by comparing anything to anything.

The copy is committed rather than regenerated on demand for the reason
docs/snowflake-account.md §10 gives about the landing zone: a population that
exists only in one agent's working directory and in one Snowflake account is a
population with no copies. `make snowflake-load-roster` loads these files.

A committed copy is a second thing to keep true, which is what these tests are
for. They regenerate the shipped population and hold the committed bytes to it —
so a retune that moves the population fails here, loudly, in `make ci`, rather
than quietly desynchronising the live account the next time somebody publishes.

## Why ``roster/inputs/`` exists, as of GitHub #106

The population is a function of three things: the catalogue, the published
rewards terms, and ``population.toml``. Two of those used to be the committed
*test fixtures* — a two-entree catalogue and the harvest tests' recorded site —
because that is what a laptop with no network could reach, and the account was
loaded from the same small generation, so the two agreed.

#106 ended that. The account now holds a population composed from the real
harvested catalogue: 192 published items and 1,385 modifiers rather than ten
rows. Regenerating from the fixture would therefore no longer produce what the
account holds, and a test comparing the two would be asserting that two
different generations are the same one.

So the inputs travel with the roster. ``roster/inputs/`` carries the built
catalogue and the three policy tables
:func:`~chip_chat.data_gen.rewards.load_rewards_terms` reads — 1.4 megabytes,
against seventeen for the population they generate — and these tests build from
those. `make ci` still needs no network, no credential and no harvest, and
"reproducible from this repository byte for byte" stays a claim you can check
rather than one you have to believe.

The fixture catalogue has not gone anywhere and is still what the rest of this
suite runs against: every property it asserts holds at any catalogue size, and
a suite that regenerated 192 items to prove referential integrity would spend
the time to learn nothing new.
"""

import json
from decimal import Decimal
from functools import cache
from pathlib import Path

from population_fixtures import shipped_config

from chip_chat.catalog import load_catalog
from chip_chat.data_gen import SyntheticPopulation, generate_population
from chip_chat.data_gen.records import to_jsonl
from chip_chat.data_gen.rewards import load_rewards_terms
from chip_chat.harvest.blobs import LocalBlobStore

ROSTER = Path(__file__).resolve().parents[1] / "roster"
"""Where the committed copy lives."""

INPUTS = ROSTER / "inputs"
"""The catalogue and policy tables the committed roster was generated from.

A landing zone in miniature: the same layout ``chip_chat.harvest.blobs`` writes
and the same prefixes the loaders read, so nothing here has to know it is a
fixture. See this module's docstring for why it is committed at all.
"""


@cache
def shipped_population() -> SyntheticPopulation:
    """Regenerate what the live account is supposed to be holding.

    Generated once for the whole module. Three and a half seconds against the
    real catalogue, measured on 2026-08-28, which is what makes committing the
    inputs affordable rather than merely correct.
    """
    blobs = LocalBlobStore(INPUTS)
    return generate_population(
        load_catalog(blobs), load_rewards_terms(blobs), shipped_config()
    )


COMMITTED = ("personas", "persona_fixtures", "demo_visitors")
"""The tables `chip_chat.snowflake.load` is the only route into the account for.

The other three arrive from silver on issue #39's nightly schedule, are an
order of magnitude larger, and are reproducible from this repository byte for
byte — ``manifest.json`` records their digests, which is what makes the claim
checkable rather than asserted.
"""


def test_the_committed_roster_is_the_shipped_populations() -> None:
    """Byte for byte, table by table, against a fresh generation."""
    population = shipped_population()

    for name in COMMITTED:
        expected = to_jsonl(population.table(name))
        actual = (ROSTER / f"{name}.jsonl").read_bytes()
        assert actual == expected, (
            f"data-gen/roster/{name}.jsonl is not what roster/inputs/ and the "
            "shipped population.toml generate. Either the config was retuned, "
            "or the catalogue under roster/inputs/ was refreshed, and the "
            "committed roster was not re-exported; or the roster was edited by "
            "hand. Re-export it and reload the account: a roster the account "
            "does not hold is the defect this directory exists to foreclose"
        )


def test_the_committed_manifest_names_the_generation_it_came_from() -> None:
    """Including the three tables the directory does not carry.

    The manifest is the whole point of committing anything here. It records the
    seed, both input digests and a SHA-256 per table, so "does the account hold
    this generation of ``orders``?" is a question with an answer rather than an
    argument — and so a future rebuild can tell whether the landing zone it
    just regenerated is the one the gold marts were computed against.
    """
    population = shipped_population()

    manifest = json.loads((ROSTER / "manifest.json").read_text(encoding="utf-8"))

    assert manifest == population.manifest()
    assert set(manifest["tables"]) == set(population.manifest()["tables"])
    assert set(COMMITTED) < set(manifest["tables"])


def test_every_committed_fixture_agrees_with_the_committed_history() -> None:
    """The invariant the live account broke, asserted on the files themselves.

    ``test_referential_integrity.py`` holds this on the in-memory population
    and is the reason a *coherent* load cannot look like the account did. This
    holds it on the bytes that will be loaded, which is the last point before
    the two halves separate — and it fails if somebody hand-edits a number in
    ``persona_fixtures.jsonl`` to make a narrative read better.
    """
    population = shipped_population()
    orders: dict[str, int] = {}
    spend: dict[str, Decimal] = {}
    for order in population.orders:
        orders[order.demo_id] = orders.get(order.demo_id, 0) + 1
        spend[order.demo_id] = spend.get(order.demo_id, Decimal("0")) + order.total
    points: dict[str, int] = {}
    for entry in population.loyalty_ledger:
        points[entry.demo_id] = points.get(entry.demo_id, 0) + entry.delta

    committed = [
        json.loads(line)
        for line in (ROSTER / "persona_fixtures.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]

    assert len(committed) == 28
    for row in committed:
        demo_id = row["demo_id"]
        assert row["order_count"] == orders[demo_id]
        assert Decimal(str(row["lifetime_spend"])) == spend[demo_id]
        assert row["points_balance"] == points[demo_id]


def test_the_narratives_quote_the_numbers_beside_them() -> None:
    """A prose contradiction is a contradiction, and two archetypes are exposed.

    The Lapsed Customer's narrative and the Newcomer's quote the points balance
    in words — "14,495 points still unredeemed" — so a fix that corrected the
    numeric columns and left the prose alone would have traded one visible
    contradiction for a less visible one. The generator renders both from the
    same measured facts, and this is what says so.
    """
    population = shipped_population()

    quoting = [
        row
        for row in population.persona_fixtures
        if "point" in row.narrative and any(ch.isdigit() for ch in row.narrative)
    ]

    assert quoting, "no fixture narrative quotes a number of points any more"
    for row in quoting:
        assert f"{row.points_balance:,}" in row.narrative, (
            f"{row.demo_id}'s narrative talks about points and does not quote "
            f"{row.points_balance:,}: {row.narrative}"
        )
