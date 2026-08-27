"""RFC-001 §10's table, one row at a time, each verified by breaking the thing.

    *A lane may fail, the conversation may not.*

Issue #65 is that sentence with a seven-row table under it, and the acceptance
criterion is unusually specific about method: **each row is verified by
deliberately breaking that dependency and observing the specified behaviour**,
and in every case the conversation continues and the other lanes work.

The behaviours themselves are asserted where they live -- the knowledge lane's
decline in ``search/tests/test_knowledge_lane.py``, the two Snowflake lanes' in
``snowflake/tests/test_snowflake_lanes.py``, the photo lane's in
``agent/tests/test_photo_tool.py``, the write path's in ``api/tests/test_ops.py``
and ``api/tests/test_ops_routes.py``, the budget's in
``api/tests/test_spend_gate.py``. Every one of those is a good test and none of
them answers the question this file exists for, which is the *second* half of
every row: **the blast radius**. A lane can decline correctly and still take the
turn down with it, and no test that only exercises one lane will ever notice.

So each section below breaks exactly one dependency, asserts the specified
behaviour, and then asserts that every other lane still answers under the same
break. This is the only file in the repository that holds all seven lanes at
once, which is why it lives in ``api/`` -- the request tier is the one that
depends on every package -- rather than beside any one of them.

TWO ROWS HAVE A CRITERION OF THEIR OWN, AND THEY ARE THE TWO WORTH READING.

*Cortex Analyst timeout or low confidence* must **never** fall back to a
hand-written query, *asserted in code, not just in behaviour*. Behaviour is not
enough because the reassuring observation -- "the refusal came back" -- is
equally consistent with a fallback that happened to be missing today. So
:func:`test_a_declined_question_ran_no_statement_at_all` reads the statement log
off the connection: the lane reached Snowflake's door and ran nothing. And
:func:`test_the_account_lane_has_no_hand_written_question_to_fall_back_to` reads
the lane's own module: a declined answer carries no SQL, because there is none
to carry.

*Databricks job failed* must serve stale marts **with their** ``derived_at``,
never silently as fresh. The failure mode is a mart that answers beautifully and
says nothing about its age, so the assertion is on both halves -- the timestamp
is present, and the staleness is stated -- and on the health surface listing the
lane as stale while reporting it up, because a stale mart is a nightly job that
is down and not a lane that is.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from chip_chat.agent.health import LaneState, probe
from chip_chat.agent.lanes import Lanes
from chip_chat.agent.model import ToolInvocation
from chip_chat.agent.orders import OrderDesk
from chip_chat.agent.testing import ScriptedModel, answer
from chip_chat.agent.tools import PHOTO_UNAVAILABLE_MESSAGE, dispatch, offered_tools
from chip_chat.api.confirmations import ConfirmationLedger
from chip_chat.api.drafts import DraftStore
from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.limits import SpendLimits
from chip_chat.api.ops import (
    OPS_UNAVAILABLE_MESSAGE,
    OpsService,
    OpsUnavailableError,
    unavailable_card,
)
from chip_chat.api.outcome import STOP_STATE_MESSAGE, Stop, StopReason
from chip_chat.api.testing import FakeClock, RecordingWriteBackend
from chip_chat.api.turns import SpendGate
from chip_chat.otel import ToolName, agent_step, chat_turn, tool_call
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.search.chunks import (
    CHUNK_ID,
    HARVESTED_AT,
    HEADING,
    KIND,
    SOURCE_URL,
    TEXT,
)
from chip_chat.search.errors import SearchError
from chip_chat.search.lane import KnowledgeLane
from chip_chat.search.retrieve import Retriever
from chip_chat.snowflake import reads
from chip_chat.snowflake.analyst import REFUSAL, Thresholds
from chip_chat.snowflake.cortex import AnalystError
from chip_chat.snowflake.lane import AccountLane, PersonalizationLane
from chip_chat.snowflake.testing import (
    FakeConnection,
    StubAnalyst,
    analyst_response,
    checkout_of,
    failing_checkout,
    sql_part,
    suggestions_part,
)
from chip_chat.vision.describe import DescribeUnavailableError
from chip_chat.vision.lane import PhotoLane
from chip_chat.vision.store import PHOTO_REF_ARGUMENT
from chip_chat.vision.testing import STUB_PHOTO_REF, StubVisionModel, photo_lane

SESSION = "sess-isolation"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

BURRITO = "CMG-2"
WHITE_RICE = "CMG-5001"
BLACK_BEANS = "CMG-5051"
VISITOR = "dm-000001"

BALANCE_SQL = (
    "SELECT SUM(l.point_change) AS points_balance "
    "FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger AS l"
)


# ---------------------------------------------------------------------------
# Driving one tool, the way a turn does
# ---------------------------------------------------------------------------


def call(
    tool: ToolName, lanes: Lanes, **arguments: object
) -> tuple[Mapping[str, Any], SpanRecorder]:
    """Dispatch one tool inside the spans the schema requires around it."""
    invocation = ToolInvocation(call_id="c1", name=tool.value, arguments=arguments)
    with (
        span_recorder("api") as spans,
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
    ):
        result = dispatch(invocation, session_id=SESSION, desk=OrderDesk(), lanes=lanes)
    return result, spans


def declined(result: Mapping[str, Any]) -> bool:
    """Whether a tool result is a lane saying it cannot answer right now."""
    return "declined" in result


def spoke(result: Mapping[str, Any]) -> str:
    """The sentence the model is handed to say. Empty is a failure of the row."""
    return str(result.get("say") or result.get("detail") or "")


def still_answers(lanes: Lanes, *tools: ToolName) -> None:
    """Assert each tool answers under whatever break is currently in place.

    The blast-radius half of every row, factored out because it is the same
    assertion seven times and because writing it once means it cannot be
    quietly omitted from the row where it would have failed.
    """
    for tool in tools:
        arguments: dict[str, object] = {}
        if tool is ToolName.SEARCH_MENU_KNOWLEDGE:
            arguments["query"] = "what is in a burrito"
        if tool is ToolName.ASK_ACCOUNT_QUESTION:
            arguments["question"] = "how many points do I have"
        if tool is ToolName.MATCH_MEAL_FROM_PHOTO:
            arguments[PHOTO_REF_ARGUMENT] = str(STUB_PHOTO_REF)
        result, _ = call(tool, lanes, **arguments)
        assert not declined(result), f"{tool.value} declined but should not have"
        assert "rejected" not in result, f"{tool.value} was refused but should not be"


# ---------------------------------------------------------------------------
# The doubles, and the switch that breaks each one
# ---------------------------------------------------------------------------


class _SearchService:
    """A search service that answers one hit, or refuses every call.

    Only :meth:`search` is on the retrieval path. The other eight raise, so a
    test reaching one is a test doing something it did not mean to.
    """

    __slots__ = ("_down",)

    def __init__(self, *, down: bool = False) -> None:
        self._down = down

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        del target, query
        if self._down:
            raise SearchError("the search service returned 503")
        return {
            "value": [
                {
                    CHUNK_ID: "rewards-1",
                    KIND: "rewards",
                    TEXT: "You earn 10 points per $1 spent.",
                    HEADING: "Earning points",
                    SOURCE_URL: "https://www.chipotle.com/rewards",
                    HARVESTED_AT: "2026-08-20T00:00:00+00:00",
                    "@search.score": 0.031,
                    "@search.rerankerScore": 2.7,
                }
            ]
        }

    def index_names(self) -> list[str]:
        raise NotImplementedError

    def create_index(self, definition: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def delete_index(self, name: str) -> None:
        raise NotImplementedError

    def upload(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        raise NotImplementedError

    def document_count(self, index: str) -> int:
        raise NotImplementedError

    def alias_target(self, alias: str) -> str | None:
        raise NotImplementedError

    def set_alias(self, alias: str, index: str) -> None:
        raise NotImplementedError

    def delete_alias(self, alias: str) -> None:
        raise NotImplementedError


def knowledge_lane(*, down: bool = False) -> KnowledgeLane:
    """The real lane over a service that answers, or over one that does not."""
    return KnowledgeLane(Retriever(_SearchService(down=down)))


def account_connection() -> FakeConnection:
    """A connection that knows the account lane's two fixed statements.

    Returned rather than wrapped so a test can read
    :attr:`~chip_chat.snowflake.testing.FakeConnection.statements` afterwards,
    which is how the no-fallback criterion is asserted in code.
    """
    return FakeConnection(
        {
            BALANCE_SQL: [[1_340]],
            reads._POINTS_BALANCE_SQL: [[1_340, 42, NOW]],
            reads._AFFORDABLE_REWARDS_SQL: [
                ["side-tortilla", "SIDE TORTILLA", 250, "https://example", None]
            ],
        }
    )


PROBE_THRESHOLDS = Thresholds(timeout_seconds=15.0, min_confidence=0.5)
"""The shipped defaults, written out rather than read from the environment.

