"""The nightly hand-off: what crosses from the lakehouse into Snowflake, and how.

Issue [#39](https://github.com/gganssle/chip_chat/issues/39) is the seam between
the two clocks. Databricks does the expensive overnight thinking; Snowflake is
the governed serving layer the agent queries on every turn. This module is the
declaration of what moves across, in what shape, and by what statement -- and
``databricks/notebooks/snowflake_publish.py`` is the loop that runs it.

**This module imports nothing but the standard library**, for the reason
:mod:`chip_chat.databricks.gold` gives: Terraform uploads it beside the notebook
and the notebook puts its directory on ``sys.path``, so it has to import two
ways -- as ``chip_chat.databricks.publish`` under pytest and as a flat
``publish`` on the driver. The Snowflake column lists below are therefore
transcribed rather than imported from ``chip_chat.snowflake.schema``, and
``test_publish.py`` asserts the transcription against it. That is the same
duplication ``sql/08_marts.sql`` argues for in its header: the producer holds
itself to the schema in ``make ci`` and so does the consumer, and a rename that
only one of them made is a failing test on both sides rather than a nightly job
that lands a column into nothing.

## The mechanism, and the one that was rejected

The Snowflake connector, decided on
[#12](https://github.com/gganssle/chip_chat/issues/12) and not Iceberg. The
rationale is on that ticket and is not re-argued here.

What the connector will happily do, and what this module refuses to let it do,
is own the target table. ``mode("overwrite")`` with the connector's own staging
table DROPS the target and renames a new one into its place. Everything hanging
off the target goes with it: [#43](https://github.com/gganssle/chip_chat/issues/43)'s
row access policy, the column comments #45's semantic view retrieves against,
and the declared keys a text-to-SQL system reads to know which joins exist. A
publish that silently detaches a row access policy is a breach nobody sees.

So the connector writes into :data:`STAGING_SCHEMA` and never anywhere else, and
one statement makes a generation live:

    INSERT OVERWRITE INTO CHIP_CHAT.MARTS.usual_order (...) SELECT ... FROM ...

``INSERT OVERWRITE`` truncates and inserts inside a single transaction, so a
conversation querying mid-publish sees last night's generation or tonight's and
never half of either -- which is the second acceptance criterion. The target
table object is never replaced, so nothing attached to it moves.

**One statement rather than ``BEGIN; TRUNCATE; INSERT; COMMIT;``**, which is the
idiom ``chip_chat.snowflake.load`` uses for the same guarantee on the developer
path. The difference is not taste: the connector's ``Utils.runQuery`` opens its
own JDBC connection per call, so a transaction opened in one call is not the
session the next call lands in. A multi-statement transaction cannot be spread
across two ``runQuery`` calls at all, and one that looked like it worked would be
four autocommitted statements with a window between each pair.

## Why the staging tables are not beside their targets

``CHIP_CHAT_READ`` holds ``SELECT ON FUTURE TABLES`` in all three serving
schemas -- ``sql/03_grants.sql`` grants it so that a table #42 or #39 adds later
is readable without anyone remembering to re-run a grants file. That is right
for a declared table and wrong for an incoming generation: an ``orders_incoming``
in ``CHIP_CHAT.ACCOUNTS`` would be a complete unscoped copy of the population,
readable by the identity the agent runs as, covered by no row access policy,
because #43 attaches policies to tables by name.

:data:`STAGING_SCHEMA` exists so that sentence cannot be true. Nothing but
``CHIP_CHAT_PUBLISH`` is granted anything in it, it holds no declared table, and
a staging table is dropped when its swap succeeds. A staging table left behind
is therefore evidence of a failed run rather than litter.

## What is published, and what is deliberately not

Eleven tables. The four harvested catalogue tables, the three synthetic account
tables the marts are computed from, and the four gold marts.

**Not ``demo_visitors``, ``personas`` or ``persona_fixtures``.** ``demo_visitors``
is the one account table a visitor writes to -- ``display_name``,
``home_store_override`` and ``stated_preferences`` live there and nowhere else --
so a nightly overwrite would delete every edit a visitor made that day. The other
two are reference rows the generator emits once. All three reach Snowflake
through ``chip_chat.snowflake.load``, run by an operator as ``CHIP_CHAT_ADMIN``.
That is also why ``CHIP_CHAT_PUBLISH`` is granted the three tables here by name
rather than the schema: it still physically cannot read ``demo_visitors``, which
is the containment RFC-001 §04 rests its answer to PRD Q2 on.

**Not ``gold_synthetic.recommendations``.** #37 batch-scores the recommender into
a fifth gold table, and ``CHIP_CHAT.MARTS`` has no table for it -- RFC-001 §04
fixes four. Publishing it would mean adding a table to the serving schema, which
is a decision about where ``get_recommendations`` reads from rather than a line in
this list.

## derived_at is carried, never restamped

RFC-001 §10: when the Databricks job fails, serve stale gold marts **with their
``derived_at``**, alert, and never silently serve stale data as fresh. Two halves,
and this module owns both.

The stale half is structural. The swap is one statement per table, so a run that
dies -- mid-write, mid-swap, between two tables -- leaves the previous generation
exactly where it was. There is no state to reconcile and nothing to roll back by
hand.

The timestamp half is a rule about the projection, and it is the one that is easy
to get wrong. ``derived_at`` is selected out of the gold mart unchanged. The
publish never writes ``current_timestamp()`` into it. If it did, a night when the
gold pipeline failed and the publish copied yesterday's mart again would produce
rows stamped tonight -- stale data presented as fresh, which is the one outcome
§10 names. :data:`CARRIED_ONLY` is that rule as data, and ``test_publish.py``
holds every projection to it.

## How a value crosses

Three transports, and two of them exist because a value that crosses through a
type conversion nobody wrote down is a value that arrives nearly right.

:data:`DIRECT` is everything the connector maps without a decision: strings,
booleans, longs, and exact decimals.

:data:`JSON_ARRAY` is the four ``ARRAY`` columns. They travel as a JSON string
and are parsed back on the Snowflake side, the same way
``databricks/notebooks/recommender_publish.py`` sends its two decimals across as
strings: it is one fewer thing that has to be true about the connector's variant
support, and it is legible in the staging table when a publish is being debugged.

:data:`UTC_TIMESTAMP` is every ``TIMESTAMP_NTZ``. The connector maps a Spark
``TimestampType`` onto ``TIMESTAMP_LTZ``, which is a *zoned* type: landing one in
a ``TIMESTAMP_NTZ`` column applies whatever the session's timezone happens to be.
Every timestamp in this database is UTC and carries no zone -- ``sql/06_catalogue.sql``
says so about the whole account -- so the value is formatted to a string in Spark
and read back with an explicit format in Snowflake, and no engine's timezone
setting is consulted by either end. The notebook pins
``spark.sql.session.timeZone`` to :data:`SPARK_TIMEZONE` and fails if it is
anything else, which is what makes that a property rather than an assumption.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ACCOUNT_TABLES",
    "CARRIED_ONLY",
    "DATABASE",
    "DIRECT",
    "JSON_ARRAY",
    "PRIVATE_KEY_SECRET",
    "PUBLISH_ROLE",
    "PUBLISH_WAREHOUSE",
    "SECRET_SCOPE",
    "SNOWFLAKE_TIMESTAMP_FORMAT",
    "SOURCE",
    "SPARK_TIMESTAMP_FORMAT",
    "SPARK_TIMEZONE",
    "STAGING_SCHEMA",
    "TARGETS",
    "TRANSPORTS",
    "UTC_TIMESTAMP",
    "WAREHOUSE_CREDITS_PER_HOUR",
    "WAREHOUSE_MINIMUM_SECONDS",
    "Column",
    "Target",
    "column_names",
    "credits",
    "drop_staging",
    "options",
    "required_columns",
    "select",
    "staging",
    "swap",
    "target",
    "targets_in",
]

# --- The two accounts, named once ---------------------------------------------

DATABASE: Final = "CHIP_CHAT"
"""The Snowflake database. ``chip_chat.snowflake.account.DATABASE``, transcribed."""

STAGING_SCHEMA: Final = "STAGING"
"""Where every incoming generation lands, and the only schema this job writes
outside the three it publishes into. See the module docstring for why it is not
beside the targets. ``sql/02_database.sql`` creates it and ``sql/03_grants.sql``
gives it to ``CHIP_CHAT_PUBLISH`` and to nobody else."""

PUBLISH_ROLE: Final = "CHIP_CHAT_PUBLISH"
"""The only role this job assumes. It cannot read ``demo_visitors``."""

PUBLISH_WAREHOUSE: Final = "CHIP_CHAT_PUBLISH_WH"
"""Batch compute. A publish cannot queue in front of a conversation because it
has no ``USAGE`` on the warehouse a conversation runs on."""

SOURCE: Final = "net.snowflake.spark.snowflake"
"""The Spark data source. ``SNOWFLAKE_SOURCE_NAME`` in the connector's own docs,
and a string here because the notebook may not import Scala to ask for it."""

SECRET_SCOPE: Final = "chip-chat-snowflake"
"""The Databricks secret scope holding the publisher's credential.

