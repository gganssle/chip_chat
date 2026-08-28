"""The lane configuration, as a label a document prints and a comparison checks.

Every test here is free and offline. The builder is exercised only along the
path where it *refuses* -- an environment with no credential -- because the path
where it succeeds opens a Snowflake connection, and ``make ci`` runs nothing that
needs a logged-in human. The wired path is exercised by ``make experiment-wired``
against the live account and its numbers are in ``eval/experiments/BASELINE.md``.
"""

import pytest

from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.testing import ScriptedModel
from chip_chat.eval.golden.slice import SliceDeployment
from chip_chat.eval.grounding.slice import SliceTurnSource
from chip_chat.eval.trajectory.slice import SliceTraceSource
from chip_chat.eval.wiring import (
    LANE_CHOICES,
    NO_WIRING,
    UNSTATED,
    UNWIRED,
    WIRED,
    LaneWiringError,
    OneVisitor,
    WiredLanes,
    Wiring,
    run_lanes,
    stated,
)


class _Lane:
    """Stands in for any of the four. Nothing reads it; only its presence."""


def _wired(**lanes: object) -> Lanes:
    return Lanes(**lanes)  # type: ignore[arg-type]


def test_no_lanes_is_labelled_none_rather_than_left_empty() -> None:
    """``none`` is a measurement; empty is the absence of one."""
    assert Wiring.of(NO_LANES) == NO_WIRING
    assert NO_WIRING.label == "none"
    assert stated(NO_WIRING.label)
    assert not stated(UNSTATED)


def test_the_label_names_the_lanes_in_the_architecture_table_s_order() -> None:
    wiring = Wiring.of(_wired(personalization=_Lane(), account=_Lane()))

    assert wiring.wired == ("account", "personalization")
    assert wiring.label == "account+personalization"
    assert f"{wiring}" == "account+personalization"


def test_the_label_is_read_off_the_lanes_rather_than_from_a_flag() -> None:
    """``Lanes.describe`` is the source, so a fifth lane cannot be left out."""
    assert Wiring.of(_wired(knowledge=_Lane(), photo=_Lane())).label == "knowledge+photo"


def test_every_deployment_name_states_the_lane_configuration() -> None:
    """The name is what a report prints and what a result records as its source.

    All three adapters, because a reader who found the wiring on one document
    and not on the next would learn to stop looking for it.
    """
    model = ScriptedModel()

    assert SliceDeployment(model).name.endswith("lanes: none")
    assert SliceTraceSource(golden=(), model=model).name.endswith(  # type: ignore[arg-type]
        "lanes: none"
    )
    assert SliceTurnSource(golden=(), model=model).name.endswith(  # type: ignore[arg-type]
        "lanes: none"
    )


def test_a_wired_deployment_says_so_in_its_name() -> None:
    lanes = _wired(account=_Lane(), personalization=_Lane())

    name = SliceDeployment(ScriptedModel(), lanes=lanes).name

    assert name.endswith("lanes: account+personalization")


def test_an_unwired_run_yields_no_lanes_and_no_visitor() -> None:
    with run_lanes(UNWIRED, "regular") as wired:
        assert wired.lanes is NO_LANES
        assert wired.visitor is None
        assert "none" in wired.note


def test_the_unwired_note_names_the_three_tools_that_are_not_offered() -> None:
    """Printed before the first model call, so a forgotten flag costs nothing."""
    assert "conditional tools" in WiredLanes().note


def test_wiring_without_a_credential_refuses_rather_than_falling_back() -> None:
    """The whole point: a silent fall back produces the number that started this.

    An environment with no ``SNOWFLAKE_ACCOUNT`` is a laptop that cannot reach
    the account, and a run that quietly scored the unwired slice under a heading
    saying the deployment was measured is exactly what ``cc-lanes`` is about.
    """
    with pytest.raises(LaneWiringError) as raised, run_lanes(WIRED, "regular", env={}):
        pass  # pragma: no cover - the context never opens

    assert "SNOWFLAKE_ACCOUNT" in str(raised.value)
    assert "--lanes wired" in str(raised.value)


def test_an_unknown_choice_is_refused_at_the_door() -> None:
    with pytest.raises(ValueError, match="not one of"), run_lanes("account", "regular"):
        pass  # pragma: no cover - the context never opens

    assert LANE_CHOICES == (UNWIRED, WIRED)


def test_a_session_is_unbound_until_the_run_names_its_visitor() -> None:
    """The pool turns ``None`` into a refusal rather than an unscoped query."""
    sessions = OneVisitor()

    assert sessions.demo_id_for("golden-set-01") is None

    sessions.bind("demo-0011")

    assert sessions.demo_id_for("golden-set-01") == "demo-0011"
    assert sessions.demo_id_for("a-different-session") == "demo-0011"
