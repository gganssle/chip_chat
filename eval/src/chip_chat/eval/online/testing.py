"""Each feared condition, produced deliberately, so each monitor is demonstrated.

#76's second acceptance criterion is *each monitor tested by producing the
condition deliberately*, and the word doing the work is **producing**. A monitor
asserted to be correct by reading its ``if`` statement is a monitor nobody has
seen fire. So every condition in :mod:`chip_chat.eval.online.monitors` is built
here as a real span tree or a real turn, run through the real loop, and checked
by what came out.

**These are fixtures, and would be a fraud as evidence about the product.**
``chip_chat.eval.photos.testing`` makes the argument once and it holds here: a
turn assembled by hand tells you the detector works. It tells you nothing about
how often the condition occurs, and the drill's output must never be read as a
rate. What it establishes is the thing that is otherwise unestablished and
matters more than a rate: that when the condition happens in production,
something fires.

**The disclosure drill deliberately does the forbidden thing, in a fixture.**
:func:`cross_visitor_disclosure` builds a span carrying a second visitor's
identifier. That is the condition launch gate one exists to make impossible, and
a fixture is the only place it may be constructed -- nothing here reaches a tool
signature, an endpoint or a request path, and the invariant it violates is
violated in a data structure that never leaves this process.

``make online-drill`` runs all of it, free, with no credentials and no model.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from openinference.semconv.trace import DocumentAttributes, SpanAttributes

from chip_chat.eval.online.monitors import (
    BUDGET_BREACH,
    COST_TOKEN_CEILING,
    CROSS_VISITOR,
    LATENCY_CEILING_MS,
    PHOTO_WITHOUT_SKU,
    REFUSAL_WITH_EVIDENCE,
    UNGROUNDED_CLAIM,
    UNGROUNDED_CLAIM_JUDGED,
    Monitor,
)
from chip_chat.eval.online.signals import END_TIME, LiveTurn, read_turn
from chip_chat.eval.trajectory.trees import TraceSpan
from chip_chat.otel.attributes import ChipChatAttributes, DbAttributes
from chip_chat.otel.schema import SpanName

__all__ = ["DRILLS", "Drill", "budget_breach", "cross_visitor_disclosure", "drills"]

_APP: Final = "chip-chat-api"
_AGENT: Final = "chip-chat-agent"
_DEMO: Final = "demo-0001"
_OTHER_DEMO: Final = "demo-0002"

_NANOS_PER_MILLI: Final = 1_000_000


@dataclass(frozen=True, slots=True)
class Drill:
    """One condition, produced on purpose, and the monitor that should catch it.

    Attributes:
        name: What the drill is called.
        monitor: The monitor it exists to demonstrate.
        turn: The turn carrying the condition.
        grounded: What a judge would say, where the monitor is a judged one.
            Supplied rather than obtained: a drill that called a model would
            cost money to prove an ``if`` statement works, and would fail on a
            laptop with no credentials.
        declined: The same, for the refusal judge.
        why: What this condition is, in the ticket's own words.
    """

    name: str
    monitor: Monitor
    turn: LiveTurn
    grounded: bool | None = None
    declined: bool | None = None
    why: str = ""


def _turn_span(
    *,
    trace: str,
    demo: str = _DEMO,
    tokens: int = 1_200,
    duration_ms: float = 900.0,
) -> TraceSpan:
    """A ``chat.turn`` root, with the two numbers monitor five reads."""
    return TraceSpan(
        name=SpanName.CHAT_TURN.value,
        span_id="0" * 16,
        parent_id=None,
        trace_id=trace,
        service=_APP,
        started=0,
        attributes={
            SpanAttributes.SESSION_ID: "session-drill",
            ChipChatAttributes.DEMO_ID: demo,
            ChipChatAttributes.PERSONA_ID: "persona-loyal-regular",
            ChipChatAttributes.TOKENS_TOTAL: tokens,
            END_TIME: int(duration_ms * _NANOS_PER_MILLI),
        },
    )


def _step_span(trace: str, index: int = 0) -> TraceSpan:
    return TraceSpan(
        name=SpanName.AGENT_STEP.value,
        span_id=f"1{index:015d}",
        parent_id="0" * 16,
        trace_id=trace,
        service=_AGENT,
        started=1 + index,
        attributes={ChipChatAttributes.AGENT_STEP_INDEX: index},
    )


def _tool_span(trace: str, tool: str, order: int = 0) -> TraceSpan:
    return TraceSpan(
        name=f"tool.{tool}",
        span_id=f"2{order:015d}",
        parent_id=f"1{0:015d}",
        trace_id=trace,
        service=_AGENT,
        started=100 + order,
        attributes={SpanAttributes.TOOL_PARAMETERS: '{"question":"drill"}'},
    )


def _search_span(trace: str, passages: Sequence[tuple[str, str]]) -> TraceSpan:
    """A ``retriever.search`` carrying documents in OpenInference's flat layout."""
    attributes: dict[str, Any] = {}
    for index, (identifier, content) in enumerate(passages):
        prefix = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{index}"
        attributes[f"{prefix}.{DocumentAttributes.DOCUMENT_ID}"] = identifier
        attributes[f"{prefix}.{DocumentAttributes.DOCUMENT_CONTENT}"] = content
    return TraceSpan(
        name=SpanName.RETRIEVER_SEARCH.value,
        span_id="3" + "0" * 15,
        parent_id=f"2{0:015d}",
        trace_id=trace,
        service=_AGENT,
        started=200,
        attributes=attributes,
    )


