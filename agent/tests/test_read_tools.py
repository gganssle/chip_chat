"""The six read tools, and the column RFC-001 §06's table does not have.

    *Eleven tools. Note the absent column: none of them takes a visitor
    identifier, and none of the read tools has side effects.*

Issue #61 is that sentence built. What is asserted here is the tool *layer*:
which of the six are offered given what is wired, which span each opens, and
that a lane which cannot answer produces a decline the model can read rather
than an exception that takes the turn with it.

The lanes' own behaviour is asserted where the lanes live --
``snowflake/tests/test_snowflake_lanes.py`` for the account and personalization
declines, ``search/tests/test_knowledge_lane.py`` for retrieval's. Duplicating
that here would be two places to change and one place to forget.

**The first test in this file is the launch gate.** It walks every schema of
every read tool at every depth and fails if anything identifier-shaped appears.
``test_sabotage.py`` makes the same assertion across all eleven and reads as the
attack it defeats; this one is scoped to the six and is the one #61 asks for by
name, so that adding a ``demo_id`` to ``get_points_balance`` next year fails a
test whose title says what was broken.
"""

import re
import textwrap
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from chip_chat.agent.hardcoded import ACCOUNT
from chip_chat.agent.lanes import CONDITIONAL_TOOLS, NO_LANES, Lanes
from chip_chat.agent.loop import RUNTIME_CONTEXT, runtime_context
from chip_chat.agent.model import ToolInvocation
from chip_chat.agent.orders import OrderDesk
from chip_chat.agent.surface import TOOL_SPECS, argument_names, spec
from chip_chat.agent.tools import (
    TOOLS,
    dispatch,
    offered_schemas,
    offered_tools,
)
from chip_chat.otel import ToolName, agent_step, chat_turn
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.search.chunks import (
    CHUNK_ID,
    HARVESTED_AT,
    HEADING,
    KIND,
    SOURCE_URL,
    TEXT,
)
from chip_chat.search.lane import KnowledgeLane
from chip_chat.search.retrieve import Retriever
from chip_chat.snowflake import reads
from chip_chat.snowflake.lane import AccountLane, PersonalizationLane
from chip_chat.snowflake.testing import (
    FakeConnection,
    StubAnalyst,
    analyst_response,
    checkout_of,
    failing_checkout,
    sql_part,
)

SESSION = "sess-reads"
NOW = datetime.now(UTC)
"""Actually now. A mart's freshness is measured against the real clock.

The same pin that rotted in ``api/tests/test_failure_isolation.py``, in the file
next door and for the same reason. ``derived_at`` is compared to the wall clock
against :data:`~chip_chat.snowflake.reads.DEFAULT_STALE_AFTER_HOURS`, so a fixed
timestamp here describes a mart that quietly becomes stale on the second day and
stays that way -- and
``test_the_usual_order_carries_the_marts_confidence_and_derived_at`` then asserts
the opposite of what it was written to assert, without a line of source changing.

Pin a threshold, because it is configuration. Never pin the clock when the
property under test is relative.
"""

READ_TOOLS: tuple[ToolName, ...] = (
    ToolName.SEARCH_MENU_KNOWLEDGE,
    ToolName.ASK_ACCOUNT_QUESTION,
    ToolName.GET_POINTS_BALANCE,
    ToolName.GET_USUAL_ORDER,
    ToolName.GET_RECOMMENDATIONS,
    ToolName.MATCH_MEAL_FROM_PHOTO,
)
"""RFC-001 §06's six read tools, in the order that table lists them."""

_IDENTIFIER_SHAPED = re.compile(
    r"(demo|visitor|user|customer|account|member|persona|session|caller|subject)"
    r"[-_ ]?id",
    re.IGNORECASE,
)
"""Anything that reads as "whose data is this". A pattern rather than a list, so
a future ``callerId`` fails the same test that would catch ``demo_id``."""

BALANCE_SQL = (
    "SELECT SUM(l.point_change) AS points_balance "
    "FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger AS l"
)


# ---------------------------------------------------------------------------
# The launch gate: the absent column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", READ_TOOLS, ids=lambda tool: tool.value)
def test_no_read_tool_schema_contains_a_visitor_identifier(tool: ToolName) -> None:
    """#61's second acceptance criterion, and the one that must never be edited.

    RFC-001 §05: the absence of the parameter *is* the enforcement mechanism.
    There is no argument for the model to get wrong and no field an injected
    instruction can populate. Adding a ``demo_id`` to any of these six is the
    single change that would break the system's primary security guarantee, so
    it fails here by name.
    """
    offenders = sorted(
        name for name in argument_names(spec(tool)) if _IDENTIFIER_SHAPED.search(name)
    )

    assert offenders == []