``Thresholds.from_env`` is what a deployment uses, and a test that let a
developer's shell move the confidence floor would pass or fail for reasons that
have nothing to do with the row it is verifying.
"""


def account_lane(
    connection: FakeConnection | None = None, *, analyst: StubAnalyst | None = None
) -> AccountLane:
    """The account lane, over a connection and an Analyst a test can break."""
    return AccountLane(
        checkout_of(connection if connection is not None else account_connection()),
        analyst
        if analyst is not None
        else StubAnalyst(
            analyst_response(sql_part(BALANCE_SQL, verified="points_balance"))
        ),
        thresholds=PROBE_THRESHOLDS,
    )


def low_confidence() -> StubAnalyst:
    """Analyst answering with suggestions and no SQL, which is it saying no.

    RFC-001 §10's row is *timeout or low confidence*, and this is the low end
    of :data:`chip_chat.snowflake.analyst.CONFIDENCE`: the service offering
    questions it could have answered instead. There is no statement in the
    response, so a lane that answered anyway would have had to write one.
    """
    return StubAnalyst(
        analyst_response(
            suggestions_part(
                "how many points do I have", "what did I order most this year"
            )
        )
    )


def personalization_lane(*, derived_at: datetime = NOW) -> PersonalizationLane:
    """The personalization lane, over marts computed at ``derived_at``.

    Moving that timestamp back is how the Databricks row is broken: nothing
    about the lane changes, and the marts it reads are simply old.
    """
    connection = FakeConnection(
        {
            reads._USUAL_ORDER_SQL: [
                ["BOWL-CHICKEN", ["MOD-GUAC"], 0.82, derived_at, "Chicken Bowl"]
            ],
            reads._RECOMMENDATIONS_SQL: [
                [
                    1,
                    "ITEM-1",
                    "you order the chicken bowl most weeks",
                    0.4,
                    "3",
                    derived_at,
                    "A",
                ]
            ],
        }
    )
    return PersonalizationLane(checkout_of(connection))


def whole_estate(**overrides: Any) -> Lanes:
    """Every lane wired and working, unless a test replaced one."""
    return Lanes(
        knowledge=overrides.get("knowledge", knowledge_lane()),
        account=overrides.get("account", account_lane()),
        personalization=overrides.get("personalization", personalization_lane()),
        photo=overrides.get("photo", photo_lane()[0]),
    )


@pytest.fixture(autouse=True)
def _pin_the_staleness_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's shell must not decide whether a mart is stale."""
    monkeypatch.delenv(reads.STALE_AFTER_HOURS_VARIABLE, raising=False)


