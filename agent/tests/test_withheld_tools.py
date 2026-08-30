"""A lane that is up and a tool of it that is not: the withdrawal, one name at a time.

``chip-znk`` is the bug this file holds. ``cc-lpy4`` wired the personalization
lane, which offered ``get_recommendations`` to the model, and every call of it
came back ``PERSONALIZATION_LANE_UNAVAILABLE`` because
``CHIP_CHAT.MARTS.recommendations`` has never been published --
:data:`chip_chat.snowflake.reads.RECOMMENDATIONS_MART` names the table and the
reason nothing creates it in one docstring, and RFC-001 §04's four serving marts
are the reason.

Offered-and-always-declining is the exact shape :mod:`chip_chat.agent.lanes`
argues against in its own words, so the fix is to withdraw the name rather than
the lane. What is asserted here is that withdrawing it is **narrow** and
**visible**: narrow, because ``get_usual_order`` beside it goes on reading this
visitor's own habit mart off the same connection; visible, because a tool that
vanished from the list without saying so would trade a failure an operator can
see in a trace for one they cannot see anywhere.

The corresponding assertion about the *deployment* -- that
:func:`chip_chat.api.app.build_lanes` is what withholds it, and that the
personalization lane it withholds it from is still wired -- lives in
``api/tests/test_lane_wiring.py``, because this package has no opinion about
which tables exist on anybody's Snowflake account.
"""

from datetime import UTC, datetime

from chip_chat.agent.health import LaneState, probe
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.model import ToolInvocation
from chip_chat.agent.orders import OrderDesk
from chip_chat.agent.tools import TOOLS, dispatch, offered_schemas, offered_tools
from chip_chat.otel import ToolName, agent_step, chat_turn
from chip_chat.snowflake import reads
from chip_chat.snowflake.lane import PersonalizationLane
from chip_chat.snowflake.testing import FakeConnection, checkout_of

SESSION = "sess-withheld"

NOW = datetime.now(UTC)


def _personalization() -> PersonalizationLane:
    """A personalization lane that answers both marts, over a fake connection.

    Deliberately a working lane. The bug is not a lane that fails; it is a lane
    that works and a tool of it that, on the live account, cannot -- and a
    double that declined would be testing the wrong thing, because it would pass
    just as well against the behaviour ``chip-znk`` is complaining about.
    """
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


def _wired() -> Lanes:
    """Personalization wired, nothing withheld. The state ``chip-znk`` found."""
    return Lanes(personalization=_personalization())


# ---------------------------------------------------------------------------
# The withdrawal itself
# ---------------------------------------------------------------------------


def test_a_withheld_tool_is_not_offered_even_though_its_lane_is_wired() -> None:
    """The whole of the fix, in the one place the model can see."""
    wired = _wired()

    assert ToolName.GET_RECOMMENDATIONS in offered_tools(wired)

    minus = wired.without(ToolName.GET_RECOMMENDATIONS)

    assert ToolName.GET_RECOMMENDATIONS not in offered_tools(minus)


def test_withholding_one_tool_leaves_the_rest_of_its_lane_answering() -> None:
    """Narrow on purpose.

    ``cc-lpy4`` wiring this lane is what moved ``get_usual_order`` off the
    hardcoded fixture, which was half of ``docs/public-demo.md`` §9. Withdrawing
    the lane to withdraw one tool would give that back.
    """
    minus = _wired().without(ToolName.GET_RECOMMENDATIONS)

    assert minus.personalization is not None
    assert ToolName.GET_USUAL_ORDER in offered_tools(minus)


def test_withholding_shortens_the_tool_list_and_never_reshuffles_it() -> None:
    """``run_turn`` compares tuples, so the order is load-bearing.

    A conversation opened with one order and run against another raises
    :class:`~chip_chat.agent.loop.ToolRegistrationError`, which would turn a
    withdrawal into an outage on every turn.
    """
    wired = _wired()
    minus = wired.without(ToolName.GET_RECOMMENDATIONS)

    kept = [
        tool for tool in offered_tools(wired) if tool is not ToolName.GET_RECOMMENDATIONS
    ]

    assert list(offered_tools(minus)) == kept


def test_the_schemas_the_model_is_shown_lose_the_name_too() -> None:
    """The list and the definitions are two things that could disagree.

    They disagree in the worst way: the prose says a tool is gone and the
    function-calling payload still carries its schema, so the model calls it
    anyway.
    """
    minus = _wired().without(ToolName.GET_RECOMMENDATIONS)
    names = [schema["function"]["name"] for schema in offered_schemas(minus)]

    assert ToolName.GET_RECOMMENDATIONS.value not in names
    assert names == [tool.value for tool in offered_tools(minus)]


