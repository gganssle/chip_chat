"""The visitor-scoped reads, and the predicate that must never appear in them.

``chip_chat.snowflake.reads`` is three queries and the types they return. The
test that matters most is the first one: RFC-001 §05's guarantee is held by
#43's row access policies, and a query here that helpfully filtered on
``demo_id`` would need a visitor identifier to put in it -- which is the thing
the whole tool surface is arranged not to have. So the assertion is about a
string that is *absent* from every statement this module can run.

Everything else is about not guessing. A mart with no ``derived_at`` is
reported stale rather than fresh, a confidence outside ``[0, 1]`` is reported as
no confidence rather than clamped into one, and a row shaped in a way the module
did not expect raises rather than being rounded into an answer.
"""

from datetime import UTC, datetime, timedelta

import pytest

from chip_chat.snowflake import reads, schema
from chip_chat.snowflake.testing import FakeConnection

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

_STATEMENTS = (
    reads._POINTS_BALANCE_SQL,
    reads._AFFORDABLE_REWARDS_SQL,
    reads._USUAL_ORDER_SQL,
    reads._RECOMMENDATIONS_SQL,
)
"""Every statement this module can run. Private on purpose -- they are not an
interface -- and reached here because what is asserted about them is a property
of the SQL text rather than of the results."""


# ---------------------------------------------------------------------------
# The absent predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("statement", _STATEMENTS)
def test_no_read_filters_on_a_visitor(statement: str) -> None:
    """RFC-001 §05, as a property of the SQL rather than of a reviewer's memory.

    ``sql/11_semantic_view.sql`` puts it plainly: every query is written as
    though the visitor were the only person in the database, because the session
    cannot see another visitor's rows. A ``WHERE demo_id = ...`` here would be a
    second opinion about identity sitting underneath the one that is enforced.
    """
    assert schema.DEMO_ID not in statement.casefold()


@pytest.mark.parametrize("statement", _STATEMENTS)
def test_every_read_is_a_single_select(statement: str) -> None:
    """No second statement after a semicolon, and nothing that writes."""
    assert statement.lstrip().upper().startswith("SELECT")
    assert ";" not in statement


def test_the_reads_name_the_tables_the_schema_declares() -> None:
    """Spelled from :mod:`chip_chat.snowflake.schema`, never retyped.

    A read against ``CHIP_CHAT.MARTS.usual_orders`` would fail on the live
    account and pass every offline test, so the names come from the declaration
    the DDL is generated from.
    """
    assert schema.table("loyalty_ledger").qualified() in reads._POINTS_BALANCE_SQL
    assert schema.table("rewards").qualified() in reads._AFFORDABLE_REWARDS_SQL
    assert schema.table("usual_order").qualified() in reads._USUAL_ORDER_SQL
    assert schema.table("menu_items").qualified() in reads._USUAL_ORDER_SQL


def test_the_recommendations_mart_is_the_one_table_the_schema_does_not_declare() -> None:
    """And the reason is a decision this ticket deliberately does not take.

    RFC-001 §04 prints ``recommendations`` in the data model and also fixes four
    serving marts. ``cc-afo5`` is where the fifth is decided; until then the
    name is spelled once, beside its argument, and the read declines.
    """
    declared = {f"CHIP_CHAT.{table.schema}.{table.name}" for table in schema.TABLES}

    assert reads.RECOMMENDATIONS_MART not in declared
    assert reads.RECOMMENDATIONS_MART in reads._RECOMMENDATIONS_SQL


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


def _balance_connection(
    balance: int = 1_340, movements: int = 42, rewards: list[list[object]] | None = None
) -> FakeConnection:
    catalogue: list[list[object]] = (
        [
            ["side-tortilla", "SIDE TORTILLA", 250, "https://example/rewards", None],
            [
                "entree-and-chips",
                "ENTREE AND CHIPS",
                1_600,
                "https://example/rewards",
                None,
            ],
        ]
        if rewards is None
        else rewards
    )
    return FakeConnection(
        {
            reads._POINTS_BALANCE_SQL: [[balance, movements, NOW - timedelta(days=2)]],
            reads._AFFORDABLE_REWARDS_SQL: catalogue,
        }
    )