# ---------------------------------------------------------------------------
# Row 1 — AI Search unavailable. Blast radius: knowledge only.
# ---------------------------------------------------------------------------


def test_ai_search_down_makes_the_knowledge_lane_decline_and_say_why() -> None:
    lanes = whole_estate(knowledge=knowledge_lane(down=True))

    result, spans = call(
        ToolName.SEARCH_MENU_KNOWLEDGE, lanes, query="what is in a burrito"
    )

    assert result["declined"] == "KNOWLEDGE_LANE_UNAVAILABLE"
    assert result["detail"] == "the search service returned 503"
    assert any("not answering" in note for note in result["notes"])
    assert spans.span_named("tool.search_menu_knowledge").status.is_ok is False


def test_ai_search_down_leaves_every_other_lane_answering() -> None:
    """The blast-radius column: *knowledge only*."""
    lanes = whole_estate(knowledge=knowledge_lane(down=True))

    still_answers(
        lanes,
        ToolName.ASK_ACCOUNT_QUESTION,
        ToolName.GET_POINTS_BALANCE,
        ToolName.GET_USUAL_ORDER,
        ToolName.GET_RECOMMENDATIONS,
        ToolName.MATCH_MEAL_FROM_PHOTO,
    )


def test_ai_search_down_is_named_by_the_health_surface() -> None:
    """ "The demo is broken" resolves to "the knowledge lane is down"."""
    report = probe(whole_estate(knowledge=knowledge_lane(down=True)), session_id=SESSION)

    assert report.down == ("knowledge",)
    assert report.lane("account").state is LaneState.UP
    assert report.lane("personalization").state is LaneState.UP