def test_three_of_the_six_take_no_arguments_at_all() -> None:
    """Not an oversight to be tidied up later; it is the mechanism.

    ``get_points_balance``, ``get_usual_order`` and ``get_recommendations``
    answer for whoever is in the conversation because there is nothing else they
    could answer for.
    """
    argumentless = {tool for tool in READ_TOOLS if not spec(tool).parameters}

    assert argumentless == {
        ToolName.GET_POINTS_BALANCE,
        ToolName.GET_USUAL_ORDER,
        ToolName.GET_RECOMMENDATIONS,
    }


def test_the_six_reads_declare_three_arguments_between_them() -> None:
    """A surface this narrow is one a reviewer can hold in their head.

    ``query``, ``question``, ``blob_ref`` -- and not one of them names a person.
    """
    declared = {
        parameter.name for tool in READ_TOOLS for parameter in spec(tool).parameters
    }

    assert declared == {"query", "question", "blob_ref"}


def test_none_of_the_six_writes() -> None:
    """RFC-001 §06's second clause: none of the read tools has side effects."""
    assert not any(spec(tool).writes for tool in READ_TOOLS)


# ---------------------------------------------------------------------------
# What is offered, and what the model is told
# ---------------------------------------------------------------------------


def _lanes() -> Lanes:
    """Every lane wired, over doubles."""
    return Lanes(
        knowledge=_knowledge_lane(),
        account=_account_lane(),
        personalization=_personalization_lane(),
    )


def test_all_six_are_callable_once_their_lanes_are_wired() -> None:
    """#61's first acceptance criterion.

    ``match_meal_from_photo`` is offered by the photo lane, which
    ``test_photo_tool.py`` covers; the other five are here.
    """
    offered = offered_tools(_lanes())

    for tool in READ_TOOLS:
        if tool is not ToolName.MATCH_MEAL_FROM_PHOTO:
            assert tool in offered, tool


def test_a_tool_nothing_can_answer_is_not_offered_at_all() -> None:
    """#64's argument, applied to all three conditional tools.

    A definition the model can see and nothing can answer is worse than an
    absent one: the model calls it, the call fails, and the trace shows a lane
    outage rather than a deployment nobody finished.
    """
    assert offered_tools() == TOOLS

    for tool in CONDITIONAL_TOOLS:
        assert tool not in offered_tools()


def test_the_two_tools_with_no_honest_stand_in_stay_withdrawn() -> None:
    """A fabricated NL→SQL answer is PRD A4's forbidden plausible number, and a
    fabricated rationale is a sentence attributed to a model that never ran."""
    assert ToolName.ASK_ACCOUNT_QUESTION not in offered_tools()
    assert ToolName.GET_RECOMMENDATIONS not in offered_tools()

    account_only = offered_tools(Lanes(account=_account_lane()))

    assert ToolName.ASK_ACCOUNT_QUESTION in account_only
    assert ToolName.GET_RECOMMENDATIONS not in account_only


def test_the_offered_schemas_and_the_offered_tools_are_the_same_list() -> None:
    """Two lists that could disagree would disagree in the worst way."""
    lanes = _lanes()
    names = [schema["function"]["name"] for schema in offered_schemas(lanes)]

    assert names == [tool.value for tool in offered_tools(lanes)]


def test_the_runtime_context_names_what_is_offered() -> None:
    """The definitions and the prose have to agree about what exists."""
    lanes = _lanes()
    context = runtime_context(offered_tools(lanes), lanes=lanes)

    assert ToolName.ASK_ACCOUNT_QUESTION.value in context
    assert ToolName.GET_RECOMMENDATIONS.value in context
    assert ToolName.ASK_ACCOUNT_QUESTION.value not in RUNTIME_CONTEXT


def test_the_menu_paragraph_stops_being_true_when_a_corpus_is_wired() -> None:
    """A model told the menu is three items while retrieval returns forty will
    either contradict the corpus or refuse to read it, and both look like a
    retrieval bug."""
    wired = runtime_context(lanes=Lanes(knowledge=_knowledge_lane()))

    assert "hardcoded menu" in RUNTIME_CONTEXT
    assert "hardcoded menu" not in wired
    assert "published pages" in wired


def test_the_persona_sentence_stops_being_true_when_the_account_lane_is_wired() -> None:
    """The other half of ``docs/public-demo.md`` §9, and the more expensive half.

    Until ``cc-lpy4`` this function named
    :data:`chip_chat.agent.hardcoded.ACCOUNT` unconditionally, so a deployment
    whose account tools read the visitor's own rows still opened every
    conversation by telling the model it was serving the Ballard regular. The
    model then said both -- the store from the system message and the balance
    from the tool -- which is one sentence containing two visitors.
    """
    wired = runtime_context(lanes=_lanes())

    assert ACCOUNT.display_name in RUNTIME_CONTEXT
    assert ACCOUNT.display_name not in wired
    assert "already bound to this" in wired


