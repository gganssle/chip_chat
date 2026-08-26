"""The four marts, what every number in them means, and what a confidence is.

Issue #36 asks for `customer_360`, `usual_order`, `item_affinity` and
`spend_summary`, and it is the issue that earns Databricks its place in this
architecture: Snowflake is the governed low-latency store the agent hits every
turn, and Databricks is the batch engine that computes overnight what would be
far too slow to compute mid-conversation. The pipeline is
``databricks/notebooks/gold_marts.py`` and, like #33's and #34's, it is almost
empty: it loops over :data:`MARTS` and declares one materialized view per
entry from the SQL held here. Every decision that is not "call Spark" lives in
this module, where ``databricks/tests/test_gold.py`` can assert it without a
cluster.

**This module imports nothing but the standard library, and that is
load-bearing** -- the same reason it is load-bearing in
:mod:`chip_chat.databricks.bronze` and :mod:`chip_chat.databricks.silver`. A
Lakeflow pipeline runs a notebook in the workspace, not an installed wheel, so
Terraform uploads this exact file beside the notebook and the notebook puts its
directory on ``sys.path``. It therefore has to import two ways: as
``chip_chat.databricks.gold`` under pytest, and as a flat top-level ``gold`` on
the driver. The handful of constants it shares with ``silver.py``,
``catalog.py`` and ``chip_chat.data_gen`` are spelled out again below and
asserted equal to theirs in the tests rather than imported.

**The SQL is here rather than in the notebook**, which is the one place this
module's shape differs from silver's. Silver's tables are all built the same
four ways, so a declaration plus a loop is the whole pipeline. A mart is an
aggregation and each of the four is a genuinely different one, so what is
declared here is the query itself -- with every table name and every threshold
left as a placeholder :func:`query` fills from the constants below. That has
two consequences worth the trouble. A threshold cannot drift from the SQL that
applies it, because there is only one of each. And ``gold_verify.py`` can run
the very same query the pipeline ran, against the very same silver input, and
compare -- which is how the fifth acceptance criterion, *marts rebuild
deterministically from the same silver input*, becomes an assertion rather than
a hope.

## What a mart is allowed to read

Three tables: ``silver_synthetic.orders``, ``silver_synthetic.order_items``,
and ``silver_harvested.menu_items`` for the one column that says which items are
entrees.

**Never ``demo_visitors``.** RFC-001 §04 answers PRD Q2 -- may a visitor edit
their persona? -- by containment rather than by asking nicely: the three fields
a visitor may edit (``display_name``, ``home_store_override``,
``stated_preferences``) are all columns of ``demo_visitors``, and no editable
field is an input to a mart, so no edit can invalidate one. The RFC says a
reviewer checks the property by confirming nothing under the medallion pipeline
selects from that table. :data:`FORBIDDEN_SOURCES` is that check, and
``test_gold.py`` runs it over every query here and over the notebooks.

``customer_360.favourite_store`` is derived from ``orders.store_id`` and may
therefore legitimately disagree with a visitor's ``home_store_override``: an
override changes where the next order would be priced, not where past orders
happened. The RFC is explicit that the serving layer says so rather than
reconciling silently, and this layer is not the serving layer.

The ledger is not read either. None of the four marts carries a points balance
-- RFC-001 §04 does not give one a column, and stored value reaches the visitor
through ``persona_fixtures.points_balance`` and issue #27's reconciliation.

## One set of orders, so that every count agrees

An order that was cancelled never happened, and an order that was refunded had
its money returned. :data:`SETTLED_STATUSES` is the one rule, applied to all
four marts: a mart counts settled orders and nothing else. That is the same set
``chip_chat.data_gen`` already lets earn loyalty points, and the tests assert
the two agree.

Applying it *everywhere* rather than per-mart is deliberate. Two columns both
called ``order_count`` that count different orders is exactly the sort of thing
that produces a confident wrong answer in conversation, so
``sum(spend_summary.order_count) = customer_360.order_count`` holds for every
visitor -- and ``gold_verify.py`` asserts it.

The consequence to know about: ``persona_fixtures`` measures the same customers
over *all* their orders, because ``chip_chat.data_gen.fixtures`` selects
exemplars before any of this exists. Roughly four orders in a hundred are
cancelled or refunded, so a fixture's ``usual_share`` and this layer's
``confidence`` are computed over slightly different denominators. They are
already two different definitions on purpose -- see below -- and this is one
more reason not to compare them as though they were one.

## The clock a mart is measured against

``lapsed_flag`` and ``cadence_days`` need a "now", and ``current_timestamp()``
is the wrong one: it would make a customer lapse further every day, so a mart
rebuilt from unchanged silver would not equal the mart it replaced, and the
fifth acceptance criterion would be false by construction.

So the instant is read out of the data -- :data:`AS_OF`, the latest settled
order in the population. That is the same call ``chip_chat.data_gen`` makes
when it measures ``days_since_order`` against the generated window's fixed end
rather than against the wall clock, and for the same reason: a measurement that
moves on its own is not reproducible.

:data:`DERIVED_AT` is the other half of the same argument and is the only
column here whose value is the wall clock. RFC-001 §10 requires a failed
nightly job to serve stale marts **with their timestamp**, never silently as
fresh, so every mart carries one -- including the three whose §04 schema does
not name it.

## What ``usual_order.confidence`` is

The issue is blunt that this field carries the most weight: the Explorer
persona genuinely does not have a usual order, and the product requirement is
that Cilantro can state a visitor's usual *and briefly how it worked that out*,
which means a low confidence has to produce an honest hedge rather than a
confident wrong answer.

The metric is the **lower bound of the Wilson score interval at
:data:`CONFIDENCE_LEVEL`** for the proportion of a customer's settled orders
that are exactly their commonest basket. In words: *the share we could still
defend if this customer's history were an unlucky sample.* It is not the raw
share, and the difference is the whole point -- two orders out of three is a
share of 67% and evidence of nothing, and this metric says so where a share
cannot. :func:`confidence` is that number in Python and
:func:`confidence_expression` is the same arithmetic in SQL; the tests run them
against each other.

:data:`CONFIDENCE_BANDS` is what a value means in words, which is the other
half of what the issue asks for. The bands are what the serving layer renders,
and they are calibrated against the population's own fixture bounds: the
Regular clears :data:`STATED` with room, and the Explorer cannot reach it at
any sample size. ``test_gold.py`` reads those bounds out of ``population.toml``
and asserts exactly that, so a retune of the generator that broke the
separation would fail ``make ci`` rather than surface as a wrong answer in a
demo.

## Deterministic means the ties break on the data

Every aggregation that picks *one* of something -- a favourite store, a usual
basket -- breaks its ties on the identity of the thing chosen, never on
arrival order. ``first()`` over an unordered group is reproducible only until
the files underneath it are rewritten, which is the kind of bug that surfaces
as a customer's usual order changing for no reason a month later.

For the same reason, every number here is computed in exact arithmetic where it
can be: ``lift`` is a ratio of four integers evaluated as one decimal division,
and money is ``DECIMAL`` all the way through, so no mart's value depends on the
order Spark happened to add things up in.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from math import sqrt
from typing import Final

__all__ = [
    "AS_OF",
    "CADENCE",
    "CALIBRATION",
    "CONFIDENCE",
    "CONFIDENCE_BANDS",
    "CONFIDENCE_LEVEL",
    "COUNT",
    "DERIVED_AT",
    "ENTREE_CATEGORY",
    "FORBIDDEN_SOURCES",
    "HEDGED",
    "LAPSED_AFTER_DAYS",
    "LAYER",
    "LIFT",
    "MARTS",
    "MINIMUM_CO_ORDERS",
    "MONEY",
    "NO_USUAL",
    "PERIOD_FORMAT",
    "PERIOD_PATTERN",
    "RFC_COLUMNS",
    "SETTLED_STATUSES",
    "SOURCE_LAYER",
    "STATED",
    "STREAM",
    "STREAMS",
    "Band",
    "Calibration",
    "Column",
    "Expectation",
    "Mart",
    "Source",
    "band",
    "column_names",
    "confidence",
    "confidence_expression",
    "expectations",
    "mart",
    "marts_for",
    "query",
    "schema_name",
    "sources",
]

LAYER: Final = "gold"
"""The medallion layer everything in this module writes to."""

SOURCE_LAYER: Final = "silver"
"""The layer everything in this module reads.