# ---------------------------------------------------------------------------
# Row 2 — Snowflake unavailable. Blast radius: three lanes.
# ---------------------------------------------------------------------------


def dead_snowflake() -> Lanes:
    """Both Snowflake-backed lanes over a pool that will not check out."""
    refuse = failing_checkout(RuntimeError("the warehouse did not answer"))
    return whole_estate(
        account=AccountLane(refuse, StubAnalyst(None)),
        personalization=PersonalizationLane(refuse),
    )


@pytest.mark.parametrize(
    "tool",
    [
        ToolName.ASK_ACCOUNT_QUESTION,
        ToolName.GET_POINTS_BALANCE,
        ToolName.GET_USUAL_ORDER,
        ToolName.GET_RECOMMENDATIONS,
    ],
    ids=lambda tool: tool.value,
)
def test_snowflake_down_makes_the_account_and_personalization_lanes_decline(
    tool: ToolName,
) -> None:
    arguments = {"question": "how many points do I have"} if spec_takes(tool) else {}

    result, spans = call(tool, dead_snowflake(), **arguments)

    assert declined(result)
    assert spoke(result)
    assert spans.span_named(f"tool.{tool.value}").status.is_ok is False


def test_snowflake_down_leaves_menu_questions_working() -> None:
    """The row says so in as many words: *menu questions still work*."""
    still_answers(dead_snowflake(), ToolName.SEARCH_MENU_KNOWLEDGE)


def test_snowflake_down_leaves_the_photo_lane_working() -> None:
    """Vision is not in Snowflake's blast radius, so a photo still resolves."""
    still_answers(dead_snowflake(), ToolName.MATCH_MEAL_FROM_PHOTO)


def test_snowflake_down_stops_the_write_path_without_half_writing(
    drafts: DraftStore, clock: FakeClock
) -> None:
    """The action lane is the third of the three, and it fails closed.

    A visitor who has already pressed Confirm is told ordering is unavailable
    and nothing is written -- which is the same sentence row 5 specifies,
    reached by a different route.
    """
    backend = RecordingWriteBackend()
    ops = OpsService(backend, drafts, ConfirmationLedger(clock=clock))
    draft = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, draft.draft_id)
    backend.take_down()

    with in_a_write_span(), pytest.raises(OpsUnavailableError) as refused:
        ops.session(VISITOR).place_order(draft.draft_id)

    assert refused.value.message == OPS_UNAVAILABLE_MESSAGE
    assert backend.writes == []


def test_snowflake_down_is_named_by_the_health_surface() -> None:
    report = probe(dead_snowflake(), session_id=SESSION)

    assert set(report.down) == {"account", "personalization"}
    assert report.lane("knowledge").state is LaneState.UP


# ---------------------------------------------------------------------------
# Row 3 — Cortex Analyst timeout or low confidence. Never a fallback query.
# ---------------------------------------------------------------------------


