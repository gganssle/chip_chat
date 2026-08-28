"""Which lane is down, answered before anybody has to guess.

RFC-001 §10's governing sentence is *a lane may fail, the conversation may not*,
and the whole of this repository is arranged so that a failing lane declines
politely instead of ending a turn. That is the right behaviour and it has one
cost, which issue #65 names in its scope: it makes an outage quiet. Cilantro
keeps answering menu questions while the account lane is dead, the visitor is
told something reasonable, and the sentence that reaches whoever is running the
stand is *"the demo is broken"* — which is both wrong and useless.

This module is what turns that sentence into *"the account lane is down, since
14:12, because the pool did not produce a bound connection"*. It probes each
lane with the cheapest question that lane answers, reads the decline the lane
already returns rather than inventing a health protocol beside it, and renders
the result as something a person can read in a terminal.

**It asks the lanes, not the services.** There is no ping to AI Search here and
no ``SELECT 1`` against Snowflake. The question worth answering during a demo is
not "is the search service up" but "will ``search_menu_knowledge`` answer", and
those differ in every way that matters: a corpus alias pointing at a deleted
index, an expired token, a semantic view that will not compile. Each lane
already has a decline path that knows the difference — the tools depend on it —
so the probe drives the same path a turn would.

**A probe costs one cheap read per wired lane and no model call.** The knowledge
lane is asked a fixed query, the account lane its fixed points read rather than
the Cortex Analyst path, personalization both marts. The photo lane is the one
exception: describing an image is a vision completion, and spending one to find
out whether the lane is up would make the health surface the most expensive
thing on the deployment. So it is reported as wired or not, and its liveness is
whatever the last turn found.

**Freshness is health.** RFC-001 §10's Databricks row says a stale mart must be
served with its ``derived_at`` and never silently as fresh. That makes staleness
an operational state rather than a data detail, so :class:`LaneHealth` carries
``derived_at`` and ``stale`` up from :class:`chip_chat.snowflake.reads.Mart` and
:meth:`HealthReport.render` prints them. A personalization lane answering
happily off marts computed nine days ago is a lane that is up and a nightly job
that is down, and the two are worth telling apart before somebody demonstrates
last week's recommendations.

WHERE THIS IS SURFACED, AND WHERE IT IS NOT YET. ``python -m chip_chat.agent``
renders a report for whatever lanes the caller assembles, which is what makes it
operable from a shell — ``az containerapp exec`` into the running container and
you have the answer. What it is not, yet, is an HTTP route: mounting
:meth:`HealthReport.as_dict` at ``GET /healthz/lanes`` is four lines in
:func:`chip_chat.api.app.create_app`, and that module belongs to the request
path rather than to the agent. The seam is deliberately this way round —
``api/`` already imports ``chip_chat.agent.lanes`` and holds the assembled
:class:`~chip_chat.agent.lanes.Lanes`, so the route has everything it needs and
this module has no opinion about HTTP.

.. code-block:: python

    report = probe(lanes, session_id=session_id)
    print(report.render())
    if not report.healthy:
        ...  # the names are in report.down

Issue #65's table is verified in ``api/tests/test_failure_isolation.py``, one
row at a time, by breaking the dependency and watching this module name it.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.otel import ToolName, agent_step, chat_turn, tool_call

__all__ = [
    "LANE_TOOLS",
    "HealthReport",
    "LaneHealth",
    "LaneState",
    "probe",
]

_PROBE_QUERY: Final = "what is on the menu"
"""What the knowledge lane is asked. Deliberately banal.

A probe that searched for something exotic would report a lane as down when it
was merely empty on that subject. This one asks the question the corpus is
least able to have nothing for, so a decline is about the lane.
"""

LANE_TOOLS: Final[dict[str, tuple[ToolName, ...]]] = {
    "knowledge": (ToolName.SEARCH_MENU_KNOWLEDGE,),
    "account": (ToolName.ASK_ACCOUNT_QUESTION, ToolName.GET_POINTS_BALANCE),
    "personalization": (ToolName.GET_USUAL_ORDER, ToolName.GET_RECOMMENDATIONS),
    "photo": (ToolName.MATCH_MEAL_FROM_PHOTO,),
    "action": (
        ToolName.PROPOSE_ORDER,
        ToolName.PLACE_ORDER,
        ToolName.CANCEL_ORDER,
        ToolName.REDEEM_POINTS,
        ToolName.UPDATE_PREFERENCES,
    ),
}
"""Which tools stop working when each lane does.

