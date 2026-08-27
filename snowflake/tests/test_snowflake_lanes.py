"""The two Snowflake lanes, and the four ways they decline.

RFC-001 §10 gives each lane a blast radius of one row, and PRD A4 gives the
account lane one specific behaviour: *an explicit "I cannot answer that
reliably" path, never a plausible number, and never a hand-written fallback
query.* Both are behaviour under failure, which is what a live service is worst
at producing on demand -- so every test here runs over
:mod:`chip_chat.snowflake.testing` and none of them costs a credit.

The span assertions are here rather than in ``agent/`` because ``db.cortex_analyst``
is the account lane's own child and the lane is what opens it. The tool span
above it belongs to :mod:`chip_chat.agent.tools`, which is where its tests are.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from chip_chat.otel import ToolName, agent_step, chat_turn, tool_call
from chip_chat.otel.attributes import DbAttributes
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.snowflake import reads
from chip_chat.snowflake.analyst import REFUSAL, Thresholds
from chip_chat.snowflake.cortex import AnalystError
from chip_chat.snowflake.lane import (
    ACCOUNT_UNAVAILABLE,
    AccountAnswer,
    AccountLane,
    PersonalizationLane,
)
from chip_chat.snowflake.testing import (
    FakeConnection,
    StubAnalyst,
    analyst_response,
    checkout_of,
    failing_checkout,
    sql_part,
    suggestions_part,
)

SESSION = "sess-account"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

VERIFIED_SQL = (
    "SELECT SUM(l.point_change) AS points_balance "
    "FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger AS l"
)
GENERATED_SQL = (
    "SELECT * FROM SEMANTIC_VIEW(CHIP_CHAT.ACCOUNTS.ACCOUNT_LANE METRICS total_spend)"
)


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Record spans inside the two parents ``db.cortex_analyst`` requires."""
    with (
        span_recorder("snowflake") as recorder,
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
    ):
        yield recorder


def _asking(
    lane: AccountLane, question: str = "what did I spend this year"
) -> AccountAnswer:
    """Ask, inside the tool span the schema requires above ``db.cortex_analyst``."""
    with tool_call(ToolName.ASK_ACCOUNT_QUESTION, arguments={"question": question}):
        return lane.ask(question, session_id=SESSION)


# ---------------------------------------------------------------------------
# The account lane answers
# ---------------------------------------------------------------------------


def test_an_answered_question_runs_the_generated_sql_on_the_bound_connection(
    spans: SpanRecorder,
) -> None:
    """The whole design in one assertion.

    Cortex Analyst wrote a statement with no visitor in it; the answer is about
    the right visitor because the *connection* was bound before the lane saw it.
    """
    connection = FakeConnection({VERIFIED_SQL: [[1_340]]})
    lane = AccountLane(
        checkout_of(connection),
        StubAnalyst(analyst_response(sql_part(VERIFIED_SQL, verified="points"))),
    )

    answer = _asking(lane)

    assert answer.answered
    assert answer.rows == ({"points_balance": 1_340},)
    assert answer.verified_query == "points"
    assert connection.statements == [VERIFIED_SQL]
    assert "demo_id" not in " ".join(connection.statements).casefold()


def test_the_span_carries_the_sql_and_the_row_count(spans: SpanRecorder) -> None:
    """``db.cortex_analyst``'s two attributes come from opposite ends of the call.

    Which is why the lane opens the span around all three steps rather than
    around the HTTP request alone.
    """
    connection = FakeConnection({GENERATED_SQL: [[12], [13]]})
    lane = AccountLane(
        checkout_of(connection), StubAnalyst(analyst_response(sql_part(GENERATED_SQL)))
    )

    _asking(lane)
    attributes = spans.attributes_of("db.cortex_analyst")

    assert attributes[DbAttributes.DB_QUERY_TEXT] == GENERATED_SQL
    assert attributes[DbAttributes.DB_RESPONSE_RETURNED_ROWS] == 2