def _matcher_span(trace: str, *, skus: Sequence[str], escalated: bool) -> TraceSpan:
    return TraceSpan(
        name=SpanName.MATCHER_RESOLVE.value,
        span_id="4" + "0" * 15,
        parent_id=f"2{0:015d}",
        trace_id=trace,
        service=_AGENT,
        started=300,
        attributes={
            ChipChatAttributes.MATCHER_RESOLVED_SKUS: list(skus),
            ChipChatAttributes.MATCHER_ESCALATED: escalated,
        },
    )


def ungrounded_menu_claim() -> Drill:
    """A food claim on a turn whose search returned nothing.

    The deterministic half of monitor one, and the one shape that needs no
    model to be certain about: a claim cannot be grounded in passages that do
    not exist.
    """
    trace = "a" * 32
    spans = (
        _turn_span(trace=trace),
        _step_span(trace),
        _tool_span(trace, "search_menu_knowledge"),
        _search_span(trace, []),
    )
    turn = read_turn(
        spans,
        message="what's in a burrito bowl",
        reply="A burrito bowl has cilantro-lime rice, black beans and your protein.",
    )
    return Drill(
        name="ungrounded_menu_claim",
        monitor=UNGROUNDED_CLAIM,
        turn=_with(turn, claim_class="food"),
        why=(
            "An ungrounded menu claim: a food claim on a turn that searched "
            "and got nothing back."
        ),
    )


def ungrounded_menu_claim_judged() -> Drill:
    """A food claim the retrieved passages do not support.

    The judged half. The turn retrieved a passage about rice and the reply makes
    a claim about pricing, which no deterministic rule can catch and a judge
    can.
    """
    trace = "b" * 32
    spans = (
        _turn_span(trace=trace),
        _step_span(trace),
        _tool_span(trace, "search_menu_knowledge"),
        _search_span(trace, [("menu-bowl-0001", "A burrito bowl starts with rice.")]),
    )
    turn = read_turn(
        spans,
        message="how much is a burrito bowl",
        reply="A burrito bowl is $9.25 and comes with free guacamole on Tuesdays.",
    )
    return Drill(
        name="ungrounded_menu_claim_judged",
        monitor=UNGROUNDED_CLAIM_JUDGED,
        turn=_with(turn, claim_class="food", citations=("menu-bowl-0001",)),
        grounded=False,
        declined=False,
        why=(
            "An ungrounded menu claim the passages do not support, which only "
            "a judge can see."
        ),
    )


def photo_match_without_confident_sku() -> Drill:
    """A matcher resolution with no SKU and no escalation.

    PRD V5 says ask, do not guess. This is the turn that did neither: nothing
    was ordered and nothing was asked, which a visitor experiences as the
    product ignoring them.
    """
    trace = "c" * 32
    spans = (
        _turn_span(trace=trace),
        _step_span(trace),
        _tool_span(trace, "match_meal_from_photo"),
        _matcher_span(trace, skus=(), escalated=False),
    )
    turn = read_turn(
        spans,
        message="make me this",
        reply="Thanks for the photo.",
    )
    return Drill(
        name="photo_match_without_confident_sku",
        monitor=PHOTO_WITHOUT_SKU,
        turn=turn,
        why=("A photo match with no confident SKU, and worse, no escalation either."),
    )


