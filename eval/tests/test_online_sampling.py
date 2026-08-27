"""The sampling policy, and the judge budget it decides the size of.

Two properties matter and neither is obvious from the code. The decision has to
be **reproducible** -- *why was this turn not judged* is a question somebody asks
about exactly the turn that mattered -- and the budget has to report *unknown*
rather than a number when the ceiling is unset, because a plausible number there
is the hole #76's last acceptance criterion names, wearing an accounting's
clothes.
"""

import pytest

from chip_chat.eval.grounding.judge import JudgeSpend, ModelJudge
from chip_chat.eval.online.budget import CEILING_VARIABLE, budget_from_env
from chip_chat.eval.online.run import run_online
from chip_chat.eval.online.sampling import DEFAULT_RATE, Reason, SamplingPolicy
from chip_chat.eval.online.signals import LiveTurn
from chip_chat.eval.online.testing import drills

CEILING = {CEILING_VARIABLE: "2000000"}


def _turn(trace: str, **changes: object) -> LiveTurn:
    return LiveTurn(trace_id=trace, **changes)  # type: ignore[arg-type]


def test_the_same_trace_is_always_sampled_or_always_not() -> None:
    """Python's string hash is salted per process; this must not be."""
    policy = SamplingPolicy(rate=0.5)
    turn = _turn("a" * 32)

    first = policy.decide(turn)
    second = policy.decide(turn)

    assert first.judged == second.judged
    assert first.bucket == second.bucket


def test_the_rate_is_roughly_honoured_across_many_traces() -> None:
    policy = SamplingPolicy(rate=0.2, always_dietary=False, always_ungrounded=False)
    traces = [f"{index:032x}" for index in range(2_000)]

    judged = sum(1 for trace in traces if policy.decide(_turn(trace)).judged)

    assert 0.15 < judged / len(traces) < 0.25


def test_an_allergen_question_ignores_the_rate() -> None:
    """A fifth of a safety property is not a safety property."""
    policy = SamplingPolicy(rate=0.0)
    turn = _turn("b" * 32, message="is the chicken safe for my dairy allergy")

    decision = policy.decide(turn)

    assert decision.judged
    assert decision.reason is Reason.DIETARY


def test_a_claim_with_nothing_retrieved_ignores_the_rate() -> None:
    from chip_chat.eval.online.testing import ungrounded_menu_claim

    policy = SamplingPolicy(rate=0.0, always_flagged=False)

    decision = policy.decide(ungrounded_menu_claim().turn)

    assert decision.judged
    assert decision.reason is Reason.UNGROUNDED


def test_an_unreadable_trace_is_never_judged() -> None:
    """A verdict about half a turn is worse than no verdict."""
    policy = SamplingPolicy(rate=1.0)
    turn = LiveTurn(trace_id="c" * 32, error="two traces")

    decision = policy.decide(turn)

    assert not decision.judged
    assert decision.reason is Reason.UNREADABLE


def test_a_rate_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        SamplingPolicy(rate=1.5)


def test_the_budget_says_unknown_rather_than_inventing_a_ceiling() -> None:
    budget = budget_from_env(SamplingPolicy(), tokens_per_judged_turn=900, env={})

    assert budget.daily_ceiling is None
    assert budget.share(500) is None
    assert "unaccounted" in budget.describe(500)


def test_an_unparseable_ceiling_is_a_ceiling_nobody_has() -> None:
    budget = budget_from_env(
        SamplingPolicy(), tokens_per_judged_turn=900, env={CEILING_VARIABLE: "lots"}
    )

    assert budget.daily_ceiling is None


def test_the_budget_says_unaccounted_rather_than_zero_before_a_judged_run() -> None:
    """Nobody has measured it. That is not the same as it costing nothing."""
    budget = budget_from_env(SamplingPolicy(), env=CEILING)

    assert budget.share(500) is None
    assert "unaccounted rather than zero" in budget.describe(500)


def test_a_measured_run_turns_the_judges_into_a_share_of_the_day() -> None:
    budget = budget_from_env(
        SamplingPolicy(rate=0.2), tokens_per_judged_turn=900, env=CEILING
    )

    share = budget.share(500)

    assert share is not None
    assert 0.04 < share < 0.05
    assert budget.conversations_affordable() is not None


def test_the_loop_counts_what_it_spent() -> None:
    """A monitoring loop that cannot say what it cost is the hole, in the module."""

    class ScriptedModel:
        deployment = "scripted"

        def complete(self, messages: object, *, tools: object = ()) -> object:
            from chip_chat.agent.model import ModelReply

            return ModelReply(content="SUPPORTED", prompt_tokens=400, completion_tokens=2)

    judge = ModelJudge(ScriptedModel(), spend=JudgeSpend())  # type: ignore[arg-type]
    turns = [drill.turn for drill in drills()]

    run = run_online(turns, policy=SamplingPolicy(rate=1.0), judge=judge)

    assert run.judged == len([turn for turn in turns if turn.readable])
    assert run.judge_tokens > 0
    assert run.tokens_per_judged_turn > 0


def test_the_loop_runs_the_deterministic_monitors_with_no_judge_at_all() -> None:
    """A cheaper deployment, not a broken one."""
    run = run_online([drill.turn for drill in drills()], policy=SamplingPolicy(rate=0.0))

    assert run.judge_tokens == 0
    assert any(alert.monitor == "cross_visitor_disclosure" for alert in run.alerts)


def test_the_default_rate_is_the_one_the_module_argues_for() -> None:
    assert DEFAULT_RATE == 0.20
    assert "20%" in SamplingPolicy().describe()