def test_the_wired_persona_sentence_names_no_customer_at_all() -> None:
    """Not a different fixture. The right answer here is *no* identity.

    Naming one would be the same defect with better data: the model does not
    choose the visitor, has no tool argument to name one with, and every number
    it may say comes from a tool that answered for whoever the pool bound.
    """
    wired = runtime_context(lanes=_lanes())

    for word in ("Ballard", "regular at", "points on the card"):
        assert word not in wired


# ---------------------------------------------------------------------------
# The spans each tool opens
# ---------------------------------------------------------------------------


def dispatch_in_turn(
    tool: ToolName, lanes: Lanes = NO_LANES, **arguments: object
) -> tuple[Mapping[str, Any], SpanRecorder]:
    """Dispatch inside the parents ``tool.<name>`` is required to sit under."""
    invocation = ToolInvocation(call_id="c1", name=tool.value, arguments=arguments)
    with (
        span_recorder("agent") as spans,
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
    ):
        result = dispatch(invocation, session_id=SESSION, desk=OrderDesk(), lanes=lanes)
    return result, spans


def test_the_account_question_nests_a_cortex_analyst_span() -> None:
    """RFC-001 §09's tree, for the one read tool that generates SQL."""
    _, spans = dispatch_in_turn(
        ToolName.ASK_ACCOUNT_QUESTION,
        _lanes(),
        question="how many points do I have",
    )

    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            tool.ask_account_question
              db.cortex_analyst
    """).strip()
    )


@pytest.mark.parametrize(
    "tool",
    [
        ToolName.GET_POINTS_BALANCE,
        ToolName.GET_USUAL_ORDER,
        ToolName.GET_RECOMMENDATIONS,
    ],
    ids=lambda tool: tool.value,
)
def test_the_three_fixed_reads_open_no_child_span(tool: ToolName) -> None:
    """The tool span is the whole of them.

    ``db.cortex_analyst`` belongs to the *generated* query; a fixed one that
    borrowed the name would make "how often does the account lane generate SQL"
    unanswerable from a trace.
    """
    _, spans = dispatch_in_turn(tool, _lanes())

    assert spans.names() == (f"tool.{tool.value}", "agent.step", "chat.turn")


def test_menu_knowledge_nests_a_retriever_search_with_a_lane_wired() -> None:
    """The same tree as the hardcoded fallback, and a different index on it."""
    _, spans = dispatch_in_turn(
        ToolName.SEARCH_MENU_KNOWLEDGE,
        Lanes(knowledge=_knowledge_lane()),
        query="how do points work",
    )

    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            tool.search_menu_knowledge
              retriever.search
    """).strip()
    )


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


def test_the_points_read_uses_the_lane_when_one_is_wired() -> None:
    result, _ = dispatch_in_turn(ToolName.GET_POINTS_BALANCE, _lanes())

    assert result["points_balance"] == 1_340
    assert result["affordable_now"] == ["SIDE TORTILLA"]


def test_the_points_read_says_what_it_is_reading_when_none_is() -> None:
    """The week-one slice is a demo that admits what it is."""
    result, _ = dispatch_in_turn(ToolName.GET_POINTS_BALANCE)

    assert "hardcoded account fixture" in result["source"]


def test_the_usual_order_carries_the_marts_confidence_and_derived_at() -> None:
    """Both halves of the scope: the confidence is real, and stale says so."""
    result, _ = dispatch_in_turn(ToolName.GET_USUAL_ORDER, _lanes())

    assert result["confidence"] == pytest.approx(0.82)
    assert result["mart"]["derived_at"] is not None
    assert result["mart"]["stale"] is False


def test_the_recommendations_carry_their_rationale() -> None:
    result, _ = dispatch_in_turn(ToolName.GET_RECOMMENDATIONS, _lanes())

    assert result["items"][0]["rationale"].startswith("you order")


def test_the_knowledge_lane_returns_citable_passages() -> None:
    result, _ = dispatch_in_turn(
        ToolName.SEARCH_MENU_KNOWLEDGE,
        Lanes(knowledge=_knowledge_lane()),
        query="how do points work",
    )

    assert [passage["id"] for passage in result["passages"]] == ["rewards-1"]
    assert result["passages"][0]["harvested_at"]


# ---------------------------------------------------------------------------
# A lane may fail; the conversation may not
# ---------------------------------------------------------------------------