def test_the_balance_is_the_sum_of_the_ledger() -> None:
    result = reads.points_balance(_balance_connection())

    assert result.points_balance == 1_340
    assert result.movements == 42
    assert result.last_movement_at.startswith("2026-08-25")


def test_a_reward_the_balance_misses_says_how_far_off_it_is() -> None:
    """A visitor two hundred points short is owed the number, not silence.

    Which is why the catalogue read has no ``WHERE point_cost <= balance`` in
    it: the arithmetic is done here so the shortfall survives.
    """
    result = reads.points_balance(_balance_connection(balance=1_340))
    by_id = {reward.reward_id: reward for reward in result.rewards}

    assert by_id["side-tortilla"].affordable
    assert by_id["side-tortilla"].points_short == 0
    assert not by_id["entree-and-chips"].affordable
    assert by_id["entree-and-chips"].points_short == 260
    assert [reward.reward_id for reward in result.affordable] == ["side-tortilla"]


def test_an_unloaded_reward_catalogue_is_reported_and_not_rendered_as_empty() -> None:
    """``cc-99cn``: nothing publishes ``rewards`` yet.

    An empty line-up and a missing catalogue look identical in a list and are
    completely different things to say to a visitor.
    """
    result = reads.points_balance(_balance_connection(rewards=[]))

    assert result.points_balance == 1_340
    assert not result.catalogue_loaded


def test_a_balance_query_that_returned_nothing_is_an_error_not_a_zero() -> None:
    """``SUM`` over no rows still returns one row, so no rows is a failure."""
    connection = FakeConnection(
        {reads._POINTS_BALANCE_SQL: [], reads._AFFORDABLE_REWARDS_SQL: []}
    )

    with pytest.raises(reads.ReadError, match="no row at all"):
        reads.points_balance(connection)


def test_a_statement_the_warehouse_refused_becomes_a_read_error() -> None:
    """The lane's whole failure behaviour depends on one exception type."""
    connection = FakeConnection(
        raises={reads._POINTS_BALANCE_SQL: RuntimeError("warehouse suspended")}
    )

    with pytest.raises(reads.ReadError, match="warehouse suspended"):
        reads.points_balance(connection)


# ---------------------------------------------------------------------------
# The habit mart
# ---------------------------------------------------------------------------


def _usual(
    confidence: object = 0.82, derived_at: object = NOW - timedelta(hours=5)
) -> FakeConnection:
    return FakeConnection(
        {
            reads._USUAL_ORDER_SQL: [
                ["BOWL-CHICKEN", ["MOD-GUAC"], confidence, derived_at, "Chicken Bowl"],
                ["SIDE-GUACAMOLE", None, confidence, derived_at, "Side of Guacamole"],
            ]
        }
    )


def test_the_habit_carries_the_marts_own_confidence() -> None:
    result = reads.usual_order(_usual(), now=NOW)

    assert result.has_a_usual
    assert result.confidence == pytest.approx(0.82)
    assert [line.item_id for line in result.lines] == ["BOWL-CHICKEN", "SIDE-GUACAMOLE"]
    assert result.lines[0].modifiers == ("MOD-GUAC",)


def test_no_row_is_a_visitor_with_no_usual_and_not_an_outage() -> None:
    """The Explorer persona genuinely has no usual order (PRD)."""
    result = reads.usual_order(FakeConnection({reads._USUAL_ORDER_SQL: []}), now=NOW)

    assert not result.has_a_usual
    assert result.confidence is None


def test_a_confidence_outside_the_range_is_reported_as_none() -> None:
    """A mart that computed 1.4 computed something other than a confidence.

    Reporting it as *no number* is truthful; clamping it to ``1.0`` would report
    a guess as a certainty, which is the one direction this must never be wrong
    in.
    """
    assert reads.usual_order(_usual(confidence=1.4), now=NOW).confidence is None
    assert reads.usual_order(_usual(confidence=None), now=NOW).confidence is None


