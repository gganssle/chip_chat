"""The account lane's semantic view, as data. `sql/11_semantic_view.sql` is the object.

`chip_chat.snowflake.schema` does this for the tables and says why: the SQL is
the only thing that creates anything, and this module creates nothing. What it
adds is the half of #45 that a `CREATE SEMANTIC VIEW` cannot state about
itself -- what was left OUT, and why.

A semantic view is a description of the tables it names. It has no way to
mention the nine tables it does not name, and "curating the view is the work"
is mostly a claim about those nine. :data:`WITHHELD_TABLES` and
:data:`WITHHELD_COLUMNS` are the omissions written down with an argument beside
each, and `tests/test_semantic_view.py` holds three things together through
this module:

**The view is what the DDL says.** Every logical table, fact, dimension, metric
and verified query here is compared against `sql/11_semantic_view.sql`, name and
expression. A metric renamed in one and not the other fails ``make ci``.

**The view is bounded, and the boundary is closed by a test rather than by
care.** Every table in `chip_chat.snowflake.schema` is either exposed here or
withheld here with a reason, and every column of every exposed table likewise.
There is no third state, so a column added to `orders` in #42's DDL fails this
package until somebody decides whether the account lane may see it.

**The refusals are a set, not a hope.** :data:`UNANSWERABLE` is the
deliberately-unanswerable question set issue #45 asks for, each question paired
with the thing the model does not carry. PRD A4 and RFC-001 §10 make an honest
"I cannot answer that reliably" a requirement rather than a nicety, and a
refusal set that nobody wrote down is one nobody can run.

Two facts measured against the live account on 2026-08-27, both of which cost
an hour:

**A verified query must be written against the LOGICAL model.** ``__orders``,
not ``CHIP_CHAT.ACCOUNTS.orders``; ``point_change``, not ``delta``. Physical
SQL is accepted by ``CREATE``, silently rewritten into a CTE per logical table
that projects only the columns the rewriter thought were needed, and then
dropped for the compilation error that rewrite causes -- while the request still
succeeds and still names the verified query as used. The warning is in the
response body and nowhere else.

**The generated SQL is a semantic-view query, not free-form SQL.** Off the
verified path, Cortex Analyst emits ``SELECT * FROM SEMANTIC_VIEW(...)``, which
can only name elements this view declares. That is what makes the boundary
below the actual boundary rather than a strong hint.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final, Literal

from chip_chat.snowflake.account import SchemaName
from chip_chat.snowflake.schema import TABLES

__all__ = [
    "ELEMENTS",
    "LOGICAL_TABLES",
    "RELATIONSHIPS",
    "SETTLED_STATUS",
    "UNANSWERABLE",
    "VERIFIED_QUERIES",
    "VIEW_NAME",
    "VIEW_SCHEMA",
    "WITHHELD_COLUMNS",
    "WITHHELD_TABLES",
    "Element",
    "ElementKind",
    "LogicalTable",
    "Relationship",
    "Unanswerable",
    "VerifiedQuery",
    "elements_of",
    "exposed",
    "logical_table",
    "qualified",
]

VIEW_NAME: Final = "ACCOUNT_LANE"
"""The semantic view. One object, and the account lane has no second one."""

VIEW_SCHEMA: Final[SchemaName] = "ACCOUNTS"
"""It lives with the tables it is mostly about, and CHIP_CHAT_READ already
holds USAGE and SELECT there and in CATALOGUE."""

SETTLED_STATUS: Final = "COMPLETED"
"""The one ``orders.status`` that is spend.

A copy of ``orders.settled_statuses`` in `data-gen/.../population.toml`, which
`databricks.gold.SETTLED_STATUSES` also copies, and the tests assert all three
agree. A cancelled order never happened and a refunded one had its money
returned; if this view counted either, two numbers called ``lifetime_spend``
would appear in one conversation and disagree.