Gold reads silver and never bronze, for the reason silver reads bronze and
never the landing zone: a mart computed from what arrived rather than from what
is true would be a mart that quietly disagrees with the layer a human reads.
"""

STREAMS: Final[tuple[str, ...]] = ("harvested", "synthetic")
"""The two populations, as plain strings.

``chip_chat.databricks.catalog.STREAMS`` is the definition and this is a copy,
for the reason the module docstring gives. ``test_gold.py`` asserts they agree.
"""

STREAM: Final = "synthetic"
"""The stream all four marts are published into.

They describe generated customers, so they are synthetic however real the
catalogue they were composed from is. ``gold_harvested`` holds the versioned
catalogue and the chunked corpus, which are other issues' output.
"""

FORBIDDEN_SOURCES: Final[tuple[str, ...]] = ("demo_visitors",)
"""Tables no mart may read, whatever it wants from them.

One entry, and it is the mechanism behind RFC-001 §04's answer to PRD Q2. The
three fields a visitor may edit are columns of ``demo_visitors``; a mart that
reads none of them cannot be invalidated by an edit; and the way a reviewer
checks that is by confirming nothing under the medallion pipeline selects from
this table. ``test_gold.py`` is that reviewer, over the queries below and over
both notebooks.
"""

SETTLED_STATUSES: Final[tuple[str, ...]] = ("COMPLETED",)
"""The order statuses a mart counts.

A copy of ``chip_chat.data_gen.config.OrderConfig.settled_statuses``, which is
the set that earns loyalty points, and the tests assert they are equal. A
cancelled order never happened and a refunded one had its money returned;
neither is spend, and neither is evidence about what somebody usually orders.

Applied to every mart rather than to the money ones only, so that no two
columns called ``order_count`` count different orders. See the module docstring.
"""

ENTREE_CATEGORY: Final = "Entree"
"""The ``menu_items.category`` that marks a composed entree.

A copy of ``catalogue.entree_category`` in ``population.toml``, asserted equal
in the tests. It is read for one purpose: a usual order names the entree of the
usual basket, and "your usual is a bag of chips" is a worse answer than
admitting there is no usual entree. The same call
``chip_chat.data_gen.fixtures`` makes.
"""

LAPSED_AFTER_DAYS: Final = 90
"""Days of silence after which ``customer_360.lapsed_flag`` is true.

A copy of ``texture.lapsed_after_days`` in ``population.toml``, asserted equal
in the tests. Measured from the customer's last settled order to :data:`AS_OF`
and never to the wall clock -- the same reason the generator measures it
against its window's fixed end.
"""

MINIMUM_CO_ORDERS: Final = 25
"""How many orders two items must share before ``item_affinity`` has a row.

