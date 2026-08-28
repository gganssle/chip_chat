"""The online loop: sample, judge, monitor, alert — and count what it cost.

Four steps in a fixed order, and the order is the whole of the cost argument.

1. **Every** turn goes through the deterministic monitors. They read spans and
   cost nothing, and two of them -- the disclosure signal and the budget breach
   -- are worthless on a sample. A cross-visitor monitor running on a fifth of
   traffic misses four disclosures in five.
2. The sampling policy decides which turns get a judge, and a turn an
   **escalating** deterministic monitor fired on is always judged: something
   cheap has said this one is interesting, and the judge is what says *how*.
   Escalating is not the same as *fired*, and the difference is the one thing
   in this module that production taught rather than reasoning — the latency
   monitor fires on every turn the deployed app serves, so "judge anything that
   fired" is "judge everything", which is the sampling rate switched off
   without anybody deciding to. :func:`_escalating` and
   :attr:`~chip_chat.eval.online.monitors.Monitor.escalates` carry the rule.
3. The judged turns get the two questions of #75 -- is this grounded in what it
   retrieved, and did it decline -- from
   :class:`~chip_chat.eval.grounding.judge.ModelJudge`. The same judge, the same
   prompts, the same abstention. An online eval that asked a differently-worded
   question would produce a number that could not be compared to the offline one,
   and the comparison between them is the only way to notice that real traffic
   is harder than the golden set.
4. The judged monitors run on those turns, and every alert lands in one list.

**What this does not do is deliver an alert.** :class:`Alert` carries a severity
and the severity is a routing decision, but the routing is somebody's Azure
Monitor action group, a webhook or a Slack channel, and putting a delivery
mechanism in an eval package would make the eval untestable and the delivery
unowned. :class:`OnlineRun` hands back the alerts; the caller routes them.

**The run counts what it spent, because #76 makes that a budget line.**
:attr:`OnlineRun.judge_tokens` comes off the judge's own counter, and
:mod:`chip_chat.eval.online.budget` turns it into a share of the daily ceiling.
A monitoring loop that could not say what it cost would be the hole the ticket
names, in the module written to close it.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.grounding.judge import JudgeSpend, ModelJudge
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Turn
from chip_chat.eval.online.monitors import Alert, Severity, evaluate, monitor
from chip_chat.eval.online.sampling import Reason, SamplingDecision, SamplingPolicy
from chip_chat.eval.online.signals import LiveTurn

__all__ = ["OnlineRun", "Scored", "run_online"]


@dataclass(frozen=True, slots=True)
class Scored:
    """One live turn, after the loop has been round it.

    Attributes:
        turn: The turn.
        decision: Whether it was judged, and on what grounds.
        grounded: The judge's verdict on its claims, or ``None``.
        declined: The judge's verdict on whether it withheld an answer.
        alerts: Every monitor that fired.
    """

    turn: LiveTurn
    decision: SamplingDecision
    grounded: bool | None = None
    declined: bool | None = None
    alerts: tuple[Alert, ...] = ()


@dataclass(frozen=True, slots=True)
class OnlineRun:
    """A batch of live turns, scored.

    Attributes:
        scored: One per turn, in arrival order.
        judge_tokens: What the judging cost across the batch.
        judge_calls: Round trips made.
    """

    scored: tuple[Scored, ...] = ()
    judge_tokens: int = 0
    judge_calls: int = 0

    @property
    def turns(self) -> int:
        """Turns seen."""
        return len(self.scored)

    @property
    def judged(self) -> int:
        """Turns a judge was spent on."""
        return sum(1 for item in self.scored if item.decision.judged)

    @property
    def unreadable(self) -> int:
        """Turns whose trace could not be believed. A #103 counter, not a metric."""
        return sum(1 for item in self.scored if not item.turn.readable)

    @property
    def alerts(self) -> tuple[Alert, ...]:
        """Every alert the batch produced, in turn order."""
        return tuple(alert for item in self.scored for alert in item.alerts)

    def by_severity(self, severity: Severity) -> tuple[Alert, ...]:
        """Every alert at one severity, for a caller wiring one route."""
        return tuple(alert for alert in self.alerts if alert.severity is severity)

    @property
    def tokens_per_judged_turn(self) -> int:
        """What one judged turn cost, measured. Zero where nothing was judged."""
        return 0 if not self.judged else self.judge_tokens // self.judged

    def sampling_reasons(self) -> dict[str, int]:
        """How many turns each sampling rule claimed.

        The distribution is worth printing beside the rate: a batch where the
        always-sampled classes dominate is a batch whose real sampling rate is
        nothing like the configured one, and the budget arithmetic downstream
        assumes the configured one.
        """
        counts = {reason.value: 0 for reason in Reason}
        for item in self.scored:
            counts[item.decision.reason.value] += 1
        return counts