It is spelled into the ``settled_spend``, ``settled_order`` and ``settled_at``
facts rather than left to a generated ``WHERE``, so there is no unfiltered
money metric for a query to reach for.
"""

ElementKind = Literal["FACT", "DIMENSION", "METRIC"]


@dataclass(frozen=True, slots=True)
class LogicalTable:
    """One table the view names, and the business word for it.

    Attributes:
        alias: What the view calls it. This is the name a verified query and a
            generated query both use, prefixed with ``__``.
        schema: Which schema the physical table lives in.
        table: The physical table, as `chip_chat.snowflake.schema` names it.
        key: The declared ``PRIMARY KEY``, which tells Cortex Analyst what one
            row is. Held equal to the table's own declared key by the tests.
        synonyms: What a visitor calls it out loud. People say "points" and
            "rewards" and "how much have I got", never ``loyalty_ledger``.
    """

    alias: str
    schema: SchemaName
    table: str
    key: tuple[str, ...]
    synonyms: tuple[str, ...]


LOGICAL_TABLES: Final[tuple[LogicalTable, ...]] = (
    LogicalTable(
        "orders",
        schema="ACCOUNTS",
        table="orders",
        key=("order_id",),
        synonyms=(
            "orders",
            "my orders",
            "order history",
            "past orders",
            "receipts",
            "visits",
            "times i came in",
        ),
    ),
    LogicalTable(
        "order_lines",
        schema="ACCOUNTS",
        table="order_items",
        key=("order_id", "line_number"),
        synonyms=(
            "order lines",
            "what was in the order",
            "items on my order",
            "line items",
            "what i got",
        ),
    ),
    LogicalTable(
        "points",
        schema="ACCOUNTS",
        table="loyalty_ledger",
        key=("entry_id",),
        synonyms=(
            "points",
            "rewards",
            "loyalty",
            "my points",
            "points history",
            "rewards account",
        ),
    ),
    LogicalTable(
        "items",
        schema="CATALOGUE",
        table="menu_items",
        key=("item_id",),
        synonyms=(
            "items",
            "menu items",
            "dishes",
            "food",
            "what i ordered",
            "products",
        ),
    ),
    LogicalTable(
        "restaurants",
        schema="CATALOGUE",
        table="stores",
        key=("store_id",),
        synonyms=(
            "stores",
            "restaurants",
            "locations",
            "shops",
            "the place i go",
            "branches",
        ),
    ),
)
"""Five tables out of fourteen, in the order the DDL declares them.