def test_the_question_reaches_analyst_unchanged() -> None:
    """Not rewritten, not prefixed with an instruction, not joined to a history."""
    transport = StubAnalyst(analyst_response(sql_part(VERIFIED_SQL)))
    lane = AccountLane(checkout_of(FakeConnection({VERIFIED_SQL: [[1]]})), transport)

    with (
        span_recorder("snowflake"),
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
    ):
        _asking(lane, "how many points do i have")

    assert transport.questions == ["how many points do i have"]


def test_named_columns_are_read_off_the_statement_and_positional_ones_are_not(
    spans: SpanRecorder,
) -> None:
    """All-or-nothing, because a half-named row reads as a fully named one."""
    named = (
        "SELECT SUM(total) AS lifetime_spend, COUNT(*) AS orders "
        "FROM CHIP_CHAT.ACCOUNTS.orders"
    )
    lane = AccountLane(
        checkout_of(FakeConnection({named: [[412.5, 33]]})),
        StubAnalyst(analyst_response(sql_part(named))),
    )

    assert _asking(lane).rows == ({"lifetime_spend": 412.5, "orders": 33},)

    unnamed = "SELECT SUM(total), COUNT(*) FROM CHIP_CHAT.ACCOUNTS.orders"
    lane = AccountLane(
        checkout_of(FakeConnection({unnamed: [[412.5, 33]]})),
        StubAnalyst(analyst_response(sql_part(unnamed))),
    )

    assert _asking(lane).rows == ({"column_1": 412.5, "column_2": 33},)


def test_a_comma_inside_a_function_is_not_a_column_boundary(
    spans: SpanRecorder,
) -> None:
    sql = (
        "SELECT COALESCE(SUM(total), 0) AS spend, MAX(placed_at) AS last_seen "
        "FROM CHIP_CHAT.ACCOUNTS.orders"
    )
    lane = AccountLane(
        checkout_of(FakeConnection({sql: [[0, None]]})),
        StubAnalyst(analyst_response(sql_part(sql))),
    )

    assert set(_asking(lane).rows[0]) == {"spend", "last_seen"}


# ---------------------------------------------------------------------------
# The account lane declines -- PRD A4
# ---------------------------------------------------------------------------


def test_a_question_analyst_would_not_answer_gets_the_refusal_and_no_sql(
    spans: SpanRecorder,
) -> None:
    """The sentence RFC-001 §10 writes out, and nothing else.

    The connection is a fake that raises on every statement, so a lane that
    reached for a fallback query would fail this test rather than quietly
    answering.
    """
    lane = AccountLane(
        checkout_of(FakeConnection()),
        StubAnalyst(analyst_response(suggestions_part("what did I spend last month?"))),
    )

    answer = _asking(lane, "how many calories have I eaten this year")
    result = answer.as_tool_result()

    assert not answer.answered
    assert answer.sql == ""
    assert result["say"] == REFUSAL
    assert result["suggestions"] == ["what did I spend last month?"]
    assert spans.span_named("db.cortex_analyst").status.is_ok is False


def test_a_declined_lane_is_a_failed_span_and_not_a_quiet_success(
    spans: SpanRecorder,
) -> None:
    """A decline that ended a span cleanly is an outage nobody can see."""
    lane = AccountLane(checkout_of(FakeConnection()), StubAnalyst(None))

    _asking(lane)

    assert spans.span_named("db.cortex_analyst").status.is_ok is False


def test_sql_that_arrived_and_was_refused_is_still_never_run(
    spans: SpanRecorder,
) -> None:
    """The refusal RFC-001 §10 is actually about.

    A timeout is easy. A statement that arrived, parsed and would have run is
    the moment a fallback looks reasonable -- and here the floor is verified-only
    and the statement was merely generated.
    """
    connection = FakeConnection({GENERATED_SQL: [[1]]})
    lane = AccountLane(
        checkout_of(connection),
        StubAnalyst(analyst_response(sql_part(GENERATED_SQL))),
        thresholds=Thresholds(min_confidence=1.0),
    )

    answer = _asking(lane)

    assert not answer.answered
    assert connection.statements == []