Created empty by Terraform and filled by an operator with the CLI, so that no
private key ever enters Terraform state -- the same argument
``sql/04_users.sql`` makes about ``RSA_PUBLIC_KEY`` never appearing in the
checked-in SQL.
"""

PRIVATE_KEY_SECRET: Final = "publisher-private-key"
"""The key in :data:`SECRET_SCOPE` holding ``CHIP_CHAT_PUBLISHER``'s unencrypted
PKCS#8 private key. Key-pair auth, because a ``TYPE = SERVICE`` user cannot use
a password at all."""

SPARK_TIMEZONE: Final = "UTC"
"""What ``spark.sql.session.timeZone`` must be for a publish to run.

Asserted by the notebook rather than set by it. Databricks defaults to UTC, and
a workspace where it is not is one where every timestamp already in silver was
parsed against a different clock -- which is a thing to find out about rather
than to quietly correct on the way past.
"""

SPARK_TIMESTAMP_FORMAT: Final = "yyyy-MM-dd HH:mm:ss.SSSSSS"
"""How a :data:`UTC_TIMESTAMP` column is written on the Spark side. Microseconds,
because that is the resolution ``TIMESTAMP_NTZ`` keeps."""

SNOWFLAKE_TIMESTAMP_FORMAT: Final = "YYYY-MM-DD HH24:MI:SS.FF6"
"""The same instant, spelled the way ``TO_TIMESTAMP_NTZ`` reads it. The two
formats are one format written twice, and ``test_publish.py`` checks they agree
field for field."""

WAREHOUSE_CREDITS_PER_HOUR: Final = 1.0
"""What an X-Small warehouse costs per hour of activity. Snowflake's published
rate for the size ``sql/01_warehouses.sql`` creates."""

WAREHOUSE_MINIMUM_SECONDS: Final = 60
"""The minimum Snowflake bills for a warehouse resume.