Three are the visitor's own rows and two are the catalogue, present only so
that an answer can say "Steak Burrito" and "NH Town 1 Mall" instead of
``CMG-2`` and ``2118``.
"""

WITHHELD_TABLES: Final[dict[tuple[SchemaName, str], str]] = {
    ("CATALOGUE", "item_prices"): (
        "what an item costs today is a menu question. An order already carries "
        "the price it was charged, on order_items.unit_price, so pricing "
        "history against a live list answers 'what did I spend' with a number "
        "the visitor was never charged"
    ),
    ("CATALOGUE", "modifiers"): (
        "the build of a line is order_items.modifiers, an array of "
        "identifiers. Nothing in scope aggregates over it, and a text-to-SQL "
        "system handed an array column will eventually FLATTEN it into a join "
        "nobody wanted"
    ),
    ("CATALOGUE", "rewards"): (
        "the published rewards and their point costs, added by #46 so that "
        "redeem_points can validate a redemption against a catalogue rather "
        "than against a card. Withheld rather than modelled, and the reason is "
        "timing rather than principle: nothing publishes the table yet "
        "(cc-99cn), and a semantic view over an empty table does not decline "
        "to answer -- it answers that the visitor can redeem nothing, which is "
        "a confident wrong answer about the one thing they have points for. "
        "Worth reopening when the publish lands; 'what can I get for my "
        "points' is an account-lane question and this is the table that "
        "answers it"
    ),
    ("CATALOGUE", "rewards_terms"): (
        "four numbers: the earn rate, two expiry windows and the daily "
        "earning cap. Published prose that the knowledge lane already answers "
        "from, with a citation, which is the lane that should -- a rate "
        "surfaced here would be a number with no source_url attached to it in "
        "the sentence the visitor reads. place_order reads it as arithmetic; "
        "nobody should be able to ask it as a question here"
    ),
    ("ACCOUNTS", "action_receipts"): (
        "the retry keys the write path has spent, and the receipts it returned "
        "for them. Write-path bookkeeping about the mechanism rather than "
        "about the visitor's account, and the receipt column is a VARIANT "
        "holding a whole prior receipt -- a generated query that reached it "
        "would surface an answer nobody composed, dated whenever it happened. "
        "'What did I order' is orders and order_items, which are modelled"
    ),
    ("ACCOUNTS", "personas"): (
        "a kind of person rather than a person. Seven archetypes shared by "
        "five hundred customers is not a fact about the visitor in front of you"
    ),
    ("ACCOUNTS", "demo_visitors"): (
        "the three visitor-editable fields live here, and so does the only "
        "mapping from a demo_id to anything resembling a name. The account "
        "lane answers about behaviour; it has no business reading the "
        "nameplate, and PRD A4's refusal is easier to keep when the table is "
        "not in the model at all"
    ),
    ("ACCOUNTS", "demo_visitor_baseline"): (
        "#47's record of what the generator made each customer, so that ageing "
        "a session out can put the editable columns back. Withheld for "
        "demo_visitors' reason and then one of its own: it is not the "
        "visitor's account at all, it is the account they started from, and a "
        "model that could reach both would let a question about 'my usual' be "
        "answered from a row the visitor has already changed"
    ),
    ("ACCOUNTS", "persona_fixtures"): (
        "the roster of which synthetic customer demonstrates which archetype, "
        "read at entry before a visitor is bound. It is about the demo rather "
        "than about the visitor, and it is the one visitor-scoped table a row "
        "access policy would break rather than protect"
    ),
    ("MARTS", "customer_360"): (
        "a gold mart, computed overnight. RFC-001 §10 requires a stale mart to "
        "be served WITH its derived_at rather than silently as fresh, and a "
        "generated query cannot be relied on to carry that into an answer -- a "
        "row mixing last night's lifetime_spend with a live COUNT(*) is stale "
        "and fresh in one sentence. get_usual_order and get_recommendations "
        "read the marts, and they know to say when the number was computed"
    ),
    ("MARTS", "usual_order"): (
        "the same argument, and one more: the usual is a precomputed habit and "
        "the golden set separates it from 'what did I order last time' on "
        "purpose. A model that could answer both from one table would answer "
        "the wrong one"
    ),
    ("MARTS", "item_affinity"): (
        "a fact about two items and about nobody, aggregated over the whole "
        "population. The account lane answers about one visitor; a "
        "population-wide number reached through it is exactly the confident "
        "nonsense #45 exists to prevent"
    ),
    ("MARTS", "spend_summary"): (
        "spend per visitor per month, which `orders` already answers live and "
        "to the day. Two paths to one question is two answers that "
        "occasionally differ, and the live one cannot be stale"
    ),
}
"""The nine tables the view does not name, and the argument for each.