Lift is a ratio, and a ratio computed from three co-occurrences is a number
with an enormous error bar and a confident face. The recommender in
[#37](https://github.com/gganssle/chip_chat/issues/37) trains on this table and
the agent ranks with it, so a pair that has been ordered together twice is
noise that would arrive at a visitor as a suggestion.

There is no support column to filter on downstream, because RFC-001 §04 gives
this mart exactly three columns. The threshold is therefore applied here, where
it can be read, and printed by ``gold_verify.py`` beside the number of pairs it
excluded, so that "we dropped the rare ones" is a number rather than a claim.
"""

PERIOD_FORMAT: Final = "yyyy-MM"
"""The grain of ``spend_summary.period``, as a Spark date format.

Calendar months, in UTC. The question the mart answers is the one a visitor
asks -- *how much did I spend last month* -- and a week has no name a person
recognises. ``period`` is a string rather than a date because it is a label for
a bucket rather than an instant, and RFC-001 §04 names it in a list of
identifiers.
"""

PERIOD_PATTERN: Final = r"^[0-9]{4}-[0-9]{2}$"
"""What a ``period`` must look like. The expectation that :data:`PERIOD_FORMAT`
really produced it, applied to the published rows."""

AS_OF: Final = "observed_through"
"""The name of the instant a mart is measured against, inside its query.

The latest settled order in the population. Not ``current_timestamp()``: see
the module docstring, and the fifth acceptance criterion.
"""

DERIVED_AT: Final = "derived_at"
"""When the mart update ran. The one wall-clock value in this layer.

RFC-001 §10 requires a failed nightly job to serve stale marts *with their
timestamp*, never silently as fresh, so every mart carries this column --
including the three whose §04 schema does not name it. Issue #36's fourth
acceptance criterion asks for it on every row, and the expectation derived from
:attr:`Mart.required` is what enforces that.
"""

MONEY: Final = "DECIMAL(12,2)"
"""What a summed price becomes.

Two more digits than silver's ``DECIMAL(10,2)`` and the same scale: these are
sums of eighteen months of orders rather than the price of one thing, and a
sum that overflows its type in Spark is a null rather than an error. Exact
rather than floating for the reason silver gives, plus one this layer adds:
a ``DOUBLE`` sum is order-dependent in the last bits, and this layer's fifth
criterion is that a rebuild reproduces the row.
"""

COUNT: Final = "BIGINT"
"""What a count of orders becomes. Named so the declarations do not disagree."""

CADENCE: Final = "DECIMAL(8,2)"
"""What ``customer_360.cadence_days`` becomes. Days, to a hundredth."""

CONFIDENCE: Final = "DECIMAL(5,4)"
"""What ``usual_order.confidence`` becomes.

A proportion in ``[0, 1]`` to four places. Decimal rather than double so that
two runs of the same arithmetic over the same input produce the same digits,
which is the fifth acceptance criterion applied to the one column here computed
through a square root.
"""

LIFT: Final = "DECIMAL(12,6)"
"""What ``item_affinity.lift`` becomes. Exact, for the same reason."""

CONFIDENCE_LEVEL: Final = 0.95
"""The confidence level of the Wilson interval whose lower bound is the metric."""

_Z: Final = 1.96
"""The standard normal quantile for :data:`CONFIDENCE_LEVEL`, two-sided."""

_Z_SQUARED: Final = 3.8416
"""``_Z`` squared, written out rather than multiplied.

``1.96 * 1.96`` is ``3.8415999999999997`` in binary floating point and
``3.8416`` in the arithmetic anybody checking this by hand will do. Writing the
exact decimal here is what lets :func:`confidence` and
:func:`confidence_expression` agree to the last place instead of nearly.
"""


# --- What a value means in words ---------------------------------------------


@dataclass(frozen=True, slots=True)
class Band:
    """One reading of a confidence, and the sentence it licenses.

    Attributes:
        name: The band's name, as the serving layer refers to it.
        floor: The lowest confidence in this band, inclusive.
        meaning: What a value in this band means, in words. The issue asks for
            exactly this: *document what a given value means in words*.
        licence: What the assistant may say. The operative half -- a band is
            only useful if it changes the sentence.
    """

    name: str
    floor: Decimal
    meaning: str
    licence: str


STATED: Final = Band(
    name="stated",
    floor=Decimal("0.60"),
    meaning=(
        "this customer orders the same basket often enough, and has ordered "
        "enough times, that the pattern would survive an unlucky sample"
    ),
    licence=(
        "name the usual order plainly, and say how it was worked out: it is "
        "what they order most, over this many orders"
    ),
)
"""The Regular's band. The one turn-to-reorder in PRD requirement P1 needs it.

The floor is 0.60 and it is chosen against the population rather than picked:
``[personas.fixture]`` admits a Regular only above an 85% usual share over at
least thirty orders, and the lower bound of a 95% Wilson interval at that
extreme is 0.70. The Regular clears this band with room at its own worst case,
which is what ``test_gold.py`` asserts.
"""

HEDGED: Final = Band(
    name="hedged",
    floor=Decimal("0.25"),
    meaning=(
        "there is a favourite here, but the customer varies it often enough "
        "that stating it flatly would be overclaiming"
    ),
    licence=(
        "offer it as a suggestion rather than as a fact -- 'you often go for' "
        "-- and make the alternative easy to reach"
    ),
)
"""The band between the two, and the one that stops this from being a switch."""

NO_USUAL: Final = Band(
    name="no_usual",
    floor=Decimal("0"),
    meaning=(
        "this customer does not have a usual order; the commonest basket is "
        "the commonest of many rather than a habit"
    ),
    licence=(
        "say so. 'I am not sure what your usual is' is the correct answer and "
        "the row is still worth having, because what is in it is what to "
        "suggest they had last"
    ),
)
"""The Explorer's band, and a feature rather than a gap.

``[personas.fixture]`` admits an Explorer only at or below a 15% usual share.
A Wilson lower bound is never above the share it is computed from, so an
Explorer cannot reach :data:`HEDGED` at any sample size -- which is what
``test_gold.py`` asserts, and what makes the honest-hedge path in PRD §02
reachable by data rather than by a special case in a prompt.
"""

CONFIDENCE_BANDS: Final[tuple[Band, ...]] = (STATED, HEDGED, NO_USUAL)
"""Every band, highest floor first. :func:`band` reads a value against them."""


@dataclass(frozen=True, slots=True)
class Calibration:
    """One archetype the bands are calibrated against, and where it must land.

    The issue's third acceptance criterion is that ``confidence`` is
    calibrated -- *the Regular scores high, the Explorer scores low, and the
    boundary is documented*. A criterion phrased about two archetypes is only
    checkable if the archetypes are named somewhere a check can read, so they
    are named here.

    Two things read this. ``test_gold.py`` finds each ``persona_id`` in
    ``population.toml``, takes the fixture bounds that archetype admits a
    customer on, and asserts that :func:`confidence` at that extreme lands in
    :attr:`expected` -- which turns a retune of the generator into a CI failure
    rather than a wrong answer in a demo. ``gold_verify.py`` finds the same
    archetype's fixtures in the live ``persona_fixtures`` table and asserts the
    published mart put them there too.

    Attributes:
        persona_id: The archetype, as ``population.toml`` names it.
        expected: The band every fixture of that archetype must land in.
        why: What the archetype demonstrates, and what breaks if it does not.
    """

    persona_id: str
    expected: Band
    why: str


CALIBRATION: Final[tuple[Calibration, ...]] = (
    Calibration(
        persona_id="regular",
        expected=STATED,
        why=(
            "PRD requirement P1 is a reorder in one turn, which is only "
            "reachable if the assistant can state the usual without hedging"
        ),
    ),
    Calibration(
        persona_id="explorer",
        expected=NO_USUAL,
        why=(
            "PRD section 02's Explorer exists to exercise the honest 'I am not "
            "sure what your usual is' path, and a confident answer there is the "
            "exact failure the field was added to prevent"
        ),
    ),
)
"""The two archetypes the bands are calibrated against, and where each lands.

The other five archetypes are deliberately absent. They fall wherever their own
histories put them, which is the point of a measured confidence rather than a
lookup table -- an Office Manager who happens to order the same thing every
Wednesday should read as a Regular does, because they behave like one.
"""


def band(value: Decimal | float | int) -> Band:
    """Return the band ``value`` falls in.

    Args:
        value: A confidence, in ``[0, 1]``.

    Returns:
        The highest band whose floor ``value`` reaches.

    Raises:
        ValueError: If ``value`` is outside ``[0, 1]``, which is not a
            confidence this module could have produced.
    """
    reading = Decimal(str(value))
    if not Decimal(0) <= reading <= Decimal(1):
        raise ValueError(f"a confidence is in [0, 1]; got {value!r}")
    for candidate in CONFIDENCE_BANDS:
        if reading >= candidate.floor:
            return candidate
    raise AssertionError("NO_USUAL has a floor of zero and cannot be missed")


def confidence(repeats: int, orders: int) -> Decimal:
    """Return the confidence that ``repeats`` of ``orders`` is a habit.

    The lower bound of the Wilson score interval at :data:`CONFIDENCE_LEVEL`
    for the proportion ``repeats / orders``, rounded to :data:`CONFIDENCE`'s
    scale.

    This is the Python half of a definition whose SQL half is
    :func:`confidence_expression`. Neither is the authority: the tests run them
    against each other over a grid, because a metric that two implementations
    disagree about is a metric nobody can check.

    Why a lower bound rather than the share itself: two orders out of three is
    a share of 67% and evidence of nothing, and a metric that cannot tell those
    apart is one that will state an Explorer's accidental favourite as their
    usual. ``chip_chat.data_gen.records.PersonaFixture.usual_share`` is the raw
    share and its docstring is explicit that it is deliberately not called
    confidence; this is the reason.

    Args:
        repeats: How many of the customer's settled orders were the basket.
        orders: How many settled orders they placed. At least one.

    Returns:
        The bound, quantized to four decimal places and clamped to ``[0, 1]``.

    Raises:
        ValueError: If ``orders`` is not positive, or ``repeats`` is negative
            or exceeds ``orders``. Every one of those is a caller that has
            already lost track of what it is counting.
    """
    if orders < 1:
        raise ValueError(f"a confidence needs at least one order; got {orders!r}")
    if not 0 <= repeats <= orders:
        raise ValueError(f"{repeats!r} of {orders!r} orders is not a proportion")
    proportion = repeats / orders
    centre = proportion + _Z_SQUARED / (2 * orders)
    margin = _Z * sqrt(
        proportion * (1 - proportion) / orders + _Z_SQUARED / (4 * orders * orders)
    )
    bound = (centre - margin) / (1 + _Z_SQUARED / orders)
    clamped = min(1.0, max(0.0, bound))
    return Decimal(repr(clamped)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def confidence_expression(repeats: str, orders: str) -> str:
    """Return the SQL that computes :func:`confidence` over two columns.

    Args:
        repeats: The column holding how many orders were the basket.
        orders: The column holding how many orders there were.

    Returns:
        A SQL expression of type :data:`CONFIDENCE`.
    """
    proportion = f"({repeats} / {orders})"
    return (
        f"CAST(GREATEST(0, LEAST(1, "
        f"(({proportion} + {_Z_SQUARED} / (2 * {orders}))"
        f" - {_Z} * sqrt({proportion} * (1 - {proportion}) / {orders}"
        f" + {_Z_SQUARED} / (4 * {orders} * {orders})))"
        f" / (1 + {_Z_SQUARED} / {orders})"
        f")) AS {CONFIDENCE})"
    )


# --- Declarations -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """One silver table a mart reads, and the placeholder that names it.

    Attributes:
        alias: The ``{placeholder}`` in the mart's SQL. :func:`query` replaces
            it with the fully qualified silver name, so no table name is ever
            written as a literal in a query.
        stream: Which stream the table is in.
        table: The silver table, unqualified.
        why: What the mart needs from it.
    """

    alias: str
    stream: str
    table: str
    why: str


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a mart.

    Attributes:
        name: The column name. For the columns RFC-001 §04 names, it is that
            name exactly -- the agent's read tools query these by name.
        sql_type: What it is, as the published table declares it.
        why: What the number means, for the reader of the catalogue browser.
            This is the column's definition and there is nowhere else it is
            written down.
    """

    name: str
    sql_type: str
    why: str


@dataclass(frozen=True, slots=True)
class Expectation:
    """One constraint every row of a mart must satisfy.

    There is no action field, for the reason :class:`silver.Expectation` has
    none: every expectation in this layer is applied with
    ``expect_all_or_fail``. A mart is what the agent answers from, so a row
    that violates its own definition is a wrong answer in conversation rather
    than a warning in an event log.

    Attributes:
        name: The expectation's name, as it appears in the event log. A
            statement of what is true rather than of what went wrong.
        constraint: A SQL boolean expression over the mart's own columns.
        why: Why this is worth stopping a pipeline for.
    """

    name: str
    constraint: str
    why: str


@dataclass(frozen=True, slots=True)
class Mart:
    """One published mart.

    Attributes:
        name: The gold table name, unqualified. One of RFC-001 §04's four.
        stream: The stream it is published into.
        grain: What one row is, in words. The first thing to read.
        columns: Every column, in the order the query produces them.
        required: Columns that may not be null, which become expectations.
        sources: The silver tables the query reads.
        template: The SQL, with ``{placeholder}`` names for every table and
            every threshold. Never formatted directly -- :func:`query` is what
            fills it, and it is the only thing that knows what a placeholder
            may be.
        comment: What the mart holds, for the Unity Catalog comment.
        expectations: Constraints beyond the ones derived from ``required``.
    """

    name: str
    stream: str
    grain: str
    columns: tuple[Column, ...]
    sources: tuple[Source, ...]
    template: str
    comment: str
    required: tuple[str, ...] = ()
    expectations: tuple[Expectation, ...] = ()


RFC_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "customer_360": (
        "demo_id",
        "order_count",
        "lifetime_spend",
        "last_order_at",
        "favourite_store",
        "cadence_days",
        "lapsed_flag",
    ),
    "usual_order": ("demo_id", "item_id", "modifiers", "confidence", "derived_at"),
    "item_affinity": ("item_id", "related_item_id", "lift"),
    "spend_summary": ("demo_id", "period", "total", "order_count"),
}
"""RFC-001 §04's schema for the four marts, transcribed.

Here as data rather than as a comment so that ``test_gold.py`` can hold the
declarations below to it. The issue's brief is that the schemas are fixed and
must be matched exactly, because the agent's read tools query these columns by
name; a rename that a reviewer would have to notice is a rename that fails
``make ci`` instead.

:data:`DERIVED_AT` is the one addition, and only where §04 did not already name
it. RFC-001 §10 requires every mart to be servable stale with its own
timestamp, and a mart with nowhere to put one cannot be.
"""