def test_a_low_confidence_answer_says_it_cannot_answer_reliably() -> None:
    """The row's own words, and PRD A4's ladder stopping at the top rung."""
    lanes = whole_estate(account=account_lane(analyst=low_confidence()))

    result, _ = call(
        ToolName.ASK_ACCOUNT_QUESTION, lanes, question="how much did I spend in June"
    )

    assert result["declined"] == "ACCOUNT_LANE_DECLINED"
    assert spoke(result) == REFUSAL
    assert "reliably" in REFUSAL


def test_an_analyst_timeout_is_the_same_refusal() -> None:
    """A transport that took longer than the deadline, rather than one that lied."""
    slow = StubAnalyst(
        analyst_response(sql_part(BALANCE_SQL, verified="points_balance")),
        elapsed_seconds=PROBE_THRESHOLDS.timeout_seconds + 5.0,
    )
    lanes = whole_estate(account=account_lane(analyst=slow))

    result, _ = call(
        ToolName.ASK_ACCOUNT_QUESTION, lanes, question="how much did I spend in June"
    )

    assert result["declined"] == "ACCOUNT_LANE_DECLINED"
    assert spoke(result) == REFUSAL


def test_an_analyst_that_could_not_be_reached_is_the_same_refusal() -> None:
    """And a transport that raised: three ways in, one sentence out."""
    lanes = whole_estate(
        account=account_lane(analyst=StubAnalyst(error=AnalystError("no token")))
    )

    result, _ = call(
        ToolName.ASK_ACCOUNT_QUESTION, lanes, question="how much did I spend in June"
    )

    assert result["declined"] == "ACCOUNT_LANE_DECLINED"
    assert spoke(result) == REFUSAL


def test_a_declined_question_ran_no_statement_at_all() -> None:
    """**Asserted in code**: the connection was never asked anything.

    This is the criterion. A refusal that came back while a hand-written query
    quietly ran would look identical from the outside, and the statement log is
    the only place the difference is visible.
    """
    connection = account_connection()
    lanes = whole_estate(account=account_lane(connection, analyst=low_confidence()))

    call(ToolName.ASK_ACCOUNT_QUESTION, lanes, question="how much did I spend in June")

    assert connection.statements == []


def test_the_refusal_carries_no_sql_for_anything_to_reach_for() -> None:
    """A statement handed upward is a statement something will eventually run."""
    lanes = whole_estate(account=account_lane(analyst=low_confidence()))

    result, _ = call(
        ToolName.ASK_ACCOUNT_QUESTION, lanes, question="how much did I spend in June"
    )

    assert "sql" not in result
    assert "estimate" in result["never"]


def test_the_account_lane_has_no_hand_written_question_to_fall_back_to() -> None:
    """The other half of "in code": there is no fallback to reach for.

    ``chip_chat.snowflake.reads`` holds exactly the fixed statements the two
    argument-free tools run -- a balance, a reward list, two marts -- and none
    of them answers a free-text question. If a general-purpose statement ever
    appears there, this fails, which is the point.
    """
    fixed = {
        name
        for name in dir(reads)
        if name.endswith("_SQL") and isinstance(getattr(reads, name), str)
    }

    assert fixed == {
        "_POINTS_BALANCE_SQL",
        "_AFFORDABLE_REWARDS_SQL",
        "_USUAL_ORDER_SQL",
        "_RECOMMENDATIONS_SQL",
    }


def test_a_declined_question_is_one_question_and_not_a_lane() -> None:
    """Blast radius: *one question*. The same lane answers the next one."""
    connection = account_connection()
    lanes = whole_estate(
        account=account_lane(
            connection,
            analyst=StubAnalyst(
                analyst_response(suggestions_part("how many points do I have")),
                analyst_response(sql_part(BALANCE_SQL, verified="points_balance")),
            ),
        )
    )

    first, _ = call(ToolName.ASK_ACCOUNT_QUESTION, lanes, question="something odd")
    second, _ = call(
        ToolName.ASK_ACCOUNT_QUESTION, lanes, question="how many points do I have"
    )

    assert declined(first)
    assert not declined(second)
    still_answers(lanes, ToolName.GET_POINTS_BALANCE, ToolName.SEARCH_MENU_KNOWLEDGE)


