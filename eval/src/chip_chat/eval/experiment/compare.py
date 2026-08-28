"""Two results, and what moved between them.

This is the module #73's headline sentence is about. *"I tweaked the system
prompt and it feels better"* becomes a number only if there is a second number to
subtract it from, and the subtraction has to be done in three places at once or
it answers the wrong question.

**An aggregate delta is the thing this module exists not to report on its own.**
The ticket says so: *per-lane and per-requirement breakdowns rather than one
aggregate number that hides a regression in one lane behind an improvement in
another*. So :class:`Comparison` computes a delta per headline metric, per lane
and per requirement, and :attr:`Comparison.regressions` is assembled from the
second and third rather than from the first. A candidate that gains four points
of task completion while losing the account lane is a regression this reports,
and the aggregate alone would have called it an improvement.

**Two results are only comparable if they scored the same thing.**
:attr:`Comparison.comparable` checks the dataset version, and
:attr:`Comparison.warnings` says what else differs -- a different source, a
different judge, a different set of inert axes, a different set of lanes wired.
None of those is refused, because refusing would make the harness useless
exactly when somebody needs it (scoring last month's baseline against today's
candidate is the normal case), and every one of them changes what a delta means.
So they are printed above the table rather than enforced below it.

**One thing is refused, and it is not a difference.**
:attr:`Comparison.stated` is false when either side does not record which lanes
it had wired, and that comparison is not rendered at all. The distinction is the
whole of ``cc-lanes``: a stated difference is something a reader can weigh, and
an unstated one is a delta that looks identical whether a prompt got better or a
lane came up. On 27 August 2026 the second of those was worth twenty points of
tool selection and nobody could have seen it in the document. So the tables are
replaced by a refusal that says which side did not say, and the CLI exits
non-zero.

**A delta on an unmeasured metric is not zero.** Where either side is ``None``
the delta is ``None`` and the row reads *unmeasured*, for the reason every eval
under this one keeps ``unscored`` as a third verdict: two runs that both failed
to measure groundedness have not agreed about groundedness.

**Noise is not a finding, and the threshold is stated rather than assumed.**
:data:`MATERIAL` is the movement below which a rate change is reported and not
called a regression. Thirty-four rows means one row is three points, so a
threshold under that would make every comparison a regression report; a
threshold over it would hide a whole row moving. Counts have no threshold at
all: PRD section 05 makes the gates zero, and one uncited claim more than last
time is one more than zero.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from chip_chat.eval.experiment.results import (
    TARGETS,
    ExperimentResult,
    LaneResult,
    RequirementResult,
    Target,
    delegated,
)
from chip_chat.eval.wiring import stated

__all__ = [
    "MATERIAL",
    "Comparison",
    "LaneDelta",
    "MetricDelta",
    "RequirementDelta",
    "compare",
]

MATERIAL: Final = 0.03
"""The smallest rate movement this calls a regression rather than noise.