def test_a_transport_that_could_not_mint_a_token_declines_rather_than_raising(
    spans: SpanRecorder,
) -> None:
    """The one failure the real transport lets out is the token source."""
    lane = AccountLane(
        checkout_of(FakeConnection()),
        StubAnalyst(error=AnalystError("snow connection generate-jwt failed")),
    )

    answer = _asking(lane)

    assert not answer.answered
    assert "generate-jwt" in (answer.declined or "")


def test_an_admitted_statement_that_would_not_run_declines_as_an_outage(
    spans: SpanRecorder,
) -> None:
    """Distinguishable from a refusal: the lane reached Snowflake and it said no.

    Same sentence to the visitor either way, two different findings for whoever
    reads the trace.
    """
    connection = FakeConnection(
        raises={VERIFIED_SQL: RuntimeError("warehouse suspended")}
    )
    lane = AccountLane(
        checkout_of(connection), StubAnalyst(analyst_response(sql_part(VERIFIED_SQL)))
    )

    result = _asking(lane).as_tool_result()

    assert result["say"] == ACCOUNT_UNAVAILABLE
    assert "warehouse suspended" in result["reason"]


def test_a_pool_that_will_not_check_out_declines(spans: SpanRecorder) -> None:
    """#44's pool refuses a session it cannot bind; the lane must survive it."""
    lane = AccountLane(
        failing_checkout(RuntimeError("no visitor is bound to that session")),
        StubAnalyst(analyst_response(sql_part(VERIFIED_SQL))),
    )

    answer = _asking(lane)

    assert not answer.answered
    assert "no visitor is bound" in (answer.declined or "")


def test_the_refusal_tells_the_model_not_to_answer_from_anywhere_else() -> None:
    """PRD A4's *never a plausible number*, aimed at the one reader who could."""
    lane = AccountLane(checkout_of(FakeConnection()), StubAnalyst(None))

    with (
        span_recorder("snowflake"),
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
    ):
        result = _asking(lane).as_tool_result()

    assert "estimate" in result["never"]
    assert "rows" not in result


# ---------------------------------------------------------------------------
# The points read
# ---------------------------------------------------------------------------


def _points_connection() -> FakeConnection:
    return FakeConnection(
        {
            reads._POINTS_BALANCE_SQL: [[1_340, 42, NOW]],
            reads._AFFORDABLE_REWARDS_SQL: [
                ["side-tortilla", "SIDE TORTILLA", 250, "https://example", None]
            ],
        }
    )


def test_the_points_read_opens_no_child_span() -> None:
    """RFC-001 §09 gives ``db.cortex_analyst`` to the *generated* query only.

    One fixed question does not need a model to write its SQL, and the tool span
    the agent already opened is the whole of this call in the trace.
    """
    lane = AccountLane(checkout_of(_points_connection()), StubAnalyst(None))

    with (
        span_recorder("snowflake") as spans,
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
        tool_call(ToolName.GET_POINTS_BALANCE, arguments={}),
    ):
        lane.points_balance(session_id=SESSION)

    assert "db.cortex_analyst" not in spans.names()


def test_the_points_read_returns_the_balance_and_what_it_affords() -> None:
    lane = AccountLane(checkout_of(_points_connection()), StubAnalyst(None))

    result = lane.points_balance(session_id=SESSION).as_tool_result()

    assert result["points_balance"] == 1_340
    assert result["affordable_now"] == ["SIDE TORTILLA"]


def test_the_points_read_declines_when_the_pool_will_not_check_out() -> None:
    lane = AccountLane(failing_checkout(RuntimeError("pool closed")), StubAnalyst(None))

    result = lane.points_balance(session_id=SESSION).as_tool_result()

    assert result["declined"] == "ACCOUNT_LANE_UNAVAILABLE"
    assert "pool closed" in result["reason"]


def test_an_unloaded_reward_catalogue_says_so_rather_than_listing_nothing() -> None:
    """``cc-99cn``. The balance is real; the empty line-up is not an answer."""
    connection = FakeConnection(
        {
            reads._POINTS_BALANCE_SQL: [[1_340, 42, NOW]],
            reads._AFFORDABLE_REWARDS_SQL: [],
        }
    )
    lane = AccountLane(checkout_of(connection), StubAnalyst(None))

    result = lane.points_balance(session_id=SESSION).as_tool_result()

    assert result["points_balance"] == 1_340
    assert "not loaded" in result["note"]