# ---------------------------------------------------------------------------
# Row 4 — Vision model unavailable. Blast radius: vision only.
# ---------------------------------------------------------------------------


def blind_photo_lane() -> PhotoLane:
    """A photo lane whose stage-4 deployment is not answering."""
    return photo_lane(
        model=StubVisionModel(error=DescribeUnavailableError("deployment is down"))
    )[0]


def test_vision_down_asks_the_visitor_to_describe_the_meal_in_words() -> None:
    """The row's specified behaviour, which is a question and not an apology."""
    lanes = whole_estate(photo=blind_photo_lane())

    result, spans = call(
        ToolName.MATCH_MEAL_FROM_PHOTO,
        lanes,
        **{PHOTO_REF_ARGUMENT: str(STUB_PHOTO_REF)},
    )

    assert result["detail"] == PHOTO_UNAVAILABLE_MESSAGE
    assert "Tell me what you're after" in PHOTO_UNAVAILABLE_MESSAGE
    assert spans.span_named("tool.match_meal_from_photo").status.is_ok is False


def test_vision_down_leaves_the_rest_of_the_order_flow_unchanged() -> None:
    """*The rest of the order flow is unchanged*: a draft still gets proposed."""
    lanes = whole_estate(photo=blind_photo_lane())
    desk = OrderDesk()
    invocation = ToolInvocation(
        call_id="c2",
        name=ToolName.PROPOSE_ORDER.value,
        arguments={"items": [{"item_id": next(iter(_menu_ids()))}]},
    )

    with (
        span_recorder("api"),
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
    ):
        result = dispatch(invocation, session_id=SESSION, desk=desk, lanes=lanes)

    assert result["draft"]["draft_id"]


def test_vision_down_leaves_every_other_lane_answering() -> None:
    still_answers(
        whole_estate(photo=blind_photo_lane()),
        ToolName.SEARCH_MENU_KNOWLEDGE,
        ToolName.ASK_ACCOUNT_QUESTION,
        ToolName.GET_POINTS_BALANCE,
        ToolName.GET_USUAL_ORDER,
        ToolName.GET_RECOMMENDATIONS,
    )


# ---------------------------------------------------------------------------
# Row 5 — Ops API unavailable. Blast radius: writes only.
# ---------------------------------------------------------------------------


@contextmanager
def in_a_write_span() -> Iterator[None]:
    """Open the ``tool.place_order`` an ``ops.*`` span is required to sit under.

    Not scaffolding: the schema refuses to open an ``ops.*`` span anywhere else,
    and in the deployed system the Functions host rejoins the agent's tool span
    from the trace context on the request. A write outside one is a write nobody
    can find in a trace, which is what
    ``api/tests/test_ops_routes.py::test_a_write_with_no_trace_context_is_refused_before_it_is_made``
    holds the host to.
    """
    with (
        span_recorder("api"),
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
        tool_call(ToolName.PLACE_ORDER, arguments={}),
    ):
        yield


def burrito() -> dict[str, Any]:
    """A composable Steak Burrito line: both required groups filled."""
    return {
        "item_id": BURRITO,
        "quantity": 1,
        "selections": [
            {"modifier_item_id": WHITE_RICE},
            {"modifier_item_id": BLACK_BEANS},
        ],
    }


def test_the_confirmation_card_still_renders_and_reports_the_outage(
    drafts: DraftStore,
) -> None:
    """*The card renders but reports that ordering is temporarily unavailable.*

    Rendered, not withdrawn, and not silently disabled -- a visitor who is told
    nothing assumes their order went through.
    """
    draft = drafts.propose(VISITOR, [burrito()])

    card = unavailable_card(draft.as_card())

    assert card["draft_id"] == draft.draft_id
    assert card["ordering_available"] is False
    assert card["unavailable_message"] == OPS_UNAVAILABLE_MESSAGE