def run_online(
    turns: Iterable[LiveTurn],
    *,
    policy: SamplingPolicy,
    judge: ModelJudge | None = None,
) -> OnlineRun:
    """Run the loop over a batch of live turns.

    Args:
        turns: The turns, as a backend adapter produced them.
        policy: Which of them get a judge.
        judge: The judge. ``None`` runs the deterministic monitors only, which
            is a legitimate configuration -- three of the five monitors need no
            model, and running those on everything with no judge at all is a
            cheaper deployment rather than a broken one.

    Returns:
        The run.
    """
    spend = JudgeSpend() if judge is None else judge.spend
    scored: list[Scored] = []
    for turn in turns:
        deterministic = evaluate(turn)
        decision = policy.decide(turn, flagged=_escalating(deterministic))
        grounded: bool | None = None
        declined: bool | None = None
        if decision.judged and judge is not None:
            question = _as_question(turn)
            shaped = _as_turn(turn)
            grounded = judge.grounded(question, shaped)
            declined = judge.refused(question, shaped)
        alerts = evaluate(turn, grounded=grounded, declined=declined)
        scored.append(
            Scored(
                turn=turn,
                decision=decision,
                grounded=grounded,
                declined=declined,
                alerts=alerts,
            )
        )
    return OnlineRun(
        scored=tuple(scored),
        judge_tokens=spend.total_tokens,
        judge_calls=spend.calls,
    )


def _escalating(alerts: Iterable[Alert]) -> bool:
    """Whether any of these findings is a reason to spend a judge on the turn.

    Not simply *whether anything fired*, and the difference was expensive to
    learn. See :data:`~chip_chat.eval.online.monitors.BUDGET_BREACH`: the
    latency monitor fires on every turn the deployed app serves, so a rule that
    escalated on any alert escalated on everything and quietly turned a 20%
    sampling policy into a 100% one. ``Monitor.escalates`` is where that is
    decided, per monitor, with the argument beside it.
    """
    return any(monitor(alert.monitor).escalates for alert in alerts)


def _as_question(turn: LiveTurn) -> Question:
    """A live turn, in the shape the judge takes.

    Every field of the register is left at its default, and that is not
    laziness. :class:`~chip_chat.eval.grounding.questions.Question` carries what
    a *dataset row* is owed -- whether the corpus answers it, whether a citation
    is required -- and production has no such labels. The judge is documented as
    reading only the message, so handing it a question whose register is empty
    is handing it exactly what it reads, and a future judge that started
    consulting the register would fail loudly here on live traffic rather than
    quietly grading against blank labels.
    """
    return Question(entry_id=turn.trace_id, lane=Lane.NONE, message=turn.message)


def _as_turn(turn: LiveTurn) -> Turn:
    """A live turn, in the shape the judge's second argument takes."""
    return Turn(
        entry_id=turn.trace_id,
        reply=turn.reply,
        citations=turn.citations,
        claim_class=turn.claim_class,
        dropped_citations=turn.dropped_citations,
        evidence=turn.evidence,
        reports=frozenset(),
    )