Closed by a test: every table in `chip_chat.snowflake.schema` is either in
:data:`LOGICAL_TABLES` or in here. A fifteenth table added to #42's DDL fails
`make ci` until somebody decides which, which is the point -- the alternative
is a semantic view that grows by accident.
"""

WITHHELD_COLUMNS: Final[dict[tuple[str, str], str]] = {
    ("orders", "demo_id"): (
        "#45's fifth acceptance criterion. Isolation is #43's row access "
        "policy on the base table, and a model that exposed the column would "
        "invite a generated query to filter on a visitor identifier no tool "
        "signature has"
    ),
    ("orders", "priced_restaurant_id"): (
        "whose published prices priced the order, which is how a total is "
        "audited rather than how it is asked about. store_id is where the "
        "visitor went and is the one they mean"
    ),
    ("order_lines", "demo_id"): "the same as orders.demo_id",
    ("order_lines", "unit_price"): (
        "line_total already carries what the line came to including its "
        "modifiers, and a per-unit price invites an answer that multiplies it "
        "by a quantity and quietly loses the modifiers"
    ),
    ("order_lines", "modifiers"): (
        "an array of modifier identifiers. Naming the build of a line is the "
        "action lane's job through propose_order, and an array in a semantic "
        "model is an invitation to FLATTEN"
    ),
    ("points", "demo_id"): "the same as orders.demo_id",
    ("points", "order_id"): (
        "the ledger's link back to the order that earned the points. Left out "
        "with the points-to-orders relationship and for the same reason: the "
        "column is null on a signup bonus and on an expiry, so a join through "
        "it drops exactly the movements a balance must include"
    ),
    ("items", "description"): (
        "published marketing copy. The knowledge lane answers from it, with a "
        "citation; an account question never needs it"
    ),
    ("items", "calories"): (
        "the sharpest omission here. Exposed, 'how many calories have I eaten "
        "this year' becomes a sum over a column describing a DEFAULT build of "
        "an item nobody ordered by default -- plausible, arithmetically sound "
        "and false. The golden set names that question as one the account lane "
        "must refuse (a4-unanswerable-aggregate), and this absence is the "
        "refusal"
    ),
    ("items", "allergens"): (
        "PRD K3 requires an allergen answer to be unconditional and cited, "
        "which is the knowledge lane against the published chart. An allergen "
        "claim assembled by a text-to-SQL system is the worst answer this "
        "system could produce"
    ),
    ("items", "allergen_disclosure"): (
        "meaningless without the allergens array it qualifies, and dangerous "
        "with it. docs/decisions/allergen-absence.md"
    ),
    ("items", "source_url"): "half of a citation, and the account lane cites nothing",
    ("items", "harvested_at"): "the other half",
    ("restaurants", "hours"): (
        "seven objects per restaurant, published opening hours. When a "
        "restaurant is open is a menu-and-locator question; when the visitor "
        "last went is this one"
    ),
}
"""Every column of an exposed table that the view does not expose, and why.

The other half of the boundary, and the more easily lost one: a table can be in
the model for one column and drag eight more in behind it. `menu_items` is here
for its name and its category and for nothing else, and the six columns below
it are the ones that would turn an account question into a nutrition claim.
"""


@dataclass(frozen=True, slots=True)
class Element:
    """One fact, dimension or metric.

    Attributes:
        kind: Which clause of the DDL declares it.
        table: The :attr:`LogicalTable.alias` it hangs off.
        name: The business name, which is what a question is answered in.
        expression: The SQL after ``AS``, exactly as the DDL writes it.
        synonyms: What a visitor says instead. Required on every element: a
            field with no synonym is one the model only finds when somebody
            uses its name, and nobody says ``delta``.
        using: The relationships a metric reaches through, for the two that
            need the parent order's settled flag. Empty everywhere else.
    """

    kind: ElementKind
    table: str
    name: str
    expression: str
    synonyms: tuple[str, ...]
    using: tuple[str, ...] = field(default_factory=tuple)

    def qualified(self) -> str:
        """Return ``orders.total_spend`` and the like."""
        return f"{self.table}.{self.name}"


@dataclass(frozen=True, slots=True)
class Relationship:
    """One declared join.

    Attributes:
        name: The relationship name. A metric's ``USING`` clause names it.
        table: The alias holding the foreign key.
        columns: The foreign key columns.
        references: The alias joined to, on its primary key.
    """

    name: str
    table: str
    columns: tuple[str, ...]
    references: str


RELATIONSHIPS: Final[tuple[Relationship, ...]] = (
    Relationship("lines_to_order", "order_lines", ("order_id",), "orders"),
    Relationship("lines_to_item", "order_lines", ("item_id",), "items"),
    Relationship("orders_to_restaurant", "orders", ("store_id",), "restaurants"),
)
"""Three joins, and the fourth one that is deliberately absent.