def test_nothing_is_half_written_when_the_write_path_is_down(
    drafts: DraftStore, clock: FakeClock
) -> None:
    backend = RecordingWriteBackend()
    ops = OpsService(backend, drafts, ConfirmationLedger(clock=clock))
    draft = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, draft.draft_id)
    backend.take_down()

    with in_a_write_span(), pytest.raises(OpsUnavailableError):
        ops.session(VISITOR).place_order(draft.draft_id)

    assert backend.calls == []
    assert backend.writes == []


def test_the_ops_outage_message_is_not_the_budget_stop_state() -> None:
    """Two different states with two different sentences, and neither borrows.

    One is a failure and says so; the other is the cap working as designed.
    Merging them would tell a visitor the day's budget ran out because a
    Function App restarted.
    """
    assert OPS_UNAVAILABLE_MESSAGE != STOP_STATE_MESSAGE


def test_the_ops_api_being_down_leaves_every_read_lane_answering() -> None:
    """Blast radius: *writes only*. The reads never touch the Functions app."""
    still_answers(
        whole_estate(),
        ToolName.SEARCH_MENU_KNOWLEDGE,
        ToolName.ASK_ACCOUNT_QUESTION,
        ToolName.GET_POINTS_BALANCE,
        ToolName.GET_USUAL_ORDER,
        ToolName.GET_RECOMMENDATIONS,
        ToolName.MATCH_MEAL_FROM_PHOTO,
    )


def test_the_ops_outage_is_named_by_the_health_surface() -> None:
    """So that "ordering is not working" resolves to "the action lane is down"."""
    report = probe(whole_estate(), session_id=SESSION, ordering_available=False)

    assert report.down == ("action",)
    assert report.lane("knowledge").state is LaneState.UP
    assert "cards render and say so" in report.lane("action").detail


# ---------------------------------------------------------------------------
# Row 6 — Databricks job failed. Stale marts, with their derived_at.
# ---------------------------------------------------------------------------


def stale_marts() -> Lanes:
    """Marts computed nine days ago, which is what a failed nightly job leaves."""
    return whole_estate(
        personalization=personalization_lane(derived_at=NOW - timedelta(days=9))
    )


def test_a_stale_habit_is_served_with_its_derived_at() -> None:
    """Served, not withheld. The row says serve them -- and say how old they are."""
    result, _ = call(ToolName.GET_USUAL_ORDER, stale_marts())

    assert result["has_a_usual"] is True
    assert result["mart"]["stale"] is True
    assert result["mart"]["derived_at"] is not None


def test_stale_recommendations_are_served_with_their_derived_at() -> None:
    result, _ = call(ToolName.GET_RECOMMENDATIONS, stale_marts())

    assert result["items"]
    assert result["mart"]["stale"] is True
    assert result["mart"]["derived_at"] is not None


def test_a_fresh_mart_is_not_reported_stale() -> None:
    """Otherwise the flag says nothing: a detector that always fires detects nothing."""
    result, _ = call(ToolName.GET_USUAL_ORDER, whole_estate())

    assert result["mart"]["stale"] is False


def test_stale_marts_are_a_nightly_job_down_and_not_a_lane_down() -> None:
    """The alert half of the row, and the distinction that makes it actionable.

    The lane is up: it answers, and the visitor is served. What is down is the
    publish, and restarting the app would not fix it -- so the health surface
    reports the two separately.
    """
    report = probe(stale_marts(), session_id=SESSION)

    assert report.stale == ("personalization",)
    assert report.down == ()
    assert report.lane("personalization").state is LaneState.UP
    assert report.lane("personalization").derived_at is not None


def test_stale_marts_leave_every_other_lane_answering() -> None:
    still_answers(
        stale_marts(),
        ToolName.SEARCH_MENU_KNOWLEDGE,
        ToolName.ASK_ACCOUNT_QUESTION,
        ToolName.GET_POINTS_BALANCE,
        ToolName.MATCH_MEAL_FROM_PHOTO,
    )


# ---------------------------------------------------------------------------
# Row 7 — Daily budget exhausted. No model calls attempted.
# ---------------------------------------------------------------------------