The blast-radius column of RFC-001 §10's table, in the vocabulary the traces are
already written in. Printed beside each lane so that "the account lane is down"
arrives with "so ``ask_account_question`` and ``get_points_balance`` will
decline", which is the half an operator can act on.
"""


class LaneState(Enum):
    """What a probe found.

    Three states and not two, because *not wired* is not an outage. A deployment
    that never had a photo lane is working exactly as configured, and reporting
    it red would train whoever reads this to ignore red.
    """

    UP = "up"
    DOWN = "down"
    NOT_WIRED = "not_wired"
    UNPROBED = "unprobed"
    """Wired, and deliberately not asked. The photo lane; see the module docstring."""


@dataclass(frozen=True, slots=True)
class LaneHealth:
    """One lane, as the probe found it.

    Attributes:
        lane: The name :meth:`chip_chat.agent.lanes.Lanes.describe` uses.
        state: What was found.
        detail: The lane's own decline reason, or a sentence about the state.
            Never invented here: a health surface that paraphrases an error is
            a health surface that loses the one string worth reading.
        tools: Which tools this lane answers, from :data:`LANE_TOOLS`.
        derived_at: The gold mart's own timestamp, where the lane has one.
        stale: Whether that timestamp is older than the configured threshold.
    """

    lane: str
    state: LaneState
    detail: str = ""
    tools: tuple[ToolName, ...] = ()
    derived_at: str | None = None
    stale: bool = False

    @property
    def ok(self) -> bool:
        """Whether this lane is answering, or is honestly absent.

        ``NOT_WIRED`` counts as ok for the reason :class:`LaneState` gives. A
        stale mart does **not** make the lane not-ok: the lane is answering, and
        what is broken is the nightly job. :attr:`HealthReport.stale` is where
        that is read.
        """
        return self.state in (LaneState.UP, LaneState.NOT_WIRED, LaneState.UNPROBED)

    def as_dict(self) -> dict[str, Any]:
        """Render for a JSON surface."""
        body: dict[str, Any] = {
            "lane": self.lane,
            "state": self.state.value,
            "tools": [tool.value for tool in self.tools],
        }
        if self.detail:
            body["detail"] = self.detail
        if self.derived_at is not None or self.stale:
            body["derived_at"] = self.derived_at
            body["stale"] = self.stale
        return body


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Every lane, and what an operator should do about it.

    Attributes:
        lanes: One :class:`LaneHealth` per lane, in RFC-001 §10's order.
    """

    lanes: tuple[LaneHealth, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        """Whether every lane is either answering or honestly absent."""
        return all(lane.ok for lane in self.lanes)

    @property
    def down(self) -> tuple[str, ...]:
        """The names of the lanes that are wired and not answering.

        The answer to "what is broken", in the words the runbook uses.
        """
        return tuple(lane.lane for lane in self.lanes if lane.state is LaneState.DOWN)

    @property
    def stale(self) -> tuple[str, ...]:
        """The lanes answering off marts older than the threshold.

        Separate from :attr:`down` on purpose. RFC-001 §10's Databricks row is a
        different failure with a different fix -- the lane is fine and the
        nightly publish is not -- and folding the two together would send
        somebody to restart a service that is working.
        """
        return tuple(lane.lane for lane in self.lanes if lane.stale)

    def lane(self, name: str) -> LaneHealth:
        """Return one lane by name.

        Args:
            name: As :meth:`chip_chat.agent.lanes.Lanes.describe` spells it.

        Raises:
            KeyError: If no lane has that name.
        """
        for lane in self.lanes:
            if lane.lane == name:
                return lane
        raise KeyError(name)

    def as_dict(self) -> dict[str, Any]:
        """Render for a JSON surface. What ``GET /healthz/lanes`` would return."""
        return {
            "healthy": self.healthy,
            "down": list(self.down),
            "stale": list(self.stale),
            "lanes": [lane.as_dict() for lane in self.lanes],
        }

    def render(self) -> str:
        """Render as text, for whoever is standing at the demo.

        One line per lane, and a verdict on the end that says what to say out
        loud. The point of the format is that the answer to "is the demo broken"
        is readable without scrolling.
        """
        width = max((len(lane.lane) for lane in self.lanes), default=0)
        lines = []
        for lane in self.lanes:
            mark = {
                LaneState.UP: "ok  ",
                LaneState.DOWN: "DOWN",
                LaneState.NOT_WIRED: "--  ",
                LaneState.UNPROBED: "?   ",
            }[lane.state]
            note = lane.detail
            if lane.derived_at is not None:
                aged = "stale" if lane.stale else "fresh"
                note = f"marts {aged}, derived_at {lane.derived_at}"
            lines.append(f"{mark}  {lane.lane.ljust(width)}  {note}".rstrip())
        if self.down:
            lines.append(f"\nDown: {', '.join(self.down)}. Every other lane answers.")
        else:
            lines.append("\nEvery wired lane answers.")
        if self.stale:
            lines.append(
                f"Serving stale marts on: {', '.join(self.stale)}. The nightly "
                "publish has not landed; the derived_at above is what the "
                "visitor is told."
            )
        return "\n".join(lines)


def probe(
    lanes: Lanes = NO_LANES,
    *,
    session_id: str,
    ordering_available: bool | None = None,
) -> HealthReport:
    """Ask every wired lane the cheapest question it answers, and report.

    Args:
        lanes: The backing services this deployment has.
        session_id: A bound conversation, because the Snowflake-backed lanes
            check out a connection through #44's pool and the pool takes a
            session id. A session with nothing bound produces a decline, which
            would report two working lanes as down -- so this must be a session
            the visitor store knows.
        ordering_available: What
            :meth:`chip_chat.api.ops.OpsService.available` last said, or
            ``None`` where no ops service is configured. Passed in rather than
            called here: ``agent/`` does not import ``api/``, and the direction
            of that dependency is load-bearing.

    Returns:
        The report. Never raises: a probe that fails is a lane that is down, and
        an exception here would take out the surface whose whole job is to
        survive one.
    """
    return HealthReport(
        (
            _knowledge(lanes, session_id),
            _account(lanes, session_id),
            _personalization(lanes, session_id),
            _photo(lanes),
            _action(ordering_available),
        )
    )


# ---------------------------------------------------------------------------
# One probe per lane
# ---------------------------------------------------------------------------


def _knowledge(lanes: Lanes, session_id: str) -> LaneHealth:
    """Retrieval, asked one banal query. See :data:`_PROBE_QUERY`.

    The one lane whose probe has to open spans. ``retriever.search`` is a node
    of RFC-001 §09's tree and the schema refuses to open one outside a
    ``tool.*``, which is not a rule to work around: a retrieval that appeared in
    a trace with no tool above it would be a retrieval nobody could attribute.
    So the probe opens the tree it belongs in and labels the turn as a probe,
    which makes a health check legible in a trace as a health check rather than
    as a visitor nobody can find a session for.

    The alternative -- reaching past the lane to the retriever inside it --
    would probe something other than what a turn runs, which is the one thing a
    health surface may not do.
    """
    lane = lanes.knowledge
    tools = LANE_TOOLS["knowledge"]
    if lane is None:
        return LaneHealth("knowledge", LaneState.NOT_WIRED, _unwired(), tools)
    try:
        with (
            chat_turn(session_id=session_id, turn_index=0, message=_PROBE_QUERY),
            agent_step(index=0),
            tool_call(ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": _PROBE_QUERY}),
        ):
            result = lane.search(_PROBE_QUERY).as_tool_result()
    except Exception as error:  # pragma: no cover - the lane is written not to
        return LaneHealth("knowledge", LaneState.DOWN, _raised(error), tools)
    return _from_result("knowledge", result, tools)


def _account(lanes: Lanes, session_id: str) -> LaneHealth:
    """The account lane, asked its fixed points read rather than the generated one.

    Cortex Analyst is a model call with a multi-second budget and a token cost,
    and the two failures worth telling apart here are the pool and the
    warehouse -- both of which the points read reaches. A lane that checks out a
    connection and runs a fixed statement is up; whether Analyst then writes
    good SQL is a question about a question, and it belongs in the eval suite
    rather than in a health probe.
    """
    lane = lanes.account
    tools = LANE_TOOLS["account"]
    if lane is None:
        return LaneHealth("account", LaneState.NOT_WIRED, _unwired(), tools)
    try:
        result = lane.points_balance(session_id=session_id).as_tool_result()
    except Exception as error:  # pragma: no cover - the lane is written not to
        return LaneHealth("account", LaneState.DOWN, _raised(error), tools)
    return _from_result("account", result, tools)


def _personalization(lanes: Lanes, session_id: str) -> LaneHealth:
    """Both marts, because freshness is health and the two marts publish together.

    The habit mart is read first and its ``derived_at`` is what is reported: the
    nightly publish writes all four marts in one job (#39), so one timestamp
    answers the question for both. Where the habit read declines, the ranked one
    is asked as well, since a lane that answers one and not the other is a lane
    worth reporting down with the reason from whichever failed.
    """
    lane = lanes.personalization
    tools = LANE_TOOLS["personalization"]
    if lane is None:
        return LaneHealth("personalization", LaneState.NOT_WIRED, _unwired(), tools)
    try:
        usual = lane.usual_order(session_id=session_id).as_tool_result()
    except Exception as error:  # pragma: no cover - the lane is written not to
        return LaneHealth("personalization", LaneState.DOWN, _raised(error), tools)
    if "declined" in usual:
        return _from_result("personalization", usual, tools)
    mart = usual.get("mart") or {}
    return LaneHealth(
        "personalization",
        LaneState.UP,
        "",
        tools,
        derived_at=mart.get("derived_at"),
        stale=bool(mart.get("stale", False)),
    )


def _photo(lanes: Lanes) -> LaneHealth:
    """Wired or not, and no vision completion spent to find out. See the docstring."""
    tools = LANE_TOOLS["photo"]
    if lanes.photo is None:
        return LaneHealth("photo", LaneState.NOT_WIRED, _unwired(), tools)
    return LaneHealth(
        "photo",
        LaneState.UNPROBED,
        "wired; not probed, because describing an image is a model call",
        tools,
    )


def _action(ordering_available: bool | None) -> LaneHealth:
    """The write path, reported from what the ops service last said.

    ``None`` is not an outage. It means no ops service is configured on this
    deployment, which is the same state a missing lane is in and reads the same
    way in the table.
    """
    tools = LANE_TOOLS["action"]
    if ordering_available is None:
        return LaneHealth(
            "action",
            LaneState.NOT_WIRED,
            "no ops API configured; drafts are proposed and nothing is written",
            tools,
        )
    if ordering_available:
        return LaneHealth("action", LaneState.UP, "", tools)
    return LaneHealth(
        "action",
        LaneState.DOWN,
        "the ops API is unreachable; confirmation cards render and say so",
        tools,
    )


# ---------------------------------------------------------------------------
# Reading a lane's own decline
# ---------------------------------------------------------------------------


def _from_result(
    lane: str, result: dict[str, Any], tools: tuple[ToolName, ...]
) -> LaneHealth:
    """Classify one tool result. A ``declined`` key is the lane saying it is down.

    Every lane in this system returns its decline as a result rather than
    raising -- that is what keeps a turn alive -- and the two keys it comes back
    under are ``declined``, naming the condition, and ``reason`` or ``detail``,
    carrying what actually happened -- the knowledge lane spells it ``detail``
    and the Snowflake-backed ones spell it ``reason``, so both are read. That is
    why this module needs no health protocol of its own, and why a lane cannot
    be up here and down in a turn.
    """
    declined = result.get("declined")
    if declined is None:
        return LaneHealth(lane, LaneState.UP, "", tools)
    reason = str(result.get("reason") or result.get("detail") or result.get("say") or "")
    return LaneHealth(lane, LaneState.DOWN, f"{declined}: {reason}".rstrip(": "), tools)


def _unwired() -> str:
    """What a lane that was never configured says about itself."""
    return "not wired on this deployment; its tools are not offered to the model"


def _raised(error: Exception) -> str:
    """Describe a lane that raised where it was written to decline.

    Worth its own sentence rather than folding into an ordinary decline: a lane
    raising out of a probe means the decline path itself is broken, and that is
    a bug in this repository rather than an outage in a service.
    """
    return (
        f"the lane raised {type(error).__name__} rather than declining, which is "
        f"a bug in the lane and not only an outage: {error}"
    )