_ORDERS: Final = Source(
    alias="orders",
    stream="synthetic",
    table="orders",
    why="who ordered, when, where, for how much, and whether it settled",
)

_ORDER_ITEMS: Final = Source(
    alias="order_items",
    stream="synthetic",
    table="order_items",
    why="what was in the order, and how each line was built",
)

_MENU_ITEMS: Final = Source(
    alias="menu_items",
    stream="harvested",
    table="menu_items",
    why=(
        "one column: which items are entrees. A usual order names the entree "
        "of the usual basket, and nothing in the synthetic stream knows which "
        "of its item ids those are"
    ),
)

# The settled-orders CTE, shared by all four queries so that the one rule in
# `SETTLED_STATUSES` is applied once and identically. `{orders}` and
# `{settled}` are filled by `query()`.
_SETTLED = """settled AS (
        SELECT order_id, demo_id, store_id, placed_at, total
        FROM {orders}
        WHERE status IN ({settled})
    )"""

_CUSTOMER_360 = (
    """WITH """
    + _SETTLED
    + """,
    -- The instant the population was observed through: the latest settled
    -- order there is. Not `current_timestamp()`, which would make every
    -- customer lapse further every day and make a rebuild disagree with the
    -- mart it replaced.
    as_of AS (SELECT max(placed_at) AS {as_of} FROM settled),
    by_store AS (
        SELECT demo_id, store_id, count(*) AS orders_here
        FROM settled GROUP BY demo_id, store_id
    ),
    favourite AS (
        -- max() over a struct compares field by field, so this reads "the most
        -- orders there, and the lowest store id among ties". The negation is
        -- what turns the second comparison around. A tie broken by arrival
        -- order would make a customer's favourite store change when the files
        -- underneath are rewritten.
        SELECT
            demo_id,
            -(max(struct(orders_here AS placed, -store_id AS ranked)).ranked)
                AS favourite_store
        FROM by_store GROUP BY demo_id
    ),
    measured AS (
        SELECT
            s.demo_id,
            count(*) AS order_count,
            CAST(sum(s.total) AS {money}) AS lifetime_spend,
            max(s.placed_at) AS last_order_at,
            min(s.placed_at) AS first_order_at
        FROM settled s GROUP BY s.demo_id
    )
    SELECT
        m.demo_id,
        CAST(m.order_count AS {count}) AS order_count,
        m.lifetime_spend,
        m.last_order_at,
        f.favourite_store,
        -- Mean days between consecutive orders. Null for a customer who has
        -- ordered once, because one order is not a cadence and zero would read
        -- as "every day".
        CASE WHEN m.order_count > 1 THEN CAST(
            (unix_timestamp(m.last_order_at) - unix_timestamp(m.first_order_at))
            / 86400.0 / (m.order_count - 1) AS {cadence})
        END AS cadence_days,
        datediff(a.{as_of}, m.last_order_at) > {lapsed_after_days} AS lapsed_flag,
        current_timestamp() AS {derived_at}
    FROM measured m
    JOIN favourite f ON f.demo_id = m.demo_id
    CROSS JOIN as_of a"""
)