def exhausted_gate(model: ScriptedModel) -> SpendGate:
    """A gate whose daily ceiling cannot fund even one turn's reservation."""
    limits = SpendLimits(
        daily_token_ceiling=100,
        session_turn_cap=5,
        session_token_cap=6_000,
        source_requests_per_window=10,
        source_window_seconds=60.0,
        turn_token_reservation=1_000,
    )
    return SpendGate(SpendGuard(limits, kill_switch=ManualKillSwitch()), lambda: model)


def test_an_exhausted_budget_is_a_friendly_stop_state_on_entry() -> None:
    """*On entry*: the door says come back tomorrow rather than opening on an error."""
    stop = exhausted_gate(ScriptedModel(answer("never reached"))).entry_state()

    assert isinstance(stop, Stop)
    assert stop.message == STOP_STATE_MESSAGE
    assert stop.reason is StopReason.DAILY_CEILING


def test_an_exhausted_budget_is_the_same_stop_state_mid_conversation() -> None:
    """*And mid-conversation*: the same sentence, from the same vocabulary."""
    model = ScriptedModel(answer("never reached"))
    gate = exhausted_gate(model)

    with (
        span_recorder("api"),
        chat_turn(session_id=SESSION, turn_index=3),
        gate.turn(session_id=SESSION, source_address="1.2.3.4") as funded,
    ):
        assert isinstance(funded, Stop)
        assert funded.message == STOP_STATE_MESSAGE


def test_an_exhausted_budget_attempts_no_model_call() -> None:
    """**No model calls attempted**, which is the row's operative clause.

    A stop that spent a token to discover it was stopped would be a cap that
    costs money to enforce, and the cap is inline in the request path precisely
    so it does not.
    """
    model = ScriptedModel(answer("never reached"))
    gate = exhausted_gate(model)

    with (
        span_recorder("api"),
        chat_turn(session_id=SESSION, turn_index=0),
        gate.turn(session_id=SESSION, source_address="1.2.3.4") as funded,
    ):
        assert not hasattr(funded, "run")

    assert model.call_count == 0


def test_the_stop_state_is_not_an_error_message() -> None:
    """PRD S4: a designed state. It never says quota and never apologises."""
    assert "quota" not in STOP_STATE_MESSAGE.lower()
    assert "error" not in STOP_STATE_MESSAGE.lower()
    assert "sorry" not in STOP_STATE_MESSAGE.lower()


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


def test_every_lane_a_row_names_is_a_lane_the_health_surface_reports() -> None:
    """A guard on this file: a lane nobody probes is a row nobody verified."""
    report = probe(whole_estate(), session_id=SESSION, ordering_available=True)

    assert {lane.lane for lane in report.lanes} == {
        "knowledge",
        "account",
        "personalization",
        "photo",
        "action",
    }


def test_a_whole_estate_is_healthy_so_the_breaks_above_mean_something() -> None:
    """The control. If this failed, every red result above would be free."""
    report = probe(whole_estate(), session_id=SESSION, ordering_available=True)

    assert report.healthy
    assert report.down == ()
    assert report.stale == ()


def test_all_eleven_tools_are_offered_when_everything_is_wired() -> None:
    """And the breaks above withdraw nothing: a down lane still offers its tool.

    Withdrawal is for a lane a deployment does not *have*. A lane that is wired
    and failing keeps its tool, because the model has to be able to call it, get
    the decline, and tell the visitor which lane is out.
    """
    assert (
        set(offered_tools(whole_estate()))
        == set(offered_tools(dead_snowflake()))
        == set(offered_tools(whole_estate(knowledge=knowledge_lane(down=True))))
    )


# ---------------------------------------------------------------------------
# Small helpers used above
# ---------------------------------------------------------------------------


def spec_takes(tool: ToolName) -> bool:
    """Whether ``tool`` declares any argument at all. Three of the six do not."""
    from chip_chat.agent.surface import spec

    return bool(spec(tool).parameters)


def _menu_ids() -> tuple[str, ...]:
    """The item ids ``propose_order`` will accept on the week-one slice."""
    from chip_chat.agent.hardcoded import MENU

    return tuple(sorted(MENU))