def test_a_withheld_tool_called_anyway_is_refused_rather_than_run() -> None:
    """Belt and braces, and it is not paranoia.

    Nothing offers the name, but a model can emit any string it likes -- a
    conversation that saw the tool earlier, a prompt-injected suggestion -- and
    the answer has to be a refusal it can read rather than a query against a
    table that is not there.
    """
    minus = _wired().without(ToolName.GET_RECOMMENDATIONS)
    desk = OrderDesk()

    with (
        chat_turn(session_id=SESSION, turn_index=0, message="what should I try"),
        agent_step(index=0),
    ):
        result = dispatch(
            ToolInvocation(call_id="c1", name=ToolName.GET_RECOMMENDATIONS.value),
            session_id=SESSION,
            desk=desk,
            lanes=minus,
        )

    assert result["rejected"] == "TOOL_NOT_IMPLEMENTED"
    assert "declined" not in result


def test_withholding_nothing_is_the_default_and_changes_nothing() -> None:
    """Every deployment that has nothing to withhold is untouched by this."""
    assert NO_LANES.withheld == frozenset()
    assert offered_tools() == TOOLS
    assert NO_LANES.withdrawn() == ()


def test_without_accumulates_rather_than_replacing() -> None:
    """Two calls are two withdrawals, which is what a set union is for."""
    minus = (
        _wired().without(ToolName.GET_RECOMMENDATIONS).without(ToolName.GET_USUAL_ORDER)
    )

    assert minus.withheld == frozenset(
        {ToolName.GET_RECOMMENDATIONS, ToolName.GET_USUAL_ORDER}
    )


# ---------------------------------------------------------------------------
# And that the withdrawal is visible
# ---------------------------------------------------------------------------


def test_a_withdrawal_is_reported_rather_than_silent() -> None:
    """The half of the fix that stops it hiding the thing it fixed.

    Offered-and-declining is at least loud: a red tool span, once a turn.
    Absent-and-unexplained is loud nowhere, and *"why does it never recommend
    anything"* would be answered by a tool list with no gap in it.
    """
    minus = _wired().without(ToolName.GET_RECOMMENDATIONS)

    assert minus.withdrawn() == (ToolName.GET_RECOMMENDATIONS,)


def test_nothing_is_reported_withdrawn_from_a_lane_that_was_never_wired() -> None:
    """Nothing was taken away from a deployment that never had it.

    ``NOT_WIRED`` already says the lane's tools are not offered; reporting the
    same absence a second time under a different name would be two answers to
    one question.
    """
    never = NO_LANES.without(ToolName.GET_RECOMMENDATIONS)

    assert never.withdrawn() == ()


def test_the_health_surface_names_the_withheld_tool_beside_its_lane() -> None:
    """Where an operator actually looks. ``GET /healthz/lanes`` renders this."""
    minus = _wired().without(ToolName.GET_RECOMMENDATIONS)

    report = probe(minus, session_id=SESSION)
    personalization = report.lane("personalization")

    assert personalization.withheld == (ToolName.GET_RECOMMENDATIONS,)
    assert ToolName.GET_RECOMMENDATIONS not in personalization.tools
    assert ToolName.GET_USUAL_ORDER in personalization.tools
    assert report.withheld == (ToolName.GET_RECOMMENDATIONS,)
    assert report.as_dict()["withheld"] == [ToolName.GET_RECOMMENDATIONS.value]


def test_a_withheld_tool_is_not_an_outage() -> None:
    """A surface that painted configuration red would train somebody to ignore red."""
    minus = _wired().without(ToolName.GET_RECOMMENDATIONS)

    report = probe(minus, session_id=SESSION)

    assert report.lane("personalization").state is LaneState.UP
    assert report.healthy
    assert report.down == ()


def test_the_rendered_report_says_the_withdrawal_is_deliberate() -> None:
    """Somebody reading this under pressure must not go and restart something."""
    minus = _wired().without(ToolName.GET_RECOMMENDATIONS)

    rendered = probe(minus, session_id=SESSION).render()

    assert ToolName.GET_RECOMMENDATIONS.value in rendered
    assert "nothing to restart" in rendered


def test_describe_keeps_its_four_keys() -> None:
    """A structural contract, not a log line.

    :class:`chip_chat.eval.wiring.Wiring` builds itself with
    ``cls(**lanes.describe())``, so a fifth key here is a ``TypeError`` in
    another package.
    """
    minus = _wired().without(ToolName.GET_RECOMMENDATIONS)

    assert set(minus.describe()) == {"knowledge", "account", "personalization", "photo"}
    assert minus.describe()["personalization"] is True