# ---------------------------------------------------------------------------
# Personalization
# ---------------------------------------------------------------------------


def _usual_connection(
    confidence: object = 0.82, derived_at: object = NOW - timedelta(hours=5)
) -> FakeConnection:
    return FakeConnection(
        {
            reads._USUAL_ORDER_SQL: [
                ["BOWL-CHICKEN", ["MOD-GUAC"], confidence, derived_at, "Chicken Bowl"]
            ]
        }
    )


def test_a_low_confidence_habit_is_offered_as_a_guess_in_words() -> None:
    """The surface promises the confidence is real and sometimes low.

    A bare ``0.21`` reaches a model as a number rather than as a hedge, so the
    hedge is a sentence beside it.
    """
    lane = PersonalizationLane(checkout_of(_usual_connection(confidence=0.21)))

    result = lane.usual_order(session_id=SESSION, now=NOW).as_tool_result()

    assert result["confidence"] == pytest.approx(0.21)
    assert "do not call it their usual" in result["confidence_note"]


def test_a_high_confidence_habit_may_be_called_a_usual() -> None:
    lane = PersonalizationLane(checkout_of(_usual_connection(confidence=0.91)))

    result = lane.usual_order(session_id=SESSION, now=NOW).as_tool_result()

    assert "Safe to call this their usual" in result["confidence_note"]


def test_a_visitor_with_no_usual_is_told_to_say_so() -> None:
    lane = PersonalizationLane(checkout_of(FakeConnection({reads._USUAL_ORDER_SQL: []})))

    result = lane.usual_order(session_id=SESSION, now=NOW).as_tool_result()

    assert result["has_a_usual"] is False
    assert "no usual order" in result["say"]


def test_a_stale_mart_is_served_with_its_timestamp(spans: SpanRecorder) -> None:
    """RFC-001 §10: with the ``derived_at``, never silently as fresh."""
    lane = PersonalizationLane(
        checkout_of(_usual_connection(derived_at=NOW - timedelta(hours=50)))
    )

    mart = lane.usual_order(session_id=SESSION, now=NOW).as_tool_result()["mart"]

    assert mart["stale"] is True
    assert mart["derived_at"].startswith("2026-08-25")


def test_the_recommendation_rationale_reaches_the_model() -> None:
    connection = FakeConnection(
        {
            reads._RECOMMENDATIONS_SQL: [
                [1, "ITEM-1", "you order the chicken bowl most weeks", 0.4, "3", NOW, "A"]
            ]
        }
    )
    lane = PersonalizationLane(checkout_of(connection))

    result = lane.recommendations(session_id=SESSION, now=NOW).as_tool_result()

    assert result["items"][0]["rationale"] == "you order the chicken bowl most weeks"
    assert "the reason is what makes this a suggestion" in result["how_to_use_it"]


def test_a_missing_recommendations_table_declines_rather_than_answering_empty() -> None:
    """``cc-afo5``'s state today, and the two must not look alike.

    *There is nothing to suggest for you* and *the mart does not exist* are
    different sentences, and only one of them is a lane outage.
    """
    connection = FakeConnection(
        raises={reads._RECOMMENDATIONS_SQL: RuntimeError("Object does not exist")}
    )
    lane = PersonalizationLane(checkout_of(connection))

    result = lane.recommendations(session_id=SESSION, now=NOW).as_tool_result()

    assert result["declined"] == "PERSONALIZATION_LANE_UNAVAILABLE"
    assert "does not exist" in result["reason"]


def test_neither_personalization_read_raises_when_the_pool_refuses() -> None:
    """A lane may fail; the conversation may not fail with it."""
    lane = PersonalizationLane(failing_checkout(RuntimeError("pool closed")))

    assert lane.usual_order(session_id=SESSION).declined is not None
    assert lane.recommendations(session_id=SESSION).declined is not None
