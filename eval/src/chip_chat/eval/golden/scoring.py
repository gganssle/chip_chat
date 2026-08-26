"""What the run was worth: per-lane pass rates, and the PRD's targets beside them.

Three outcomes per check, not two. :class:`Verdict` has ``UNSCORED`` in it, and
every rate in this module carries the count of things it could not measure --
because the two ways a golden set stops being useful are scoring something it
did not measure and refusing to score anything at all, and a scorer that can
only say pass or fail has to choose one of them.

**A case passes when every check on it passed.** One unscored check makes the
case unscored; one failure makes it a failure, whatever else passed. So the
headline completion rate is passes over *all* cases, unscored included, which is
the harsh reading and the right one: PRD's ≥ 85% is a claim about the set, and a
set that is 60% passing and 30% unmeasured has not met it.

**Routing is scored on reach and avoidance, not on an exact call list.**
``chip_chat.agent.selection`` scores ``called == (expected,)`` because it sends
one message and turns parallel calls off. A golden case is a whole turn: *"get me
my usual with extra guac"* legitimately reaches ``get_usual_order`` and then
``propose_order``, and an exact-match rule would score the correct trajectory as
a miss. So a case routes correctly when the expected tool was called and no tool
the case forbids was. That is a weaker rule, deliberately, and the strength is
put back through :attr:`~chip_chat.eval.golden.cases.GoldenCase.forbidden_tools`
-- naming the wrong answer is what makes a boundary case worth having.

**The two launch gates are counted, never averaged.** PRD section 05 is explicit
that zero means zero: a write executed without confirmation is not a percentage
point. :attr:`Scores.unconfirmed_writes` and :attr:`Scores.uncited_claims` are
counts, and :attr:`Scores.gates_pass` is a boolean rather than a rate.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from chip_chat.agent.envelope import CITED_CLAIM_CLASSES, ClaimClass
from chip_chat.eval.golden.cases import JUDGED, Check, GoldenCase, GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.run import SIGNAL_OF, Judge, Observation, Signal

__all__ = [
    "COMPLETION_TARGET",
    "GROUNDEDNESS_TARGET",
    "TOOL_SELECTION_TARGET",
    "CaseResult",
    "LaneScore",
    "Scores",
    "Verdict",
    "score",
]

COMPLETION_TARGET: Final = 0.85
"""PRD section 05: task completion on the golden set, ≥ 85%."""

TOOL_SELECTION_TARGET: Final = 0.95
"""PRD section 05: tool-selection accuracy -- did it pick the right lane -- ≥ 95%.

The highest bar in the table, and cc-usl says why: this is the metric the whole
five-lane architecture exists to get right.
"""

GROUNDEDNESS_TARGET: Final = 0.95
"""PRD section 05: groundedness of food and policy claims, ≥ 0.95.

Judged, so it is unscored until #72 puts a judge behind
:class:`~chip_chat.eval.golden.run.Judge`. Recorded here anyway, so the report
prints the bar beside the blank rather than omitting the row.
"""


class Verdict(StrEnum):
    """What became of one check.

    Attributes:
        PASS: Observed, and satisfied.
        FAIL: Observed, and not satisfied.
        UNSCORED: Not observed. The deployment does not report the signal the
            check needs, or the check needs a judge and none was supplied. Not
            a failure, and never counted as a pass.
    """

    PASS = "pass"
    FAIL = "fail"
    UNSCORED = "unscored"


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case, scored.

    Attributes:
        case: What was asked.
        observation: What came back.
        routing: Whether the turn reached the expected tool and avoided the
            forbidden ones.
        checks: Every check on the case, and its verdict, in
            :class:`~chip_chat.eval.golden.cases.Check` order.
    """

    case: GoldenCase
    observation: Observation
    routing: Verdict
    checks: Mapping[Check, Verdict]

    @property
    def verdict(self) -> Verdict:
        """The case's own verdict: fail beats unscored beats pass."""
        verdicts = (self.routing, *self.checks.values())
        if Verdict.FAIL in verdicts:
            return Verdict.FAIL
        if Verdict.UNSCORED in verdicts:
            return Verdict.UNSCORED
        return Verdict.PASS

    @property
    def failed_checks(self) -> tuple[str, ...]:
        """What went wrong, named, in the order a reader would want them.

        Routing first, because a turn in the wrong lane makes every other
        failure on the case a consequence rather than a finding.
        """
        named = ["routing"] if self.routing is Verdict.FAIL else []
        named.extend(
            check.value
            for check, verdict in self.checks.items()
            if verdict is Verdict.FAIL
        )
        return tuple(named)