Load-bearing for the fifth acceptance criterion rather than trivia: a publish
that moves eleven small tables can easily be *less* than a minute of warehouse
time, and an estimate that multiplied the elapsed seconds by the rate would
report a cost below what the account is actually charged.
"""


# --- How a value crosses ------------------------------------------------------

DIRECT: Final = "direct"
"""Crosses as itself. Strings, booleans, longs, exact decimals."""

JSON_ARRAY: Final = "json_array"
"""Crosses as a JSON string and is parsed back into an ``ARRAY``."""

UTC_TIMESTAMP: Final = "utc_timestamp"
"""Crosses as a formatted string and is read back as ``TIMESTAMP_NTZ``."""

TRANSPORTS: Final[tuple[str, ...]] = (DIRECT, JSON_ARRAY, UTC_TIMESTAMP)
"""Every transport there is. A column declaring anything else is refused by
:func:`select` rather than producing SQL that fails on the cluster."""

CARRIED_ONLY: Final[tuple[str, ...]] = ("derived_at", "harvested_at")
"""Columns the publish may only copy, never compute.

``derived_at`` is when the *mart* was computed and ``harvested_at`` is when the
*page* was fetched. Neither is when the copy happened, and RFC-001 §10 turns on
the difference: a night the gold pipeline failed republishes yesterday's rows,
and a publish that restamped them would present stale data as fresh. The rule is
enforced by a test over every projection rather than by remembering it.
"""


@dataclass(frozen=True, slots=True)
class Column:
    """One column of one published table.

    Attributes:
        name: The column name. The same on both sides -- the serving layer's
            name is the lakehouse's, and a publish that renamed anything would
            be a schema decision made inside a copy.
        transport: One of :data:`TRANSPORTS`.
        required: Whether the Snowflake target declares it ``NOT NULL``. The
            publish checks these on the Spark side before it writes anything,
            because a ``NULL result in a non-nullable column`` half way through
            a swap is a correct refusal with a useless message.
    """

    name: str
    transport: str = DIRECT
    required: bool = False


@dataclass(frozen=True, slots=True)
class Target:
    """One table published from the lakehouse into Snowflake.

    Attributes:
        table: The unqualified name. The same on both sides.
        schema: The Snowflake schema: ``CATALOGUE``, ``ACCOUNTS`` or ``MARTS``.
        layer: The medallion layer read from: ``silver`` or ``gold``.
        stream: ``harvested`` or ``synthetic``.
        columns: Every column the serving table declares, in the order
            ``snowflake/sql/`` declares them. A column the lakehouse has and the
            serving table does not is simply not named here -- CHIP_CHAT.CATALOGUE
            is a projection of silver rather than a copy of it.
        key: The target's declared ``PRIMARY KEY``. Snowflake does not enforce
            one, so this is what ``publish_verify.py`` counts duplicates over.
        why: What this table is for, in one sentence, for whoever is reading a
            run's output at three in the morning.
    """

    table: str
    schema: str
    layer: str
    stream: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]
    why: str

    @property
    def qualified(self) -> str:
        """The target, database-qualified: ``CHIP_CHAT.MARTS.usual_order``."""
        return f"{DATABASE}.{self.schema}.{self.table}"


# --- What crosses -------------------------------------------------------------
#
# The four catalogue tables are a PROJECTION of silver: `menu_items` carries
# fifteen columns in the lakehouse and nine here, and the nine are the ones a
# conversation reads. `sql/06_catalogue.sql`'s header carries that argument.

_CATALOGUE: Final[tuple[Target, ...]] = (
    Target(
        table="menu_items",
        schema="CATALOGUE",
        layer="silver",
        stream="harvested",
        columns=(
            Column("item_id", required=True),
            Column("name", required=True),
            Column("category"),
            Column("description"),
            Column("calories"),
            Column("allergens", transport=JSON_ARRAY),
            Column("allergen_disclosure", required=True),
            Column("source_url", required=True),
            Column("harvested_at", transport=UTC_TIMESTAMP, required=True),
        ),
        key=("item_id",),
        why="what is orderable, and what Chipotle says about it",
    ),
    Target(
        table="item_prices",
        schema="CATALOGUE",
        layer="silver",
        stream="harvested",
        columns=(
            Column("restaurant_id", required=True),
            Column("item_id", required=True),
            Column("unit_price", required=True),
            Column("unit_delivery_price", required=True),
            Column("is_available", required=True),
            Column("source_url", required=True),
            Column("harvested_at", transport=UTC_TIMESTAMP, required=True),
        ),
        key=("restaurant_id", "item_id"),
        why="money, keyed by the restaurant that published it",
    ),
    Target(
        table="modifiers",
        schema="CATALOGUE",
        layer="silver",
        stream="harvested",
        columns=(
            Column("modifier_id", required=True),
            Column("item_id", required=True),
            Column("modifier_item_id", required=True),
            Column("name", required=True),
            Column("delta_calories"),
        ),
        key=("modifier_id",),
        why="how an item is built",
    ),
    Target(
        table="stores",
        schema="CATALOGUE",
        layer="silver",
        stream="harvested",
        columns=(
            Column("store_id", required=True),
            Column("name"),
            Column("city"),
            Column("region"),
            Column("hours", transport=JSON_ARRAY),
        ),
        key=("store_id",),
        why="where an order happens, and when it could have",
    ),
)

# The three account tables, and the list is exactly `schema.MART_INPUTS`: the
# tables the marts are computed from are the tables the publish carries. Not
# demo_visitors -- see the module docstring.

_ACCOUNTS: Final[tuple[Target, ...]] = (
    Target(
        table="orders",
        schema="ACCOUNTS",
        layer="silver",
        stream="synthetic",
        columns=(
            Column("order_id", required=True),
            Column("demo_id", required=True),
            Column("store_id", required=True),
            Column("placed_at", transport=UTC_TIMESTAMP, required=True),
            Column("status", required=True),
            Column("total", required=True),
            Column("channel", required=True),
            Column("priced_restaurant_id"),
        ),
        key=("order_id",),
        why="eighteen months of synthetic orders, one row each",
    ),
    Target(
        table="order_items",
        schema="ACCOUNTS",
        layer="silver",
        stream="synthetic",
        columns=(
            Column("order_id", required=True),
            Column("line_number", required=True),
            Column("demo_id", required=True),
            Column("item_id", required=True),
            Column("qty", required=True),
            Column("modifiers", transport=JSON_ARRAY),
            Column("unit_price"),
            Column("line_total"),
        ),
        key=("order_id", "line_number"),
        why="the lines of an order, carrying their own demo_id",
    ),
    Target(
        table="loyalty_ledger",
        schema="ACCOUNTS",
        layer="silver",
        stream="synthetic",
        columns=(
            Column("entry_id", required=True),
            Column("demo_id", required=True),
            Column("delta", required=True),
            Column("reason", required=True),
            Column("order_id"),
            Column("reward_name"),
            Column("created_at", transport=UTC_TIMESTAMP, required=True),
        ),
        key=("entry_id",),
        why="every movement of loyalty points, and what moved them",
    ),
)

# The four marts. RFC-001 §04's four, in §04's order, and `derived_at` on every
# one of them because §10 requires a stale mart to be servable with its own
# timestamp.

_MARTS: Final[tuple[Target, ...]] = (
    Target(
        table="customer_360",
        schema="MARTS",
        layer="gold",
        stream="synthetic",
        columns=(
            Column("demo_id", required=True),
            Column("order_count"),
            Column("lifetime_spend"),
            Column("last_order_at", transport=UTC_TIMESTAMP),
            Column("favourite_store"),
            Column("cadence_days"),
            Column("lapsed_flag"),
            Column("derived_at", transport=UTC_TIMESTAMP),
        ),
        key=("demo_id",),
        why="how many, how much, how often, where, and whether they went quiet",
    ),
    Target(
        table="usual_order",
        schema="MARTS",
        layer="gold",
        stream="synthetic",
        columns=(
            Column("demo_id", required=True),
            Column("item_id"),
            Column("modifiers", transport=JSON_ARRAY),
            Column("confidence"),
            Column("derived_at", transport=UTC_TIMESTAMP),
        ),
        key=("demo_id",),
        why="the one basket that is theirs, and how sure we are",
    ),
    Target(
        table="item_affinity",
        schema="MARTS",
        layer="gold",
        stream="synthetic",
        columns=(
            Column("item_id", required=True),
            Column("related_item_id", required=True),
            Column("lift"),
            Column("derived_at", transport=UTC_TIMESTAMP),
        ),
        key=("item_id", "related_item_id"),
        why="which items are ordered together, over the whole population",
    ),
    Target(
        table="spend_summary",
        schema="MARTS",
        layer="gold",
        stream="synthetic",
        columns=(
            Column("demo_id", required=True),
            Column("period", required=True),
            Column("total"),
            Column("order_count"),
            Column("derived_at", transport=UTC_TIMESTAMP),
        ),
        key=("demo_id", "period"),
        why="spend by month, for the question that asks for a number",
    ),
)

TARGETS: Final[tuple[Target, ...]] = _CATALOGUE + _ACCOUNTS + _MARTS
"""Every published table, in the order the publish runs them.