``points`` joins to nothing. ``loyalty_ledger.order_id`` is null on a signup
bonus and on an expiry, so a points-to-orders relationship would let a
perfectly reasonable question about points over a period drop the opening
balance and answer with a number that is wrong in the visitor's favour. Nothing
in scope needs the join, so it is not declared.
"""

_FACTS: Final[tuple[Element, ...]] = (
    Element(
        "FACT",
        "orders",
        "order_total",
        "orders.total",
        ("order total", "what that order came to", "basket total"),
    ),
    Element(
        "FACT",
        "orders",
        "settled_spend",
        f"IFF(orders.status = '{SETTLED_STATUS}', orders.total, 0)",
        ("spend on an order", "money actually spent"),
    ),
    Element(
        "FACT",
        "orders",
        "settled_order",
        f"IFF(orders.status = '{SETTLED_STATUS}', 1, 0)",
        ("a completed order", "an order that went through"),
    ),
    Element(
        "FACT",
        "orders",
        "settled_at",
        f"IFF(orders.status = '{SETTLED_STATUS}', orders.placed_at, NULL)",
        ("when the order actually happened",),
    ),
    Element(
        "FACT",
        "order_lines",
        "quantity",
        "order_lines.qty",
        ("how many", "quantity", "number of them"),
    ),
    Element(
        "FACT",
        "order_lines",
        "line_spend",
        "order_lines.line_total",
        ("line total", "what that item came to"),
    ),
    Element(
        "FACT",
        "points",
        "point_change",
        "points.delta",
        ("points moved", "points earned or spent", "change in points"),
    ),
)

_DIMENSIONS: Final[tuple[Element, ...]] = (
    Element(
        "DIMENSION",
        "orders",
        "order_id",
        "orders.order_id",
        ("order number", "order id", "receipt number"),
    ),
    Element(
        "DIMENSION",
        "orders",
        "ordered_at",
        "orders.placed_at",
        (
            "when i ordered",
            "order time",
            "date of the order",
            "when i came in",
            "when i went",
        ),
    ),
    Element(
        "DIMENSION",
        "orders",
        "ordered_on",
        "CAST(orders.placed_at AS DATE)",
        ("day i ordered", "order date", "what day"),
    ),
    Element(
        "DIMENSION",
        "orders",
        "order_month",
        "DATE_TRUNC('MONTH', orders.placed_at)",
        ("month", "by month", "each month", "monthly"),
    ),
    Element(
        "DIMENSION",
        "orders",
        "order_year",
        "YEAR(orders.placed_at)",
        ("year", "this year", "last year", "by year"),
    ),
    Element(
        "DIMENSION",
        "orders",
        "order_status",
        "orders.status",
        (
            "status",
            "outcome",
            "cancelled",
            "refunded",
            "completed",
            "did it go through",
        ),
    ),
    Element(
        "DIMENSION",
        "orders",
        "order_channel",
        "orders.channel",
        ("channel", "delivery", "in store", "pickup", "how i ordered"),
    ),
    Element(
        "DIMENSION",
        "order_lines",
        "line_number",
        "order_lines.line_number",
        ("line", "line number"),
    ),
    Element(
        "DIMENSION",
        "items",
        "item_name",
        "items.name",
        (
            "item",
            "item name",
            "dish",
            "what it is called",
            "the food",
            "burrito",
            "bowl",
        ),
    ),
    Element(
        "DIMENSION",
        "items",
        "item_category",
        "items.category",
        ("category", "kind of item", "entree", "side", "drink", "type of food"),
    ),
    Element(
        "DIMENSION",
        "restaurants",
        "store_name",
        "restaurants.name",
        ("store", "store name", "restaurant", "location", "which store", "the branch"),
    ),
    Element(
        "DIMENSION",
        "restaurants",
        "store_city",
        "restaurants.city",
        ("city", "town", "where", "neighbourhood", "area"),
    ),
    Element(
        "DIMENSION",
        "restaurants",
        "store_region",
        "restaurants.region",
        ("state", "region", "which state"),
    ),
    Element(
        "DIMENSION",
        "points",
        "movement_reason",
        "points.reason",
        ("reason", "why the points moved", "earned", "redeemed", "bonus"),
    ),
    Element(
        "DIMENSION",
        "points",
        "reward_name",
        "points.reward_name",
        ("reward", "what i redeemed", "free item", "which reward"),
    ),
    Element(
        "DIMENSION",
        "points",
        "moved_at",
        "points.created_at",
        ("when the points moved", "when i earned them", "when i redeemed"),
    ),
)

_METRICS: Final[tuple[Element, ...]] = (
    Element(
        "METRIC",
        "orders",
        "total_spend",
        "SUM(orders.settled_spend)",
        (
            "spend",
            "total spend",
            "how much have i spent",
            "money spent",
            "what have i spent",
            "how much did i spend",
        ),
    ),
    Element(
        "METRIC",
        "orders",
        "order_count",
        "SUM(orders.settled_order)",
        (
            "orders",
            "how many orders",
            "number of orders",
            "how many times",
            "visits",
            "how often",
        ),
    ),
    Element(
        "METRIC",
        "orders",
        "average_order_value",
        "SUM(orders.settled_spend) / NULLIF(SUM(orders.settled_order), 0)",
        (
            "average order",
            "average spend",
            "typical order",
            "average basket",
            "how much do i usually spend",
        ),
    ),
    Element(
        "METRIC",
        "orders",
        "first_order_at",
        "MIN(orders.settled_at)",
        (
            "first order",
            "when did i start",
            "my first visit",
            "how long have i been coming",
        ),
    ),
    Element(
        "METRIC",
        "orders",
        "last_order_at",
        "MAX(orders.settled_at)",
        (
            "last order",
            "when did i last order",
            "most recent order",
            "when was i last there",
            "last visit",
            "when did i last go",
        ),
    ),
    Element(
        "METRIC",
        "order_lines",
        "items_ordered",
        "SUM(order_lines.quantity * orders.settled_order)",
        (
            "how many items",
            "how many of them",
            "times i ordered it",
            "how many burritos",
            "how often do i order",
        ),
        using=("lines_to_order",),
    ),
    Element(
        "METRIC",
        "order_lines",
        "item_spend",
        "SUM(order_lines.line_spend * orders.settled_order)",
        ("spent on an item", "how much have i spent on", "money on that item"),
        using=("lines_to_order",),
    ),
    Element(
        "METRIC",
        "points",
        "points_balance",
        "SUM(points.point_change)",
        (
            "points",
            "points balance",
            "how many points do i have",
            "my balance",
            "rewards balance",
            "how much have i got",
        ),
    ),
    Element(
        "METRIC",
        "points",
        "points_earned",
        "SUM(IFF(points.delta > 0, points.delta, 0))",
        ("points earned", "how many points have i earned", "points i got"),
    ),
    Element(
        "METRIC",
        "points",
        "points_redeemed",
        "SUM(IFF(points.delta < 0, -points.delta, 0))",
        ("points redeemed", "points spent", "how many points have i used"),
    ),
)

ELEMENTS: Final[tuple[Element, ...]] = _FACTS + _DIMENSIONS + _METRICS
"""Every fact, dimension and metric, in the order the DDL declares them.