_USUAL_ORDER = (
    """WITH """
    + _SETTLED
    + """,
    entrees AS (
        SELECT item_id FROM {menu_items} WHERE category = '{entree_category}'
    ),
    -- A line, rendered as text. The basket below is a sorted list of these, and
    -- text is what Spark can sort a list of without depending on how an array
    -- of structs happens to compare.
    lines AS (
        SELECT
            i.order_id,
            concat_ws('|', i.item_id,
                      coalesce(array_join(sort_array(i.modifiers), '+'), ''),
                      CAST(i.qty AS STRING)) AS line_key,
            CASE WHEN e.item_id IS NOT NULL
                 THEN concat_ws('|', i.item_id,
                                coalesce(array_join(sort_array(i.modifiers), '+'), ''))
            END AS entree_key
        FROM {order_items} i
        LEFT JOIN entrees e ON e.item_id = i.item_id
    ),
    baskets AS (
        SELECT
            s.demo_id,
            s.order_id,
            array_join(sort_array(collect_list(l.line_key)), ';') AS basket_key,
            -- The entree of the basket: the lowest by item id, so a family
            -- ordering three of them still has one stable answer, and every
            -- order sharing a basket key agrees about it.
            min(l.entree_key) AS entree_key
        FROM settled s
        JOIN lines l ON l.order_id = s.order_id
        GROUP BY s.demo_id, s.order_id
    ),
    -- The denominator is every settled order, including the ones whose basket
    -- carried no entree. "How often is this what they order" is a question
    -- about all their orders.
    totals AS (SELECT demo_id, count(*) AS order_count FROM baskets GROUP BY demo_id),
    counted AS (
        SELECT demo_id, basket_key, min(entree_key) AS entree_key, count(*) AS repeats
        FROM baskets
        WHERE entree_key IS NOT NULL
        GROUP BY demo_id, basket_key
    ),
    -- Most repeats wins; the basket's own key breaks the tie. A customer whose
    -- two commonest baskets are level gets the same answer every rebuild.
    winner AS (
        SELECT demo_id, max(struct(repeats AS repeats, basket_key AS basket_key,
                                  entree_key AS entree_key)) AS best
        FROM counted GROUP BY demo_id
    ),
    resolved AS (
        SELECT
            w.demo_id,
            split_part(w.best.entree_key, '|', 1) AS item_id,
            filter(split(split_part(w.best.entree_key, '|', 2), '\\\\+'),
                   m -> m <> '') AS modifiers,
            w.best.repeats AS repeats,
            t.order_count AS order_count
        FROM winner w JOIN totals t ON t.demo_id = w.demo_id
    )
    SELECT
        demo_id,
        item_id,
        modifiers,
        {confidence} AS confidence,
        current_timestamp() AS {derived_at}
    FROM resolved"""
)