The order is the one ``snowflake/sql/`` declares the tables in, which is a
foreign key order: ``menu_items`` before the two tables that reference it,
``orders`` before ``order_items``. Snowflake enforces none of those keys, so the
order buys nothing at write time -- what it buys is that a run killed halfway
leaves a consistent *set* of generations, with a catalogue no order line points
outside of, rather than lines referring to items that have not landed yet.
"""

ACCOUNT_TABLES: Final[tuple[str, ...]] = tuple(item.table for item in _ACCOUNTS)
"""The three account tables this job carries. Asserted equal to
``chip_chat.snowflake.schema.MART_INPUTS``: the tables the marts are computed
from are exactly the tables the publish is trusted with."""


# --- Lookups ------------------------------------------------------------------


def target(name: str) -> Target:
    """Return the published table called ``name``.

    Args:
        name: An unqualified table name, in any case.

    Returns:
        The declaration.

    Raises:
        KeyError: If nothing by that name is published.
    """
    for candidate in TARGETS:
        if candidate.table.upper() == name.upper():
            return candidate
    raise KeyError(f"{name!r} is not published into {DATABASE}")


def targets_in(schema_name: str) -> Iterator[Target]:
    """Yield every target published into ``schema_name``, in :data:`TARGETS` order."""
    for candidate in TARGETS:
        if candidate.schema == schema_name:
            yield candidate


def column_names(candidate: Target) -> tuple[str, ...]:
    """Return ``candidate``'s column names, in declaration order."""
    return tuple(column.name for column in candidate.columns)