The two ``order_lines`` metrics are the only ones with a ``USING``: a line
carries no status of its own, so the settled rule has to be reached through
``lines_to_order``. A metric that referenced ``orders.status`` directly is
rejected by Snowflake -- an expression may name another logical table's
declared ELEMENTS through a relationship, not its physical columns, and the
error it gives is ``invalid identifier 'ORDERS.STATUS'``.
"""


@dataclass(frozen=True, slots=True)
class VerifiedQuery:
    """One question the frequent path does not re-derive.

    Attributes:
        name: The verified query name, which the response reports back in
            ``confidence.verified_query_used``.
        question: The question as a visitor asks it.
        sql: The SQL, against the LOGICAL model -- ``__orders``, and the
            element names above. See this module's docstring for what happens
            to a verified query written against the physical tables.
        onboarding: Whether Snowsight offers it as a suggested first question.
        covers: The golden-set case ids it answers, from `eval/golden/cases.json`.
    """

    name: str
    question: str
    sql: str
    onboarding: bool = False
    covers: tuple[str, ...] = field(default_factory=tuple)


VERIFIED_QUERIES: Final[tuple[VerifiedQuery, ...]] = (
    VerifiedQuery(
        "points_balance",
        "how many points do i have",
        "SELECT SUM(l.point_change) AS points_balance FROM __points AS l",
        onboarding=True,
    ),
    VerifiedQuery(
        "spend_this_year",
        "what have i spent here this year",
        "SELECT SUM(o.settled_spend), SUM(o.settled_order) FROM __orders AS o "
        "WHERE o.order_year = YEAR(CURRENT_DATE())",
        onboarding=True,
        covers=("a2-spend-this-year",),
    ),
    VerifiedQuery(
        "spend_by_month",
        "what have i spent each month",
        "SELECT o.order_month, SUM(o.settled_spend), SUM(o.settled_order) "
        "FROM __orders AS o GROUP BY o.order_month",
    ),
    VerifiedQuery(
        "last_order",
        "what did i order last time",
        "the most recent settled order, joined out to its lines, its items and "
        "its restaurant",
        onboarding=True,
        covers=("a1-last-order",),
    ),
    VerifiedQuery(
        "most_ordered_item",
        "what do i order most",
        "items ordered and spent per item name, settled orders only",
    ),
    VerifiedQuery(
        "most_visited_store",
        "which store do i go to most",
        "visits and spend per store, settled orders only",
        covers=("a2-most-visited-store",),
    ),
    VerifiedQuery(
        "last_visit_by_store",
        "when did i last go to each store",
        "the most recent settled order per store",
        covers=("a2-store-last-visit",),
    ),
)
"""The seven questions the frequent path is not re-derived for.