_ITEM_AFFINITY = (
    """WITH """
    + _SETTLED
    + """,
    -- One row per (order, item). DISTINCT because two lines of the same item
    -- built two ways is one item in that order, and lift is about which items
    -- turn up together rather than how many of each.
    in_order AS (
        SELECT DISTINCT s.order_id, i.item_id
        FROM settled s JOIN {order_items} i ON i.order_id = s.order_id
    ),
    corpus AS (SELECT count(DISTINCT order_id) AS orders FROM in_order),
    per_item AS (
        SELECT item_id, count(*) AS orders_with FROM in_order GROUP BY item_id
    ),
    pairs AS (
        SELECT a.item_id AS item_id, b.item_id AS related_item_id,
               count(*) AS orders_with_both
        FROM in_order a
        JOIN in_order b ON b.order_id = a.order_id AND b.item_id <> a.item_id
        GROUP BY a.item_id, b.item_id
    )
    SELECT
        p.item_id,
        p.related_item_id,
        -- lift = P(both) / (P(a) * P(b)), which cancels to four integers and
        -- one division. Written that way on purpose: three float divisions
        -- multiplied together is a number whose last digits depend on the
        -- order Spark evaluated them in, and this layer's fifth criterion is
        -- that a rebuild reproduces the row.
        CAST(CAST(p.orders_with_both AS DECIMAL(38,0)) * c.orders
             / (CAST(x.orders_with AS DECIMAL(38,0)) * y.orders_with)
             AS {lift}) AS lift,
        current_timestamp() AS {derived_at}
    FROM pairs p
    JOIN per_item x ON x.item_id = p.item_id
    JOIN per_item y ON y.item_id = p.related_item_id
    CROSS JOIN corpus c
    WHERE p.orders_with_both >= {minimum_co_orders}"""
)

_SPEND_SUMMARY = (
    """WITH """
    + _SETTLED
    + """
    SELECT
        demo_id,
        date_format(placed_at, '{period_format}') AS period,
        CAST(sum(total) AS {money}) AS total,
        CAST(count(*) AS {count}) AS order_count,
        current_timestamp() AS {derived_at}
    FROM settled
    GROUP BY demo_id, date_format(placed_at, '{period_format}')"""
)