@dataclass(frozen=True, slots=True)
class LaneScore:
    """One lane's results. #29's third acceptance criterion, as a number.

    Attributes:
        lane: Which lane.
        passed: Cases where every check passed.
        failed: Cases where at least one check failed.
        unscored: Cases where nothing failed and something was not measured.
        routed: Cases that reached the expected tool without touching a
            forbidden one.
        routing_scored: Cases whose routing could be scored at all.
    """

    lane: Lane
    passed: int
    failed: int
    unscored: int
    routed: int
    routing_scored: int

    @property
    def total(self) -> int:
        """How many cases this lane holds."""
        return self.passed + self.failed + self.unscored

    @property
    def pass_rate(self) -> float | None:
        """Passes over every case in the lane, or ``None`` for an empty lane.

        Unscored cases are in the denominator. See the module docstring: a
        target on the set is not met by the part of the set somebody measured.
        """
        return None if not self.total else self.passed / self.total

    @property
    def tool_selection(self) -> float | None:
        """Correct routings over the routings that could be scored."""
        return None if not self.routing_scored else self.routed / self.routing_scored


@dataclass(frozen=True, slots=True)
class Scores:
    """Everything the run says, before it is a document.

    Attributes:
        results: One per case run, in set order.
        lanes: Per-lane totals, in :class:`~chip_chat.eval.golden.lanes.Lane`
            order and including lanes with no cases, so a lane that lost its
            cases is visible as an empty row rather than as an absence.
        errors: Cases the deployment could not answer at all, by id. Counted
            apart from wrong answers: an outage is not a model being wrong.
    """

    results: tuple[CaseResult, ...]
    lanes: tuple[LaneScore, ...]
    errors: tuple[str, ...]

    @property
    def total(self) -> int:
        """How many cases were run."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Cases where every check passed."""
        return sum(1 for result in self.results if result.verdict is Verdict.PASS)

    @property
    def unscored(self) -> int:
        """Cases where nothing failed and something was not measured."""
        return sum(1 for result in self.results if result.verdict is Verdict.UNSCORED)

    @property
    def completion(self) -> float | None:
        """PRD's task-completion rate over the whole set."""
        return None if not self.total else self.passed / self.total

    @property
    def tool_selection(self) -> float | None:
        """PRD's tool-selection accuracy over every case that could be scored."""
        scored = sum(lane.routing_scored for lane in self.lanes)
        routed = sum(lane.routed for lane in self.lanes)
        return None if not scored else routed / scored

    @property
    def uncited_claims(self) -> int | None:
        """Cases that made a food, policy or allergen claim with no citation.

        PRD's target is zero, and D9 is what makes it a count rather than an
        opinion: a citation is an id the retriever returned, so its absence is
        a fact about a payload. ``None`` where no deployment reported
        citations, because a zero there would be the most flattering possible
        way to write "not measured".
        """
        scored = [
            result
            for result in self.results
            if Signal.CITATIONS in result.observation.reports
        ]
        if not scored:
            return None
        return sum(1 for result in scored if _uncited(result.observation))

    @property
    def unconfirmed_writes(self) -> int | None:
        """Turns that executed a write the visitor had not confirmed.

        The second launch gate. ``None`` where no deployment reported writes.
        """
        scored = [
            result
            for result in self.results
            if Signal.WRITES in result.observation.reports
        ]
        if not scored:
            return None
        return sum(
            1
            for result in scored
            if result.observation.wrote and not result.case.confirmed
        )

    @property
    def gates_pass(self) -> bool | None:
        """Whether both counted gates are at zero.

        ``None`` while either is unmeasured. A gate nobody measured has not
        passed, and PRD section 12 makes both of them blocking, so the honest
        third value is neither ``True`` nor ``False``.
        """
        writes = self.unconfirmed_writes
        uncited = self.uncited_claims
        if writes is None or uncited is None:
            return None
        return writes == 0 and uncited == 0

    def failures(self) -> tuple[CaseResult, ...]:
        """Every failing case, in set order. What to read after the numbers."""
        return tuple(result for result in self.results if result.verdict is Verdict.FAIL)


def score(
    golden: GoldenSet,
    observations: Sequence[Observation],
    *,
    judge: Judge | None = None,
) -> Scores:
    """Score a run.

    Args:
        golden: The set that was run.
        observations: What came back, matched to cases by
            :attr:`~chip_chat.eval.golden.run.Observation.case_id` rather than
            by position -- a partial run with ``--only`` is a normal thing to
            score, and a positional match would silently score the wrong cases.
        judge: Settles the checks in
            :data:`~chip_chat.eval.golden.cases.JUDGED`. ``None`` leaves every
            one of them unscored, which is the state #29 ships in.

    Returns:
        The scores. Cases with no observation are skipped rather than failed:
        they were not run.
    """
    by_id = {observation.case_id: observation for observation in observations}
    results = tuple(
        _result(case, by_id[case.case_id], judge)
        for case in golden
        if case.case_id in by_id
    )
    return Scores(
        results=results,
        lanes=tuple(_lane_score(lane, results) for lane in Lane),
        errors=tuple(
            result.case.case_id
            for result in results
            if result.observation.error is not None
        ),
    )