def _dead_lanes() -> Lanes:
    """Every Snowflake-backed lane wired to a pool that will not check out."""
    refuse = failing_checkout(RuntimeError("the pool did not answer"))
    return Lanes(
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
def test_a_lane_that_cannot_answer_declines_instead_of_raising(tool: ToolName) -> None:
    """#61's third acceptance criterion, and RFC-001 §10.

    The result is something the model can say to the visitor, so the next
    question -- which may be about a different lane entirely -- still gets
    answered.
    """
    arguments = {"question": "what did I spend"} if spec(tool).parameters else {}

    result, _ = dispatch_in_turn(tool, _dead_lanes(), **arguments)

    assert "declined" in result
    assert result["say"]


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
def test_a_declining_lane_fails_its_tool_span(tool: ToolName) -> None:
    """Otherwise an outage looks exactly like a tool that worked.

    A tool span that ended cleanly with a polite sentence in it is the one
    failure mode a dashboard cannot see, so the decline is a failed span as well
    as a readable result.
    """
    arguments = {"question": "what did I spend"} if spec(tool).parameters else {}

    _, spans = dispatch_in_turn(tool, _dead_lanes(), **arguments)

    assert spans.span_named(f"tool.{tool.value}").status.is_ok is False


def test_a_tool_call_that_arrives_without_its_lane_is_refused_readably() -> None:
    """The model asked for a lane this deployment does not have.

    A fact about the call rather than about a service, so it is a ``rejected``
    and the span is not failed -- the two are worth telling apart in a trace.
    """
    result, spans = dispatch_in_turn(
        ToolName.GET_RECOMMENDATIONS, Lanes(account=_account_lane())
    )

    assert result["rejected"] == "TOOL_NOT_IMPLEMENTED"
    assert spans.span_named("tool.get_recommendations").status.is_ok is not False


def test_an_invented_visitor_argument_is_refused_before_any_body_runs() -> None:
    """The live path, not a test fixture: ``dispatch`` binds through the surface.

    A model told to attach an identifier to every call gets a rejection it can
    read, and no lane is ever offered the extra field.
    """
    result, _ = dispatch_in_turn(
        ToolName.GET_POINTS_BALANCE, _lanes(), demo_id="someone-elses-visitor"
    )

    assert result["rejected"] == "ARGUMENTS_REJECTED"
    assert "demo_id" in result["detail"]


def test_the_surface_still_holds_eleven_and_six_of_them_read() -> None:
    """A guard on this file itself: READ_TOOLS is the six, not a subset."""
    reads_in_the_surface = [tool.name for tool in TOOL_SPECS if not tool.writes]

    assert set(READ_TOOLS) < set(reads_in_the_surface)
    assert len(reads_in_the_surface) == len(READ_TOOLS) + 1  # plus propose_order


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


def _account_lane() -> AccountLane:
    """An account lane over a connection that knows two statements."""
    connection = FakeConnection(
        {
            BALANCE_SQL: [[1_340]],
            reads._POINTS_BALANCE_SQL: [[1_340, 42, NOW]],
            reads._AFFORDABLE_REWARDS_SQL: [
                ["side-tortilla", "SIDE TORTILLA", 250, "https://example", None]
            ],
        }
    )
    return AccountLane(
        checkout_of(connection),
        StubAnalyst(analyst_response(sql_part(BALANCE_SQL, verified="points_balance"))),
    )


def _personalization_lane() -> PersonalizationLane:
    """A personalization lane over both marts, both fresh."""
    connection = FakeConnection(
        {
            reads._USUAL_ORDER_SQL: [
                ["BOWL-CHICKEN", ["MOD-GUAC"], 0.82, NOW, "Chicken Bowl"]
            ],
            reads._RECOMMENDATIONS_SQL: [
                [1, "ITEM-1", "you order the chicken bowl most weeks", 0.4, "3", NOW, "A"]
            ],
        }
    )
    return PersonalizationLane(checkout_of(connection))


class _OneHitService:
    """The narrowest possible :class:`~chip_chat.search.client.SearchService`.

    Only :meth:`search` is on the retrieval path, so the other eight raise. A
    build reaching one of them here would be a test doing something it did not
    mean to, which is worth an exception rather than a silent no-op.
    """

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        del target, query
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


def _knowledge_lane() -> KnowledgeLane:
    """A real :class:`KnowledgeLane` over one hit, so the span is the real span."""
    return KnowledgeLane(Retriever(_OneHitService()))


@pytest.fixture(autouse=True)
def _no_stale_threshold_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the staleness threshold, so a developer's shell cannot change a result."""
    monkeypatch.delenv(reads.STALE_AFTER_HOURS_VARIABLE, raising=False)