def required_columns(candidate: Target) -> tuple[str, ...]:
    """Return the columns the Snowflake target declares ``NOT NULL``.

    What the notebook counts nulls in before it writes anything. A publish that
    would be refused by the target's own constraint is better refused in Spark,
    where the message can name the column and the table.
    """
    return tuple(column.name for column in candidate.columns if column.required)


def staging(candidate: Target) -> str:
    """Return the staging table ``candidate``'s incoming generation lands in.

    Schema-prefixed -- ``CHIP_CHAT.STAGING.MARTS_USUAL_ORDER`` -- even though the
    eleven table names happen to be unique across the three serving schemas
    today. A staging table read on its own says which serving table it belongs
    to, and the flat namespace stops a twelfth table from making that stop being
    true quietly.
    """
    return f"{DATABASE}.{STAGING_SCHEMA}.{candidate.schema}_{candidate.table}".upper()


# --- The SQL ------------------------------------------------------------------


def _spark_expression(column: Column) -> str:
    """Return how ``column`` is read out of the lakehouse.

    Raises:
        ValueError: If the column declares a transport this module has no
            expression for. A publish that emitted the column name and hoped
            would land a Spark type nobody chose into a Snowflake column
            somebody did.
    """
    if column.transport == DIRECT:
        return column.name
    if column.transport == JSON_ARRAY:
        return f"to_json({column.name}) AS {column.name}"
    if column.transport == UTC_TIMESTAMP:
        return (
            f"date_format({column.name}, '{SPARK_TIMESTAMP_FORMAT}') AS {column.name}"
        )
    raise ValueError(
        f"{column.name} declares transport {column.transport!r}; "
        f"expected one of {TRANSPORTS}"
    )