MARTS: Final[tuple[Mart, ...]] = (
    Mart(
        name="customer_360",
        stream=STREAM,
        grain="one row per visitor who has ever placed a settled order",
        columns=(
            Column(
                name="demo_id",
                sql_type="STRING",
                why="whose account this is. What #43's row access policy compares",
            ),
            Column(
                name="order_count",
                sql_type=COUNT,
                why="settled orders, all time. Cancelled and refunded are not counted",
            ),
            Column(
                name="lifetime_spend",
                sql_type=MONEY,
                why="what those orders totalled, exactly",
            ),
            Column(
                name="last_order_at",
                sql_type="TIMESTAMP",
                why="their most recent settled order. What lapsed_flag is measured from",
            ),
            Column(
                name="favourite_store",
                sql_type="INT",
                why=(
                    "the store most of their orders were placed at, ties broken "
                    "on the lowest store id. Derived from orders.store_id, and "
                    "may legitimately disagree with a visitor's "
                    "home_store_override -- RFC-001 §04 says the serving layer "
                    "says so rather than reconciling silently"
                ),
            ),
            Column(
                name="cadence_days",
                sql_type=CADENCE,
                why=(
                    "mean days between consecutive settled orders. Null for a "
                    "customer with one order, because one order is not a cadence"
                ),
            ),
            Column(
                name="lapsed_flag",
                sql_type="BOOLEAN",
                why=(
                    "more than LAPSED_AFTER_DAYS of silence, measured to the "
                    "population's latest settled order and never to the wall clock"
                ),
            ),
            Column(
                name=DERIVED_AT,
                sql_type="TIMESTAMP",
                why="when this row was computed. RFC-001 §10 serves it with the row",
            ),
        ),
        required=(
            "demo_id",
            "order_count",
            "lifetime_spend",
            "last_order_at",
            "favourite_store",
            "lapsed_flag",
            DERIVED_AT,
        ),
        sources=(_ORDERS,),
        template=_CUSTOMER_360,
        expectations=(
            Expectation(
                name="counts_at_least_one_order",
                constraint="order_count > 0",
                why=(
                    "the grain is a customer who has ordered; a row for one who "
                    "has not is a row every other column of which is a guess"
                ),
            ),
            Expectation(
                name="spent_a_positive_amount",
                constraint="lifetime_spend > 0",
                why=(
                    "silver already refuses an order that totalled nothing, so "
                    "a customer whose orders sum to zero is an arithmetic fault "
                    "in this layer rather than a quiet customer"
                ),
            ),
            Expectation(
                name="a_cadence_is_a_gap_or_it_is_absent",
                constraint="cadence_days IS NULL OR cadence_days > 0",
                why=(
                    "a cadence of zero would read as 'orders every day' and is "
                    "what a first-order and last-order collision would produce"
                ),
            ),
        ),
        comment=(
            "One row per visitor who has ordered. Counts and money are over "
            "SETTLED orders only, so sum(spend_summary.order_count) equals "
            "order_count here. favourite_store is derived from orders.store_id "
            "and may disagree with a visitor's home_store_override on purpose. "
            "lapsed_flag is measured against the latest settled order in the "
            "population, never the wall clock. Built by gh-36."
        ),
    ),
    Mart(
        name="usual_order",
        stream=STREAM,
        grain=(
            "one row per visitor whose settled orders include at least one "
            "basket carrying an entree"
        ),
        columns=(
            Column(name="demo_id", sql_type="STRING", why="whose usual this is"),
            Column(
                name="item_id",
                sql_type="STRING",
                why=(
                    "the entree of their commonest settled basket, lowest item "
                    "id first where a basket carries several"
                ),
            ),
            Column(
                name="modifiers",
                sql_type="ARRAY<STRING>",
                why="how they have it built, sorted. Empty rather than null",
            ),
            Column(
                name="confidence",
                sql_type=CONFIDENCE,
                why=(
                    "the lower bound of a 95% Wilson interval on the share of "
                    "their settled orders that are exactly this basket -- the "
                    "share that survives an unlucky sample. Read it through "
                    "CONFIDENCE_BANDS: at or above 0.60 state it, above 0.25 "
                    "hedge it, below that say there is no usual"
                ),
            ),
            Column(
                name=DERIVED_AT,
                sql_type="TIMESTAMP",
                why=(
                    "when this row was computed. Surfaced to the visitor with "
                    "the answer (PRD P1) and used for staleness (RFC-001 §10)"
                ),
            ),
        ),
        required=("demo_id", "item_id", "modifiers", "confidence", DERIVED_AT),
        sources=(_ORDERS, _ORDER_ITEMS, _MENU_ITEMS),
        template=_USUAL_ORDER,
        expectations=(
            Expectation(
                name="a_confidence_is_a_proportion",
                constraint="confidence >= 0 AND confidence <= 1",
                why=(
                    "the bands read it as one, and a value outside the range "
                    "would land in a band by accident rather than by measurement"
                ),
            ),
        ),
        comment=(
            "One row per visitor with a usual, and a visitor without one gets a "
            "row saying so rather than no row: confidence is the lower bound of "
            "a 95% Wilson interval on the share of their settled orders that "
            "are this basket, so an Explorer scores low and the assistant "
            "hedges. Read it through gold.CONFIDENCE_BANDS. A visitor whose "
            "orders carry no entree at all has no row, which is the honest "
            "absence. Built by gh-36."
        ),
    ),
    Mart(
        name="item_affinity",
        stream=STREAM,
        grain=(
            "one row per ordered pair of items that have appeared together in "
            "at least MINIMUM_CO_ORDERS settled orders"
        ),
        columns=(
            Column(name="item_id", sql_type="STRING", why="the item asked about"),
            Column(
                name="related_item_id",
                sql_type="STRING",
                why="the item that turns up with it. Never the same item",
            ),
            Column(
                name="lift",
                sql_type=LIFT,
                why=(
                    "P(both) / (P(item) * P(related)) over settled orders. One "
                    "means independent, above one means they go together, below "
                    "one means they are ordered instead of each other. "
                    "Symmetric: the reversed pair carries the same number"
                ),
            ),
            Column(
                name=DERIVED_AT,
                sql_type="TIMESTAMP",
                why="when this row was computed",
            ),
        ),
        required=("item_id", "related_item_id", "lift", DERIVED_AT),
        sources=(_ORDERS, _ORDER_ITEMS),
        template=_ITEM_AFFINITY,
        expectations=(
            Expectation(
                name="relates_two_different_items",
                constraint="item_id <> related_item_id",
                why=(
                    "an item's lift against itself is a number with no meaning "
                    "and the largest one in the table, so it would sit at the "
                    "top of every recommendation"
                ),
            ),
            Expectation(
                name="lifts_by_a_positive_factor",
                constraint="lift > 0",
                why=(
                    "a pair with a row has been ordered together, so its "
                    "numerator is positive; a zero or a negative is arithmetic "
                    "that went wrong rather than a weak pair"
                ),
            ),
        ),
        comment=(
            "Which items are ordered together, as lift over settled orders. The "
            "only mart here that is not visitor-scoped -- it is a fact about the "
            "population, carries no demo_id and needs no row access policy. "
            "Pairs seen together fewer than MINIMUM_CO_ORDERS times are absent, "
            "because a lift computed from three co-occurrences is noise with a "
            "confident face. Built by gh-36."
        ),
    ),
    Mart(
        name="spend_summary",
        stream=STREAM,
        grain="one row per visitor per calendar month in which they ordered",
        columns=(
            Column(name="demo_id", sql_type="STRING", why="whose spend this is"),
            Column(
                name="period",
                sql_type="STRING",
                why="the calendar month, UTC, as YYYY-MM",
            ),
            Column(
                name="total",
                sql_type=MONEY,
                why="what their settled orders in that month totalled",
            ),
            Column(
                name="order_count",
                sql_type=COUNT,
                why="how many settled orders that was",
            ),
            Column(
                name=DERIVED_AT,
                sql_type="TIMESTAMP",
                why="when this row was computed",
            ),
        ),
        required=("demo_id", "period", "total", "order_count", DERIVED_AT),
        sources=(_ORDERS,),
        template=_SPEND_SUMMARY,
        expectations=(
            Expectation(
                name="is_a_calendar_month",
                constraint=f"period RLIKE '{PERIOD_PATTERN}'",
                why=(
                    "period is the key the serving layer filters on, and a "
                    "month that does not look like one is a format that changed "
                    "under a query nobody re-read"
                ),
            ),
            Expectation(
                name="a_month_with_a_row_had_an_order_in_it",
                constraint="order_count > 0 AND total > 0",
                why=(
                    "the grain is a month they ordered in; a zero row is an "
                    "empty month that should simply be absent, and absent is "
                    "what the serving layer already handles"
                ),
            ),
        ),
        comment=(
            "Spend by calendar month, UTC, over settled orders. Months rather "
            "than weeks because the question a visitor asks is 'how much did I "
            "spend last month'. A month with no settled order has no row. "
            "Built by gh-36."
        ),
    ),
)
"""Every published mart, in RFC-001 §04's order."""