def refusal_where_the_corpus_answered() -> Drill:
    """A decline on a turn holding two passages that answer the question.

    #76 calls this *the failure mode a cautious system drifts toward*, and it is
    the only monitor whose condition is the product being too careful.
    """
    trace = "d" * 32
    spans = (
        _turn_span(trace=trace),
        _step_span(trace),
        _tool_span(trace, "search_menu_knowledge"),
        _search_span(
            trace,
            [
                ("menu-cal-0001", "Chicken bowl: 630 calories."),
                ("menu-cal-0002", "Steak burrito: 945 calories."),
            ],
        ),
    )
    turn = read_turn(
        spans,
        message="which has fewer calories, the chicken bowl or the steak burrito",
        reply=(
            "I'm not able to compare those. Please check the website for "
            "nutrition information."
        ),
    )
    return Drill(
        name="refusal_where_the_corpus_answered",
        monitor=REFUSAL_WITH_EVIDENCE,
        turn=turn,
        grounded=True,
        declined=True,
        why=(
            "A refusal where the corpus plainly had the answer — two passages, "
            "and a decline."
        ),
    )


def cross_visitor_disclosure() -> Drill:
    """A Cortex Analyst query naming a second visitor's rows.

    Launch gate one's condition, built in a fixture and nowhere else. See the
    module docstring: nothing here is reachable from a tool signature or an
    endpoint, and the identifier never leaves this process.
    """
    trace = "e" * 32
    analyst = TraceSpan(
        name=SpanName.DB_CORTEX_ANALYST.value,
        span_id="5" + "0" * 15,
        parent_id=f"2{0:015d}",
        trace_id=trace,
        service=_AGENT,
        started=400,
        attributes={
            DbAttributes.DB_QUERY_TEXT: (
                f"select sum(total) from orders where demo_id = '{_OTHER_DEMO}'"
            )
        },
    )
    spans = (
        _turn_span(trace=trace),
        _step_span(trace),
        _tool_span(trace, "ask_account_question"),
        analyst,
    )
    turn = read_turn(
        spans,
        message="what has sam spent here",
        reply="Sam has spent $412 this year.",
    )
    return Drill(
        name="cross_visitor_disclosure",
        monitor=CROSS_VISITOR,
        turn=turn,
        why=(
            "A cross-visitor disclosure signal, which should be impossible and "
            "therefore alarming."
        ),
    )


def budget_breach() -> Drill:
    """A turn over both ceilings at once.

    Both numbers on one turn deliberately: the two conditions are independent
    and a drill that produced them apart would not show that the monitor
    reports them apart.
    """
    trace = "f" * 32
    spans = (
        _turn_span(
            trace=trace,
            tokens=COST_TOKEN_CEILING + 5_000,
            duration_ms=LATENCY_CEILING_MS + 4_000,
        ),
        _step_span(trace),
        _tool_span(trace, "search_menu_knowledge"),
        _search_span(trace, [("menu-bowl-0001", "A burrito bowl starts with rice.")]),
    )
    turn = read_turn(spans, message="tell me everything", reply="Here you go.")
    return Drill(
        name="latency_or_cost_breach",
        monitor=BUDGET_BREACH,
        turn=turn,
        why="Latency and cost per conversation breaching their targets.",
    )


DRILLS: Final[tuple[str, ...]] = (
    "cross_visitor_disclosure",
    "ungrounded_menu_claim",
    "ungrounded_menu_claim_judged",
    "refusal_where_the_corpus_answered",
    "photo_match_without_confident_sku",
    "latency_or_cost_breach",
)
"""Every drill, in :data:`~chip_chat.eval.online.monitors.MONITORS` order."""


def drills() -> tuple[Drill, ...]:
    """Build every drill.

    Returns:
        One per monitor, most alarming first. A monitor with no drill is a
        monitor nobody has seen fire, and
        ``eval/tests/test_online_monitors.py`` refuses that state.
    """
    return (
        cross_visitor_disclosure(),
        ungrounded_menu_claim(),
        ungrounded_menu_claim_judged(),
        refusal_where_the_corpus_answered(),
        photo_match_without_confident_sku(),
        budget_breach(),
    )


def _with(turn: LiveTurn, **changes: Any) -> LiveTurn:
    """A turn with a few fields replaced.

    The response envelope's fields -- ``claim_class`` and ``citations`` -- do
    not come off a span today, because nothing in the request path builds a
    :class:`~chip_chat.agent.envelope.ResponseEnvelope` (bead ``cc-bap``). A
    drill sets them by hand so the monitor that reads them can be demonstrated
    now, and :func:`~chip_chat.eval.online.signals.read_turn` will fill them in
    from the trace the day that wiring lands.
    """
    from dataclasses import replace

    return replace(turn, **changes)