def _snowflake_expression(column: Column) -> str:
    """Return how ``column`` is read back out of its staging table.

    The inverse of :func:`_spark_expression`, and the tests run the pair over
    every column so that a transport added to one and not the other fails
    `make ci`.

    Raises:
        ValueError: If the transport is unknown.
    """
    if column.transport == DIRECT:
        return column.name
    if column.transport == JSON_ARRAY:
        return f"PARSE_JSON({column.name})::ARRAY"
    if column.transport == UTC_TIMESTAMP:
        return f"TO_TIMESTAMP_NTZ({column.name}, '{SNOWFLAKE_TIMESTAMP_FORMAT}')"
    raise ValueError(
        f"{column.name} declares transport {column.transport!r}; "
        f"expected one of {TRANSPORTS}"
    )


def select(candidate: Target, resolve: Callable[[str, str, str], str]) -> str:
    """Return the Spark SQL that reads ``candidate``'s rows out of the lakehouse.

    Args:
        candidate: The target.
        resolve: Takes ``(layer, stream, table)`` and returns a fully qualified
            Unity Catalog name. The notebook passes ``catalog.table``; the tests
            pass something that records what was asked for. No table name is
            ever written as a literal in this module, for the reason
            ``gold.query`` gives.

    Returns:
        One ``SELECT`` producing exactly the target's columns, in the target's
        order, in their transport shape.
    """
    source = resolve(candidate.layer, candidate.stream, candidate.table)
    projection = ",\n    ".join(
        _spark_expression(column) for column in candidate.columns
    )
    return f"SELECT\n    {projection}\nFROM {source}"