# --- Lookups ------------------------------------------------------------------


def schema_name(stream: str) -> str:
    """Return the unqualified schema for ``stream`` in the gold layer.

    Args:
        stream: One of :data:`STREAMS`.

    Returns:
        ``gold_harvested`` or ``gold_synthetic``.

    Raises:
        ValueError: If ``stream`` is not one of :data:`STREAMS`.
    """
    if stream not in STREAMS:
        raise ValueError(f"unknown stream {stream!r}; expected one of {STREAMS}")
    return f"{LAYER}_{stream}"


def marts_for(stream: str) -> Iterator[Mart]:
    """Yield every mart published into ``stream``, in declaration order.

    Args:
        stream: One of :data:`STREAMS`.

    Yields:
        The matching marts.

    Raises:
        ValueError: If ``stream`` is unknown.
    """
    schema_name(stream)
    for candidate in MARTS:
        if candidate.stream == stream:
            yield candidate


def mart(name: str) -> Mart:
    """Return the mart called ``name``.

    Args:
        name: An unqualified gold table name.

    Returns:
        The mart.

    Raises:
        KeyError: If no mart has that name.
    """
    for candidate in MARTS:
        if candidate.name == name:
            return candidate
    raise KeyError(f"no gold mart is called {name!r}")


def sources() -> tuple[Source, ...]:
    """Return every silver table this layer reads, deduplicated and sorted.

    What ``gold_verify.py`` checks it can read before it asserts anything, and
    what a reviewer reads to confirm the containment RFC-001 §04 requires.
    """
    seen = {
        (source.stream, source.table): source
        for candidate in MARTS
        for source in candidate.sources
    }
    return tuple(seen[key] for key in sorted(seen))


def column_names(candidate: Mart) -> tuple[str, ...]:
    """Return ``candidate``'s column names, in order."""
    return tuple(column.name for column in candidate.columns)


def expectations(candidate: Mart) -> tuple[Expectation, ...]:
    """Return every constraint applied to ``candidate``, derived ones included.

    Two sources, in this order: one per required column, and then whatever the
    mart declared for itself. All of them are applied with
    ``expect_all_or_fail`` -- see :class:`Expectation`.

    Args:
        candidate: The mart.

    Returns:
        The expectations, with unique names.

    Raises:
        ValueError: If two expectations end up sharing a name, which would
            leave one of them silently unreported in the event log.
    """
    derived = [
        Expectation(
            name=f"{column}_is_present",
            constraint=f"{column} IS NOT NULL",
            why=(
                f"{column} identifies, scopes or defines the row, and a null "
                "there is a row the serving layer cannot answer from"
            ),
        )
        for column in candidate.required
    ]
    derived += list(candidate.expectations)
    names = [item.name for item in derived]
    if len(names) != len(set(names)):
        raise ValueError(
            f"{candidate.name} declares two expectations with one name: "
            f"{sorted(name for name in names if names.count(name) > 1)}"
        )
    return tuple(derived)


# --- The query ----------------------------------------------------------------


def query(candidate: Mart, resolve: Callable[[str, str], str]) -> str:
    """Return the SQL that builds ``candidate``.

    Every table name and every threshold in :attr:`Mart.template` is a
    placeholder, and this is the only function that fills them. That is what
    stops a threshold from being written twice and drifting, and it is what
    lets ``gold_verify.py`` re-run the pipeline's own query against the
    pipeline's own input.

    Args:
        candidate: The mart.
        resolve: Takes ``(stream, table)`` and returns a fully qualified silver
            name. The notebook passes ``catalog.table`` bound to
            :data:`SOURCE_LAYER`; the tests pass something that records what
            was asked for.

    Returns:
        One SQL statement.

    Raises:
        ValueError: If the template names a placeholder the mart did not
            declare a source for, or one this module has no constant for. Both
            are a query that would otherwise fail on the cluster, minutes into
            an update.
    """
    filled: dict[str, object] = {
        "as_of": AS_OF,
        "cadence": CADENCE,
        "confidence": confidence_expression("repeats", "order_count"),
        "count": COUNT,
        "derived_at": DERIVED_AT,
        "entree_category": ENTREE_CATEGORY,
        "lapsed_after_days": LAPSED_AFTER_DAYS,
        "lift": LIFT,
        "minimum_co_orders": MINIMUM_CO_ORDERS,
        "money": MONEY,
        "period_format": PERIOD_FORMAT,
        "settled": ", ".join(f"'{status}'" for status in SETTLED_STATUSES),
    }
    for source in candidate.sources:
        if source.alias in filled:
            raise ValueError(
                f"{candidate.name} names a source {source.alias!r}, which is "
                "already a threshold placeholder"
            )
        filled[source.alias] = resolve(source.stream, source.table)
    try:
        return candidate.template.format(**filled)
    except KeyError as unknown:
        raise ValueError(
            f"{candidate.name} reads {unknown} but declares no source for it"
        ) from unknown