``sql`` here is a description rather than the statement for the four whose
statement is long: the DDL is the source of the SQL and `test_semantic_view.py`
compares the names, questions, onboarding flags and coverage rather than
re-typing five joins in two places. What the tests do assert about the SQL is
the thing worth asserting -- that none of it names ``demo_id``, and that none of
it names a physical table.

``covers`` ties each to the account-lane cases in `eval/golden/cases.json`. The
tests fail if a case that routes to ``ask_account_question`` and is not a
refusal case has no verified query behind it, which is #45's second acceptance
criterion asked of the checked-in set rather than of a deployment.

``points_balance`` covers nothing, and that is the interesting entry. #45 names
the points balance as in scope and it is the most asked question there is, so
the verified query is here -- but the golden set routes "how many points do i
have" to ``get_points_balance`` and names ``ask_account_question`` among that
case's forbidden tools. A fixed lookup does not need a generated query. This is
the fallback for the phrasings that reach the account lane anyway, which is why
it is verified and why it claims no case.
"""


@dataclass(frozen=True, slots=True)
class Unanswerable:
    """A question the account lane must decline, and what it would need.

    Attributes:
        question: As a visitor would ask it.
        withheld: The things that would have to be in the model to answer it,
            each named as ``table`` or ``logical_table.column`` and each
            required by the tests to appear in :data:`WITHHELD_TABLES` or
            :data:`WITHHELD_COLUMNS`. Empty where the refusal rests on the
            model's instructions instead -- see :attr:`instructed`.
        needs: The same fact in prose, for whoever reads the refusal set rather
            than runs it.
        instructed: True where nothing is withheld and the refusal comes from
            ``AI_QUESTION_CATEGORIZATION``. Two questions are like this and
            both are about the population: the tables ARE in the model, and
            what makes the question unanswerable is that a row access policy
            would answer it with this visitor's own rows under a plural noun.
            A weaker guarantee than an absent column, and marked as one.
        golden_case: The `eval/golden/cases.json` id, where there is one.
    """

    question: str
    withheld: tuple[str, ...]
    needs: str
    instructed: bool = False
    golden_case: str = ""


UNANSWERABLE: Final[tuple[Unanswerable, ...]] = (
    Unanswerable(
        "how many calories have i eaten here this year",
        withheld=("items.calories",),
        needs=(
            "menu_items.calories, which is withheld -- and which describes a "
            "default build of an item nobody ordered by default, so the number "
            "would be wrong as well as unavailable"
        ),
        golden_case="a4-unanswerable-aggregate",
    ),
    Unanswerable(
        "what does a steak burrito cost at my store",
        withheld=("item_prices",),
        needs=(
            "CATALOGUE.item_prices, which is withheld. The knowledge lane "
            "quotes a price with the restaurant and the harvest date attached"
        ),
    ),
    Unanswerable(
        "is there dairy in what i usually order",
        withheld=("items.allergens", "items.allergen_disclosure", "usual_order"),
        needs=(
            "menu_items.allergens and MARTS.usual_order, both withheld. PRD K3 "
            "makes an allergen answer unconditional and cited, which is the "
            "knowledge lane"
        ),
    ),
    Unanswerable(
        "what should i order next time",
        withheld=("item_affinity", "usual_order"),
        needs=(
            "MARTS.item_affinity and MARTS.usual_order, both withheld. That is "
            "get_recommendations, and it says what it computed from"
        ),
    ),
    Unanswerable(
        "what do most people order here",
        withheld=(),
        needs=(
            "a table about the population. Every table here is either this "
            "visitor's own rows or the published menu, and the policy makes "
            "the population invisible rather than absent"
        ),
        instructed=True,
    ),
    Unanswerable(
        "how much does the average customer spend",
        withheld=("customer_360", "spend_summary"),
        needs=(
            "MARTS.customer_360 and MARTS.spend_summary are withheld, and even "
            "orders would answer this with one visitor's rows called everybody"
        ),
        instructed=True,
    ),
    Unanswerable(
        "what is my name",
        withheld=("demo_visitors",),
        needs="ACCOUNTS.demo_visitors, which is withheld",
    ),
    Unanswerable(
        "what preferences have i told you about",
        withheld=("demo_visitors",),
        needs=(
            "demo_visitors.stated_preferences, which is withheld. It is also "
            "one of the three fields a visitor may edit, and reading it back "
            "through a generated query is not how the app knows what it was "
            "told"
        ),
    ),
    Unanswerable(
        "how much will i spend next month",
        withheld=(),
        needs=(
            "a forecast. Nothing here predicts, and a number produced by "
            "extrapolating eighteen months of orders is the plausible guess "
            "PRD A4 exists to forbid"
        ),
        instructed=True,
    ),
    Unanswerable(
        "what time does my store open on sunday",
        withheld=("restaurants.hours",),
        needs=(
            "stores.hours, which is withheld. When a restaurant is open is a "
            "locator question; when the visitor last went is this one"
        ),
    ),
)
"""The deliberately-unanswerable set, which is #45's third acceptance criterion.

