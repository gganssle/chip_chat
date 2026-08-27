"""Every monitor, demonstrated by producing its condition.

#76's second acceptance criterion, as tests rather than as a claim. The first
one asserts the state the ticket is really guarding against: a monitor that
exists in :data:`~chip_chat.eval.online.monitors.MONITORS` and has never been
seen to fire.
"""

import pytest

from chip_chat.eval.online.monitors import (
    COST_TOKEN_CEILING,
    LATENCY_CEILING_MS,
    MONITORS,
    Severity,
    evaluate,
)
from chip_chat.eval.online.sampling import Reason, SamplingPolicy
from chip_chat.eval.online.signals import LiveTurn, read_turn
from chip_chat.eval.online.testing import Drill, drills


def test_every_monitor_has_a_drill() -> None:
    """A monitor nobody has produced the condition for is a monitor nobody has seen."""
    covered = {drill.monitor.name for drill in drills()}

    assert covered == {monitor.name for monitor in MONITORS}


@pytest.mark.parametrize("drill", drills(), ids=lambda drill: drill.name)
def test_the_monitor_fires_on_its_own_condition(drill: Drill) -> None:
    alerts = evaluate(drill.turn, grounded=drill.grounded, declined=drill.declined)

    assert any(alert.monitor == drill.monitor.name for alert in alerts)


def test_the_disclosure_signal_pages_and_nothing_else_does() -> None:
    """It should be impossible, and therefore it is the one that wakes somebody."""
    paging = [monitor for monitor in MONITORS if monitor.severity is Severity.PAGE]

    assert [monitor.name for monitor in paging] == ["cross_visitor_disclosure"]


def test_three_of_the_monitors_need_no_model_and_therefore_run_on_everything() -> None:
    """A disclosure monitor sampling a fifth of traffic misses four in five."""
    deterministic = [monitor for monitor in MONITORS if not monitor.judged]

    assert len(deterministic) >= 3
    assert "cross_visitor_disclosure" in {monitor.name for monitor in deterministic}
    assert "latency_or_cost_breach" in {monitor.name for monitor in deterministic}


def test_an_ordinary_turn_fires_nothing() -> None:
    """Otherwise every alert is noise and nobody reads the channel."""
    from chip_chat.eval.online.testing import ungrounded_menu_claim

    quiet = ungrounded_menu_claim().turn
    from dataclasses import replace

    quiet = replace(quiet, claim_class="none")

    assert evaluate(quiet) == ()


def test_an_unjudged_turn_is_not_the_same_as_a_clean_one() -> None:
    """``None`` is not ``True``: nobody asked, so the judged monitors stay silent."""
    from chip_chat.eval.online.testing import ungrounded_menu_claim_judged

    drill = ungrounded_menu_claim_judged()

    unjudged = evaluate(drill.turn, grounded=None, declined=None)

    assert not any(alert.judged for alert in unjudged)


def test_a_refusal_on_a_turn_that_retrieved_nothing_is_the_product_working() -> None:
    """*Where the published data stops, stop.* Only a decline WITH evidence fires."""
    from chip_chat.eval.online.testing import refusal_where_the_corpus_answered

    drill = refusal_where_the_corpus_answered()
    empty = LiveTurn(trace_id=drill.turn.trace_id, reply=drill.turn.reply)

    assert evaluate(empty, declined=True) == ()


def test_a_matcher_that_escalated_reads_differently_from_one_that_did_not() -> None:
    from chip_chat.eval.online.testing import photo_match_without_confident_sku

    drill = photo_match_without_confident_sku()
    alerts = evaluate(drill.turn)

    assert any("did NOT escalate" in alert.detail for alert in alerts)


def test_a_missing_duration_is_not_measured_rather_than_fast() -> None:
    """The most flattering bug a monitor can have, refused."""
    turn = LiveTurn(trace_id="a" * 32, total_tokens=1, duration_ms=0.0)

    assert not [
        alert for alert in evaluate(turn) if alert.monitor == "latency_or_cost_breach"
    ]


def test_both_budget_conditions_are_reported_apart() -> None:
    from chip_chat.eval.online.testing import budget_breach

    alerts = [
        alert
        for alert in evaluate(budget_breach().turn)
        if alert.monitor == "latency_or_cost_breach"
    ]

    assert len(alerts) == 2
    assert any(f"{LATENCY_CEILING_MS:.0f} ms" in alert.detail for alert in alerts)
    assert any(str(COST_TOKEN_CEILING) in alert.detail for alert in alerts)


def test_an_unreadable_trace_fires_nothing_rather_than_everything() -> None:
    """A monitoring failure buried under five product alerts helps nobody."""
    turn = LiveTurn(trace_id="a" * 32, error="the trace could not be read")

    assert evaluate(turn) == ()


def test_a_flagged_turn_is_always_judged() -> None:
    """Something cheap said it is interesting; the judge says what about it."""
    policy = SamplingPolicy(rate=0.0)
    turn = read_turn(())

    for drill in drills():
        decision = policy.decide(drill.turn, flagged=True)
        assert decision.judged
        assert decision.reason is Reason.FLAGGED
    assert not policy.decide(turn).judged
