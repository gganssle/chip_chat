"""The lane health surface, as an operator meets it.

``api/tests/test_failure_isolation.py`` is where this module is pointed at real
broken lanes, one row of RFC-001 §10's table at a time. This file is the other
half: the report's own behaviour, which is what somebody at the stand is reading
under pressure.

Three properties are worth testing separately from the lanes, because each is a
decision that could be got wrong without any lane misbehaving:

**Not-wired is not down.** A deployment that never had a photo lane is working
exactly as configured. A surface that painted it red would train whoever reads it
to ignore red, which is worse than having no surface.

**Stale is not down either.** A personalization lane answering off nine-day-old
marts is a lane that is up and a nightly job that is not, and the fix for the
second is not restarting the first. So the two are separate lists and the exit
code follows only the first.

**The report says which tools stop.** "The account lane is down" is a diagnosis;
"so ``ask_account_question`` and ``get_points_balance`` will decline" is the half
somebody can act on, and the eleven tool names are the vocabulary the traces are
already written in.
"""

import json

import pytest

from chip_chat.agent.__main__ import main, report
from chip_chat.agent.health import (
    LANE_TOOLS,
    HealthReport,
    LaneHealth,
    LaneState,
    probe,
)
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.tools import TOOLS
from chip_chat.otel import ToolName

SESSION = "sess-health"


# ---------------------------------------------------------------------------
# What a bare deployment reports, which is the true answer today
# ---------------------------------------------------------------------------


def test_a_deployment_with_nothing_wired_is_healthy_and_says_so() -> None:
    """The week-one slice is a working deployment, not a broken one."""
    result = probe(NO_LANES, session_id=SESSION)

    assert result.healthy
    assert result.down == ()
    assert all(lane.state is LaneState.NOT_WIRED for lane in result.lanes)


def test_every_lane_of_the_design_is_reported() -> None:
    """Five lanes. A lane nobody probes is an outage nobody can name."""
    assert tuple(lane.lane for lane in probe(session_id=SESSION).lanes) == (
        "knowledge",
        "account",
        "personalization",
        "photo",
        "action",
    )


def test_the_lane_names_are_the_ones_the_wiring_already_uses() -> None:
    """So a startup log and a health report describe the same thing."""
    described = set(Lanes().describe())

    assert described < {lane.lane for lane in probe(session_id=SESSION).lanes}


def test_every_tool_the_agent_offers_belongs_to_exactly_one_lane() -> None:
    """Otherwise "which lane is down" cannot be turned into "what stops working".

    Every unconditional tool is accounted for, and no tool is claimed twice --
    a tool in two lanes would make the blast-radius column ambiguous in exactly
    the moment somebody is relying on it.
    """
    claimed = [tool for tools in LANE_TOOLS.values() for tool in tools]

    assert len(claimed) == len(set(claimed))
    assert set(TOOLS) <= set(claimed)
    assert set(claimed) <= set(ToolName)


# ---------------------------------------------------------------------------
# The three states that are not "down"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [LaneState.UP, LaneState.NOT_WIRED, LaneState.UNPROBED])
def test_the_states_that_are_not_an_outage(state: LaneState) -> None:
    assert LaneHealth("knowledge", state).ok is True


def test_down_is_the_only_state_that_is_not_ok() -> None:
    assert LaneHealth("knowledge", LaneState.DOWN).ok is False


def test_a_stale_lane_is_still_ok() -> None:
    """The lane answers. What is late is the publish, and that is its own list."""
    stale = LaneHealth(
        "personalization", LaneState.UP, derived_at="2026-08-18", stale=True
    )

    assert stale.ok is True
    assert HealthReport((stale,)).healthy is True
    assert HealthReport((stale,)).stale == ("personalization",)
    assert HealthReport((stale,)).down == ()


# ---------------------------------------------------------------------------
# What it renders
# ---------------------------------------------------------------------------


def _mixed() -> HealthReport:
    """One of each state, which is what a half-wired deployment looks like."""
    return HealthReport(
        (
            LaneHealth("knowledge", LaneState.UP, tools=LANE_TOOLS["knowledge"]),
            LaneHealth(
                "account",
                LaneState.DOWN,
                "ACCOUNT_LANE_UNAVAILABLE: the warehouse did not answer",
                LANE_TOOLS["account"],
            ),
            LaneHealth(
                "personalization",
                LaneState.UP,
                tools=LANE_TOOLS["personalization"],
                derived_at="2026-08-18T04:11:00+00:00",
                stale=True,
            ),
            LaneHealth("photo", LaneState.NOT_WIRED, "not wired", LANE_TOOLS["photo"]),
        )
    )


def test_the_rendered_report_names_the_lane_that_is_down() -> None:
    rendered = _mixed().render()

    assert "DOWN  account" in rendered
    assert "Down: account. Every other lane answers." in rendered


def test_the_rendered_report_carries_the_lanes_own_reason() -> None:
    """Never paraphrased here: the one string worth reading is the lane's."""
    assert "the warehouse did not answer" in _mixed().render()


def test_the_rendered_report_says_the_marts_are_stale_and_how_old() -> None:
    rendered = _mixed().render()

    assert "marts stale, derived_at 2026-08-18T04:11:00+00:00" in rendered
    assert "Serving stale marts on: personalization" in rendered


def test_a_healthy_report_says_so_in_one_line() -> None:
    assert "Every wired lane answers." in probe(session_id=SESSION).render()


def test_the_json_form_carries_the_two_lists_a_check_would_read() -> None:
    body = _mixed().as_dict()

    assert body["healthy"] is False
    assert body["down"] == ["account"]
    assert body["stale"] == ["personalization"]
    assert body["lanes"][1]["tools"] == ["ask_account_question", "get_points_balance"]


def test_the_json_form_is_serialisable_as_it_stands() -> None:
    """It is what ``GET /healthz/lanes`` would return, so it has to survive dumps."""
    assert json.loads(json.dumps(_mixed().as_dict()))["down"] == ["account"]


def test_a_lane_by_name_and_a_name_nobody_has() -> None:
    assert _mixed().lane("account").state is LaneState.DOWN
    with pytest.raises(KeyError):
        _mixed().lane("ordering")


# ---------------------------------------------------------------------------
# The command an operator actually types
# ---------------------------------------------------------------------------


def test_the_command_exits_zero_on_a_deployment_that_is_working(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    assert "Every wired lane answers." in capsys.readouterr().out


def test_the_command_renders_json_when_asked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True


def test_the_command_reports_what_probe_reports() -> None:
    """One code path, so the CLI cannot drift from the module it prints."""
    assert report().as_dict() == probe(NO_LANES, session_id=SESSION).as_dict()