Three points, because the dataset is thirty-four rows and one row is 2.9% of it.
A threshold below one row would report a regression every time a single case
flipped for a reason nobody can reproduce; a threshold above one row would hide
a case that genuinely broke. There is no honest third option at this size, and
the right fix is a bigger dataset -- which is #77's job, not this module's.
"""


@dataclass(frozen=True, slots=True)
class MetricDelta:
    """One headline metric, before and after.

    Attributes:
        target: The row of PRD section 05's table.
        baseline: The recorded value, or ``None``.
        candidate: The new value, or ``None``.
    """

    target: Target
    baseline: float | None
    candidate: float | None

    @property
    def delta(self) -> float | None:
        """Candidate minus baseline, or ``None`` where either is unmeasured."""
        if self.baseline is None or self.candidate is None:
            return None
        return self.candidate - self.baseline

    @property
    def improved(self) -> bool | None:
        """Whether the movement is in the direction the target wants."""
        delta = self.delta
        if delta is None or delta == 0:
            return None
        return delta > 0 if self.target.higher_is_better else delta < 0

    @property
    def regressed(self) -> bool:
        """Whether this moved materially the wrong way.

        A count regresses on any movement; a rate has to clear
        :data:`MATERIAL`. See the module docstring on why the two rules differ.
        """
        delta = self.delta
        if delta is None:
            return False
        wrong_way = delta < 0 if self.target.higher_is_better else delta > 0
        if not wrong_way:
            return False
        return True if self.target.counts else abs(delta) >= MATERIAL

    @property
    def newly_unmeasured(self) -> bool:
        """Whether the candidate stopped measuring something the baseline had.

        Worth its own flag. A change that quietly turns a metric off produces a
        report with fewer red numbers in it, and that is the one kind of
        improvement nobody should accept without reading why.
        """
        return self.baseline is not None and self.candidate is None


@dataclass(frozen=True, slots=True)
class LaneDelta:
    """One lane, before and after.

    Attributes:
        lane: Which lane.
        baseline: Its recorded row, or ``None`` where the baseline had no such
            lane.
        candidate: Its new row, or ``None``.
    """

    lane: str
    baseline: LaneResult | None
    candidate: LaneResult | None

    @property
    def completion_delta(self) -> float | None:
        """Task completion, candidate minus baseline."""
        return _delta(
            None if self.baseline is None else self.baseline.completion,
            None if self.candidate is None else self.candidate.completion,
        )

    @property
    def tool_selection_delta(self) -> float | None:
        """Lane selection, candidate minus baseline."""
        return _delta(
            None if self.baseline is None else self.baseline.tool_selection,
            None if self.candidate is None else self.candidate.tool_selection,
        )

    @property
    def regressed(self) -> bool:
        """Whether either rate moved materially down in this lane."""
        return any(
            delta is not None and delta <= -MATERIAL
            for delta in (self.completion_delta, self.tool_selection_delta)
        )

    def shape_deltas(self) -> Mapping[str, int]:
        """How many more or fewer rows took each failure shape.

        Where the *count* moved and the rate did not, this is the only place it
        shows. A lane whose accuracy held while ``no_tool`` turned into
        ``wrong_lane`` has changed in a way worth reading about: the same number
        made of different shapes is two different problems.
        """
        before = {} if self.baseline is None else dict(self.baseline.shapes)
        after = {} if self.candidate is None else dict(self.candidate.shapes)
        keys = sorted(set(before) | set(after))
        return {key: after.get(key, 0) - before.get(key, 0) for key in keys}


@dataclass(frozen=True, slots=True)
class RequirementDelta:
    """One PRD requirement, before and after.

    Attributes:
        requirement: The identifier.
        baseline: Its recorded row, or ``None``.
        candidate: Its new row, or ``None``.
    """

    requirement: str
    baseline: RequirementResult | None
    candidate: RequirementResult | None

    @property
    def delegated_to(self) -> str:
        """Where this is measured instead, or empty."""
        for side in (self.candidate, self.baseline):
            if side is not None and side.delegated_to:
                return side.delegated_to
        return ""

    @property
    def delta(self) -> float | None:
        """Pass rate over the covering cases, candidate minus baseline."""
        return _delta(
            None if self.baseline is None else self.baseline.rate,
            None if self.candidate is None else self.candidate.rate,
        )

    @property
    def regressed(self) -> bool:
        """Whether a requirement that was passing stopped.

        Any movement down counts, without :data:`MATERIAL`. A requirement is
        usually covered by one or two cases, so a rate here does not have the
        resolution a threshold assumes -- and a requirement that lost its only
        passing case is not noise whatever the arithmetic says.
        """
        delta = self.delta
        return delta is not None and delta < 0


@dataclass(frozen=True, slots=True)
class Comparison:
    """Two experiments, subtracted three ways.

    Attributes:
        baseline: What was recorded.
        candidate: What is being proposed.
        metrics: One per :data:`~chip_chat.eval.experiment.results.TARGETS`.
        lanes: One per lane present on either side.
        requirements: One per requirement present on either side.
    """

    baseline: ExperimentResult
    candidate: ExperimentResult
    metrics: tuple[MetricDelta, ...]
    lanes: tuple[LaneDelta, ...]
    requirements: tuple[RequirementDelta, ...]

    @property
    def comparable(self) -> bool:
        """Whether both sides scored the same dataset version.

        The one condition that makes a delta meaningless rather than merely
        qualified: two runs over different rows produce a difference that is
        partly the change and partly the rows, and nothing in the arithmetic can
        separate them.
        """
        return self.baseline.dataset_version == self.candidate.dataset_version

    @property
    def stated(self) -> bool:
        """Whether both sides said which lanes they had wired.

        The one condition this module *refuses* on rather than warns about, and
        the reason it is different in kind from every entry in
        :attr:`warnings`. A warning describes a difference the reader can then
        weigh -- a different judge, a different source, older rows. An unstated
        lane configuration is not a difference; it is the absence of the
        information a reader would need to know whether there is one. Two runs
        that both say ``none`` are comparable and two that say ``none`` and
        ``account+personalization`` are comparable-with-a-warning, because in
        both cases the reader can see it. A file that says nothing offers a
        delta that looks exactly like a model improvement and may be a lane
        coming up, and there is nothing in the document that would let anybody
        tell. So :func:`~chip_chat.eval.experiment.report.render_comparison`
        prints a refusal instead of the tables, and the CLI exits non-zero.

        This is the discipline :mod:`chip_chat.eval.retrieval.report` keeps for
        an arm whose vector half was dropped, applied to the axis that turned
        out to matter more.
        """
        return stated(self.baseline.wiring) and stated(self.candidate.wiring)

    @property
    def unstated_sides(self) -> tuple[str, ...]:
        """Which sides did not say, named, for the refusal to print."""
        sides = []
        if not stated(self.baseline.wiring):
            sides.append(f"baseline {self.baseline.experiment!r}")
        if not stated(self.candidate.wiring):
            sides.append(f"candidate {self.candidate.experiment!r}")
        return tuple(sides)

    @property
    def warnings(self) -> tuple[str, ...]:
        """Everything else that differs between the two runs, in prose."""
        notes: list[str] = []
        if self.stated and self.baseline.wiring != self.candidate.wiring:
            notes.append(
                f"different lanes wired: {self.baseline.wiring} then "
                f"{self.candidate.wiring}. A tool is offered to the model only "
                "when something can answer it, so a lane that came up moved its "
                "rows from unscoreable to scored, and part of every delta below "
                "is the deployment rather than the change."
            )
        if not self.comparable:
            notes.append(
                f"different dataset versions: {self.baseline.dataset_version} then "
                f"{self.candidate.dataset_version}. Part of every delta below is "
                "the rows rather than the change."
            )
        if self.baseline.source != self.candidate.source:
            notes.append(
                f"different sources: {self.baseline.source!r} then "
                f"{self.candidate.source!r}."
            )
        if self.baseline.judge != self.candidate.judge:
            notes.append(
                f"different judges: {self.baseline.judge or 'none'} then "
                f"{self.candidate.judge or 'none'}. A groundedness delta across "
                "two judges is partly a delta between judges."
            )
        if set(self.baseline.inert_axes) != set(self.candidate.inert_axes):
            notes.append(
                f"different inert axes: {list(self.baseline.inert_axes)} then "
                f"{list(self.candidate.inert_axes)}."
            )
        if self.baseline.fingerprint == self.candidate.fingerprint:
            notes.append(
                "both sides carry the same configuration fingerprint, so this "
                "comparison is measuring run-to-run variance rather than a change."
                if self.baseline.wiring == self.candidate.wiring
                else "both sides carry the same configuration fingerprint and "
                "different lanes, so this comparison is measuring the wiring "
                "rather than the configuration. That is a real measurement and "
                "it is the difference between what the model can do and what "
                "the deployment lets it do."
            )
        return tuple(notes)

    @property
    def regressions(self) -> tuple[str, ...]:
        """Everything that moved the wrong way, named, in reading order.

        Assembled from the lane and requirement breakdowns as well as from the
        headline metrics, which is the whole argument of this module: an
        aggregate that improved while a lane fell is a regression, and only the
        breakdown can say so.
        """
        found: list[str] = []
        for metric in self.metrics:
            if metric.regressed:
                found.append(
                    f"{metric.target.label}: {_show(metric.baseline, metric.target)} "
                    f"→ {_show(metric.candidate, metric.target)}"
                )
            elif metric.newly_unmeasured and not delegated(metric.target.metric):
                found.append(f"{metric.target.label}: measured before, unmeasured now")
        for lane in self.lanes:
            if lane.regressed:
                found.append(
                    f"lane {lane.lane}: completion {_rate(lane.completion_delta)}, "
                    f"tool selection {_rate(lane.tool_selection_delta)}"
                )
        for requirement in self.requirements:
            if requirement.regressed:
                found.append(
                    f"requirement {requirement.requirement}: {_rate(requirement.delta)}"
                )
        return tuple(found)

    @property
    def improvements(self) -> tuple[str, ...]:
        """Every headline metric that moved materially the right way."""
        return tuple(
            f"{metric.target.label}: {_show(metric.baseline, metric.target)} → "
            f"{_show(metric.candidate, metric.target)}"
            for metric in self.metrics
            if metric.improved
            and (metric.target.counts or abs(metric.delta or 0) >= MATERIAL)
        )

    @property
    def verdict(self) -> str:
        """One sentence a reader can act on."""
        if self.regressions:
            return (
                f"{len(self.regressions)} regression(s). "
                "Read the breakdowns before the headline."
            )
        if self.improvements:
            return f"{len(self.improvements)} improvement(s), no regression."
        return "Nothing moved materially in either direction."


def compare(baseline: ExperimentResult, candidate: ExperimentResult) -> Comparison:
    """Subtract two recorded results.

    Args:
        baseline: What was recorded, and what the launch criteria are checked
            against.
        candidate: What is being proposed.

    Returns:
        The comparison. Never raises on a mismatch -- see
        :attr:`Comparison.warnings` for why the mismatches are reported rather
        than refused.
    """
    return Comparison(
        baseline=baseline,
        candidate=candidate,
        metrics=tuple(
            MetricDelta(
                target=target,
                baseline=baseline.metric(target.metric).value,
                candidate=candidate.metric(target.metric).value,
            )
            for target in TARGETS
        ),
        lanes=_lane_deltas(baseline.lanes, candidate.lanes),
        requirements=_requirement_deltas(baseline.requirements, candidate.requirements),
    )


def _lane_deltas(
    baseline: Sequence[LaneResult], candidate: Sequence[LaneResult]
) -> tuple[LaneDelta, ...]:
    before = {lane.lane: lane for lane in baseline}
    after = {lane.lane: lane for lane in candidate}
    order = [lane.lane for lane in candidate]
    order += [lane.lane for lane in baseline if lane.lane not in after]
    return tuple(
        LaneDelta(lane=name, baseline=before.get(name), candidate=after.get(name))
        for name in order
    )


def _requirement_deltas(
    baseline: Sequence[RequirementResult], candidate: Sequence[RequirementResult]
) -> tuple[RequirementDelta, ...]:
    before = {item.requirement: item for item in baseline}
    after = {item.requirement: item for item in candidate}
    order = [item.requirement for item in candidate]
    order += [item.requirement for item in baseline if item.requirement not in after]
    return tuple(
        RequirementDelta(
            requirement=name, baseline=before.get(name), candidate=after.get(name)
        )
        for name in order
    )


def _delta(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None:
        return None
    return candidate - baseline


def _show(value: float | None, target: Target) -> str:
    """One value, as its own kind of number."""
    if value is None:
        return "--"
    return f"{value:.0f}" if target.counts else f"{value:.1%}"


def _rate(delta: float | None) -> str:
    """One rate delta, signed, or a dash where it could not be taken."""
    return "--" if delta is None else f"{delta:+.1%}"