def swap(candidate: Target) -> str:
    """Return the one statement that makes ``candidate``'s new generation live.

    ``INSERT OVERWRITE`` truncates the target and inserts the staging table's
    rows in a single transaction. The target table object is never dropped or
    renamed, so the row access policy #43 attaches to it, the comments #45
    retrieves against, and the declared keys all stay where they are.

    The column list is written out rather than left positional: a column added
    to the middle of the serving table by a ``CREATE OR ALTER`` would silently
    shift every value one place to the right in a positional insert, and land a
    price in a calorie count.
    """
    columns = ", ".join(column_names(candidate))
    projection = ",\n    ".join(
        _snowflake_expression(column) for column in candidate.columns
    )
    return (
        f"INSERT OVERWRITE INTO {candidate.qualified} ({columns})\n"
        f"SELECT\n    {projection}\n"
        f"FROM {staging(candidate)}"
    )


def drop_staging(candidate: Target) -> str:
    """Return the statement that removes ``candidate``'s staging table.

    Run only after the swap succeeded. A staging table that is still there is a
    run that did not finish, which is worth being able to look inside.
    """
    return f"DROP TABLE IF EXISTS {staging(candidate)}"


# --- The connection -----------------------------------------------------------


def options(url: str, user: str, schema_name: str) -> dict[str, str]:
    """Return the connector options for one schema, minus the credential.

    The private key is not a parameter and is not returned. The notebook adds
    ``pem_private_key`` from :data:`SECRET_SCOPE` at the point of use, so that
    nothing here, and nothing a test prints, has ever held one.

    Args:
        url: The account URL, e.g. ``hq72718.us-east-2.aws.snowflakecomputing.com``.
        user: The Snowflake user. ``CHIP_CHAT_PUBLISHER`` in the deployed job.
        schema_name: The schema the write is aimed at. Every write this job makes
            is into :data:`STAGING_SCHEMA`; the serving schemas are reached by
            fully qualified name in :func:`swap`, never by a session default.

    Returns:
        The options, ready to hand to ``DataFrame.write.options``.

    Raises:
        ValueError: If any of the three is empty. A connector handed a blank URL
            fails minutes later with a JDBC error that names neither the job nor
            the missing value.
    """
    for label, value in (("url", url), ("user", user), ("schema", schema_name)):
        if not value:
            raise ValueError(f"a publish needs a Snowflake {label}")
    return {
        "sfURL": url,
        "sfUser": user,
        "sfDatabase": DATABASE,
        "sfSchema": schema_name,
        "sfWarehouse": PUBLISH_WAREHOUSE,
        "sfRole": PUBLISH_ROLE,
        # Belt and braces with the UTC_TIMESTAMP transport rather than instead
        # of it: this pins the JDBC session's timezone, and the transport means
        # no timestamp depends on it having worked.
        "sfTimezone": SPARK_TIMEZONE,
    }


# --- What a run cost ----------------------------------------------------------


def credits(active_seconds: float) -> float:
    """Return what ``active_seconds`` of publish warehouse costs, in credits.

    Issue #39's fifth acceptance criterion asks for one full publish's cost
    measured and recorded. This is the Snowflake half, computed from the
    warehouse's published rate rather than read back from ``ACCOUNT_USAGE`` --
    which ``CHIP_CHAT_PUBLISH`` cannot see, deliberately, and which lags a live
    run by up to three hours anyway. ``docs/nightly-publish.md`` says how to get
    the billed figure as ``CHIP_CHAT_ADMIN`` once it has settled.

    Args:
        active_seconds: How long the warehouse was awake.

    Returns:
        Credits, billed at :data:`WAREHOUSE_CREDITS_PER_HOUR` with Snowflake's
        :data:`WAREHOUSE_MINIMUM_SECONDS` floor applied -- an eleven-table
        publish can finish inside the minimum, and an estimate that ignored the
        floor would report less than the account is charged.

    Raises:
        ValueError: If ``active_seconds`` is negative.
    """
    if active_seconds < 0:
        raise ValueError(f"a warehouse cannot be awake for {active_seconds} seconds")
    billed = max(float(active_seconds), float(WAREHOUSE_MINIMUM_SECONDS))
    return billed / 3600.0 * WAREHOUSE_CREDITS_PER_HOUR