Ten questions, each naming what the view does not carry. Two shapes, and the
difference between them is the difference between a guarantee and a good
intention.

Seven are refused because a table or a column is not in the model. Off the
verified path Cortex Analyst emits ``SELECT * FROM SEMANTIC_VIEW(...)``, which
can only name declared elements, so those questions have nothing to be answered
from and the refusal is structural.

Three carry ``instructed=True``. Those are the population and forecast
questions, and the tables they would reach for are either withheld or -- for
"what do most people order here" -- present but invisible, because a row access
policy filters them to one visitor. The failure mode there is not a wrong join;
it is a right join over one person's rows reported under a plural noun. Nothing
in the schema prevents it, so ``AI_QUESTION_CATEGORIZATION`` says so instead,
and the eval is what checks that the saying works.
"""


def qualified() -> str:
    """Return ``CHIP_CHAT.ACCOUNTS.ACCOUNT_LANE``."""
    return f"CHIP_CHAT.{VIEW_SCHEMA}.{VIEW_NAME}"


def logical_table(alias: str) -> LogicalTable:
    """Return one logical table by its alias.

    Args:
        alias: The name the view calls it, e.g. ``order_lines``.

    Returns:
        The declaration.

    Raises:
        KeyError: If the view has no such logical table.
    """
    for candidate in LOGICAL_TABLES:
        if candidate.alias == alias:
            return candidate
    raise KeyError(f"{VIEW_NAME} has no logical table {alias!r}")


def elements_of(alias: str, kind: ElementKind | None = None) -> Iterator[Element]:
    """Yield one logical table's elements, in declaration order.

    Args:
        alias: The logical table.
        kind: Restrict to facts, dimensions or metrics. All three if omitted.
    """
    for element in ELEMENTS:
        if element.table == alias and (kind is None or element.kind == kind):
            yield element


def exposed(alias: str) -> tuple[str, ...]:
    """Return the physical columns one logical table exposes, in DDL order.

    Every column of the physical table that is not in :data:`WITHHELD_COLUMNS`.
    The two are asserted to partition the table's columns, so this is the
    complement of a list somebody had to argue for rather than a list somebody
    remembered to update.
    """
    table = logical_table(alias)
    declared = next(
        candidate
        for candidate in TABLES
        if candidate.name == table.table and candidate.schema == table.schema
    )
    return tuple(
        column.name
        for column in declared.columns
        if (alias, column.name) not in WITHHELD_COLUMNS
    )