def test_an_item_that_has_left_the_menu_still_comes_back() -> None:
    """The join is a ``LEFT`` join, and this is the reason.

    *You usually order this and it is not on the menu any more* is the honest
    answer; an inner join would answer *you have no usual*.
    """
    connection = FakeConnection(
        {
            reads._USUAL_ORDER_SQL: [
                ["BOWL-RETIRED", [], 0.9, NOW - timedelta(hours=2), None]
            ]
        }
    )

    line = reads.usual_order(connection, now=NOW).lines[0]

    assert line.item_id == "BOWL-RETIRED"
    assert not line.on_the_menu


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_a_fresh_mart_is_not_stale_and_says_when_it_was_computed() -> None:
    """RFC-001 §10 asks for the timestamp on every path, not only the stale one."""
    result = reads.usual_order(_usual(), now=NOW, threshold=36.0)

    assert not result.mart.stale
    assert result.mart.age_hours == pytest.approx(5.0)
    assert result.mart.derived_at.startswith("2026-08-27T07:00")


def test_a_mart_past_the_threshold_is_stale() -> None:
    old = _usual(derived_at=NOW - timedelta(hours=40))

    assert reads.usual_order(old, now=NOW, threshold=36.0).mart.stale


def test_a_mart_row_that_cannot_date_itself_is_stale_rather_than_fresh() -> None:
    """Undated is worse than old: it cannot be served as fresh either."""
    undated = reads.usual_order(_usual(derived_at=None), now=NOW).mart

    assert undated.derived_at == ""
    assert undated.stale
    assert undated.age_hours is None


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """``TIMESTAMP_NTZ`` arrives naive and is UTC by the schema's convention."""
    naive = _usual(derived_at=datetime(2026, 8, 27, 7, 0))

    assert reads.usual_order(naive, now=NOW).mart.age_hours == pytest.approx(5.0)


def test_the_threshold_is_configurable_and_fails_loudly_when_it_is_wrong() -> None:
    assert reads.stale_after_hours({}) == reads.DEFAULT_STALE_AFTER_HOURS
    assert reads.stale_after_hours({reads.STALE_AFTER_HOURS_VARIABLE: "12"}) == 12.0

    with pytest.raises(ValueError, match="not a number"):
        reads.stale_after_hours({reads.STALE_AFTER_HOURS_VARIABLE: "nightly"})
    with pytest.raises(ValueError, match="every mart as stale"):
        reads.stale_after_hours({reads.STALE_AFTER_HOURS_VARIABLE: "0"})


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def _recommendations(count: int = 4) -> FakeConnection:
    return FakeConnection(
        {
            reads._RECOMMENDATIONS_SQL: [
                [
                    rank,
                    f"ITEM-{rank}",
                    f"because you order the chicken bowl most weeks ({rank})",
                    0.5 / rank,
                    "3",
                    NOW - timedelta(hours=6),
                    f"Item {rank}",
                ]
                for rank in range(1, count + 1)
            ]
        }
    )


def test_the_rationale_comes_from_the_mart_and_is_not_composed_here() -> None:
    """#37 renders the sentence at scoring time from the visitor's own share.

    A sentence written at serving time would be a second, unversioned opinion
    about what the model found -- and it would carry no ``model_version``.
    """
    found = reads.recommendations(_recommendations(), now=NOW)

    assert found.items[0].rationale.startswith("because you order")
    assert found.items[0].model_version == "3"


def test_only_the_strongest_few_are_returned_and_the_rank_survives() -> None:
    found = reads.recommendations(_recommendations(count=8), now=NOW)

    assert len(found.items) == reads.MAX_RECOMMENDATIONS
    assert [item.rank for item in found.items] == [1, 2, 3]


def test_no_rows_is_a_visitor_with_nothing_to_suggest_and_not_an_outage() -> None:
    empty = FakeConnection({reads._RECOMMENDATIONS_SQL: []})

    assert reads.recommendations(empty, now=NOW).items == ()


def test_a_missing_serving_table_is_a_read_error() -> None:
    """Today's state, and ``cc-afo5``'s to change.

    The lane turns this into a decline; what matters here is that it is an
    exception of the one type the lane catches and not a silent empty answer.
    """
    absent = FakeConnection(
        raises={
            reads._RECOMMENDATIONS_SQL: RuntimeError(
                "SQL compilation error: Object 'CHIP_CHAT.MARTS.RECOMMENDATIONS' "
                "does not exist"
            )
        }
    )

    with pytest.raises(reads.ReadError, match="does not exist"):
        reads.recommendations(absent, now=NOW)