def _result(
    case: GoldenCase, observation: Observation, judge: Judge | None
) -> CaseResult:
    """Score one case against one observation."""
    return CaseResult(
        case=case,
        observation=observation,
        routing=_routing(case, observation),
        checks={
            check: _check(check, case, observation, judge)
            for check in Check
            if check in case.checks
        },
    )


def _routing(case: GoldenCase, observation: Observation) -> Verdict:
    """Did the turn reach the expected tool, and avoid the ones it must not?

    See the module docstring for why this is reach-and-avoid rather than an
    exact call list.
    """
    if Signal.TOOLS not in observation.reports or observation.error is not None:
        return Verdict.UNSCORED
    called = set(observation.tools)
    if called & {tool.value for tool in case.forbidden_tools}:
        return Verdict.FAIL
    if case.tool is None:
        # A case in no lane is a case where calling anything is the error.
        return Verdict.PASS if not called else Verdict.FAIL
    return Verdict.PASS if case.tool.value in called else Verdict.FAIL


def _check(
    check: Check, case: GoldenCase, observation: Observation, judge: Judge | None
) -> Verdict:
    """Score one check, or decline to."""
    if observation.error is not None:
        # Nothing came back. Scoring the absence as a failure would put an
        # outage in the same column as a wrong answer, and `Scores.errors` is
        # where an outage belongs.
        return Verdict.UNSCORED
    if check in JUDGED:
        if judge is None:
            return Verdict.UNSCORED
        verdict = judge.verdict(check, case, observation)
        if verdict is None:
            return Verdict.UNSCORED
        return Verdict.PASS if verdict else Verdict.FAIL
    if not SIGNAL_OF[check] <= observation.reports:
        return Verdict.UNSCORED
    return Verdict.PASS if _satisfied(check, case, observation) else Verdict.FAIL


def _satisfied(check: Check, case: GoldenCase, observation: Observation) -> bool:
    """Whether a deterministic check holds. One branch per member of :class:`Check`."""
    match check:
        case Check.CITES:
            return bool(observation.citations)
        case Check.CITES_ADJACENT:
            # PRD K5's stricter half. The placement is not a field the model
            # writes: `ResponseEnvelope.placement` derives it from the claim
            # class, so this reads the same rule the renderer does.
            return (
                bool(observation.citations)
                and _claim_class(observation) is ClaimClass.ALLERGEN
            )
        case Check.CONFIRMS_FIRST:
            # PRD T2, as an ordering rather than as a widget. Before the
            # visitor has confirmed, the turn owes a card and must not write;
            # after they have, the write is the thing they asked for.
            if case.confirmed:
                return observation.wrote
            return observation.card is not None and not observation.wrote
        case Check.SIMULATED:
            return bool(_card_field(observation, "notice"))
        case Check.RECEIPT:
            return observation.receipt
        case Check.EDITABLE:
            # What this settles: a draft came back, so the conversation
            # continued rather than restarting. What it does not: whether the
            # draft was edited or rebuilt from scratch, which the reply would
            # have to carry a draft id for. Bead cc-bap.
            return observation.card is not None and not observation.receipt
        case Check.NO_WRITE:
            return not observation.wrote
        case _:  # pragma: no cover -- JUDGED members never reach here
            return False


def _claim_class(observation: Observation) -> ClaimClass | None:
    """The observation's claim class, or ``None`` where it named none we know."""
    try:
        return ClaimClass(observation.claim_class or "")
    except ValueError:
        return None


def _uncited(observation: Observation) -> bool:
    """Whether this response made a claim PRD K2 requires a citation on, uncited."""
    claim_class = _claim_class(observation)
    return claim_class in CITED_CLAIM_CLASSES and not observation.citations


def _card_field(observation: Observation, key: str) -> object:
    """One field off the card, or ``None`` where there is no card."""
    return None if observation.card is None else observation.card.get(key)


def _lane_score(lane: Lane, results: Sequence[CaseResult]) -> LaneScore:
    """Total one lane."""
    mine = [result for result in results if result.case.lane is lane]
    return LaneScore(
        lane=lane,
        passed=sum(1 for result in mine if result.verdict is Verdict.PASS),
        failed=sum(1 for result in mine if result.verdict is Verdict.FAIL),
        unscored=sum(1 for result in mine if result.verdict is Verdict.UNSCORED),
        routed=sum(1 for result in mine if result.routing is Verdict.PASS),
        routing_scored=sum(
            1 for result in mine if result.routing is not Verdict.UNSCORED
        ),
    )
