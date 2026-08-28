"""What an experiment produced, in a form two experiments can be compared in.

#73 asks for *results comparable across experiments, with per-lane and
per-requirement breakdowns rather than one aggregate number that hides a
regression in one lane behind an improvement in another*. Both halves of that
sentence are load-bearing and they pull in opposite directions, which is why
this module exists rather than the runner simply printing the three scorers'
objects.

**Comparable means serialisable.** :class:`~chip_chat.eval.golden.scoring.Scores`,
:class:`~chip_chat.eval.trajectory.scoring.TrajectoryScores` and
:class:`~chip_chat.eval.grounding.scoring.GroundingScores` are rich, live objects
holding the cases and the trajectories they were computed from. That is right for
a report and useless for a comparison: a candidate scored today has to be
comparable to a baseline recorded three weeks ago, and nothing but a file
survives three weeks. So :class:`ExperimentResult` is the flattened form,
computed once from the three scorers, written as JSON, and read back with no
dependence on the objects that produced it. :mod:`chip_chat.eval.experiment.
compare` takes two of *these*, never two runs.

**Per-lane and per-requirement are different questions, and the second one has
no home anywhere else.** The three scorers all break down by lane, because a lane
is where the architecture is. Nothing breaks down by *requirement*, because a
requirement is where the product is, and the join -- a case names the PRD
identifiers it covers -- lives on the golden case. One case can cover two
requirements and one requirement can be covered by six cases, so a requirement's
verdict is an aggregate over a different partition of the same rows, and adding
the columns up two ways is exactly what makes a regression visible that an
aggregate would hide.

**A delegated requirement is not a scored requirement and is not a gap.**
:data:`~chip_chat.eval.golden.requirements.DELEGATIONS` already draws that
distinction for coverage, and it holds here for the same reason: fold the vision
lane's delegations into *failed* and the experiment reports a regression every
time; fold them into *passed* and the experiment reports a product that is
measured where it is not.

**Nothing here computes a rate over the part somebody measured.** Every rate
carries the count it could not score beside it, and every count that is a launch
gate stays a count. That is the register the four evals below this one already
keep, and an experiment harness is the last place to start averaging gates.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from chip_chat.eval.experiment.configurations import ExperimentConfiguration
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.requirements import DELEGATIONS, REQUIREMENTS
from chip_chat.eval.golden.scoring import Scores, Verdict
from chip_chat.eval.grounding.scoring import GroundingScores
from chip_chat.eval.grounding.verdicts import Finding
from chip_chat.eval.trajectory.scoring import TrajectoryScores
from chip_chat.eval.wiring import UNSTATED

__all__ = [
    "DEFAULT_RESULTS_DIR",
    "TARGETS",
    "ExperimentResult",
    "LaneResult",
    "Metric",
    "RequirementResult",
    "ResultError",
    "Target",
    "build_result",
    "load_result",
    "write_result",
]

DEFAULT_RESULTS_DIR: Final = Path("eval/experiments/results")
"""Where recorded results live, one JSON file per experiment name."""

_SCHEMA: Final = 1
"""The recorded shape. Bumped when a column is added, so an old file loaded by
new code fails at the door rather than in the middle of a comparison."""


class ResultError(ValueError):
    """A recorded result that cannot be read as one."""


@dataclass(frozen=True, slots=True)
class Target:
    """One row of PRD section 05's table, as something a result is held to.

    Attributes:
        metric: The key a :class:`Metric` carries.
        label: How the table prints it.
        target: The number.
        higher_is_better: ``True`` for a rate to clear, ``False`` for a count
            to stay under. The two are not interchangeable and a comparison
            that treated them as one would report a rising uncited-claim count
            as an improvement.
        counts: Whether this is a count rather than a rate. A count is never
            averaged and never rendered as a percentage; PRD section 05 is
            explicit that zero means zero.
        source: Where the number comes from, so nobody negotiates with it.
    """

    metric: str
    label: str
    target: float
    higher_is_better: bool
    counts: bool
    source: str


TARGETS: Final[tuple[Target, ...]] = (
    Target(
        "completion",
        "Task completion on the golden set",
        0.85,
        True,
        False,
        "PRD §05",
    ),
    Target("tool_selection", "Tool-selection accuracy", 0.95, True, False, "PRD §05"),
    Target(
        "groundedness",
        "Groundedness of food and policy claims",
        0.95,
        True,
        False,
        "PRD §05",
    ),
    Target(
        "uncited_claims",
        "Menu claims made without a citation",
        0.0,
        False,
        True,
        "PRD §05, K2",
    ),
    Target(
        "photo_f1",
        "Photo → order, component-level F1",
        0.80,
        True,
        False,
        "PRD §05",
    ),
)
"""#73's table, transcribed. Five rows, and the fifth is measured elsewhere.

``photo_f1`` is here and always unmeasured by this harness, deliberately.
``eval/README.md`` draws the line: the labeled photo set runs the vision lane
directly, because one whole-turn case cannot say whether the salsa was right. A
table that dropped the row would let an experiment report five green metrics
while the vision lane went unscored; a table that carried it as zero would report
a failure. It carries it as *delegated*, which is what it is.
"""

_DELEGATED_METRICS: Final[frozenset[str]] = frozenset({"photo_f1"})


@dataclass(frozen=True, slots=True)
class Metric:
    """One number, and what it is a number over.

    Attributes:
        metric: Which :class:`Target` this answers.
        value: The measurement, or ``None`` where nothing could be measured.
            ``None`` is not zero and is never rendered as one.
        scored: Rows the metric could be computed over.
        asked: Rows the metric applies to at all. The gap between the two is
            the wiring rather than the product.
        note: Why ``value`` is ``None``, or what qualifies it. Empty where the
            number stands unqualified.
    """

    metric: str
    value: float | None = None
    scored: int = 0
    asked: int = 0
    note: str = ""

    @property
    def measured(self) -> bool:
        """Whether there is a number here at all."""
        return self.value is not None

    def meets(self, target: Target) -> bool | None:
        """Whether this clears its target, or ``None`` while unmeasured.

        A target nobody measured has not been met. That is the same third value
        :attr:`~chip_chat.eval.golden.scoring.Scores.gates_pass` keeps, and for
        the same reason: PRD section 12 makes several of these blocking, and
        ``False`` would say the product failed where the truth is that nothing
        looked.
        """
        if self.value is None:
            return None
        if target.higher_is_better:
            return self.value >= target.target
        return self.value <= target.target

    def as_json(self) -> Mapping[str, Any]:
        """The recorded form."""
        return {
            "metric": self.metric,
            "value": self.value,
            "scored": self.scored,
            "asked": self.asked,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class LaneResult:
    """One lane, in all three readings of the same rows.

    Attributes:
        lane: Which lane, :attr:`~chip_chat.eval.golden.lanes.Lane.NONE`
            included -- *reach for nothing* is an answer routing can be wrong
            about.
        cases: Rows in this lane.
        passed: Rows where every check on the case passed.
        failed: Rows where at least one check failed.
        unscored: Rows where nothing failed and something was not measured.
        completion: Passes over every row in the lane, unscored included.
        tool_selection: Correct lane selection over the trajectories that could
            be read.
        shapes: How many rows took each of #74's four failure shapes.
        grounded_scored: Rows the groundedness judge settled.
        grounded_failed: Of those, the ones it settled against.
        uncited: Uncited claims in this lane, or ``None`` where citations were
            not reported at all.
        over_refusals: Rows that declined where the corpus plainly had it.
        under_refusals: Rows that answered where it does not support one.
    """

    lane: str
    cases: int
    passed: int
    failed: int
    unscored: int
    completion: float | None
    tool_selection: float | None
    shapes: Mapping[str, int] = field(default_factory=dict)
    grounded_scored: int = 0
    grounded_failed: int = 0
    uncited: int | None = None
    over_refusals: int = 0
    under_refusals: int = 0

    def as_json(self) -> Mapping[str, Any]:
        """The recorded form."""
        return {
            "lane": self.lane,
            "cases": self.cases,
            "passed": self.passed,
            "failed": self.failed,
            "unscored": self.unscored,
            "completion": self.completion,
            "tool_selection": self.tool_selection,
            "shapes": dict(sorted(self.shapes.items())),
            "grounded_scored": self.grounded_scored,
            "grounded_failed": self.grounded_failed,
            "uncited": self.uncited,
            "over_refusals": self.over_refusals,
            "under_refusals": self.under_refusals,
        }


@dataclass(frozen=True, slots=True)
class RequirementResult:
    """One PRD requirement, and what the run said about it.

    Attributes:
        requirement: The identifier, e.g. ``K3``.
        lane: The lane it belongs to.
        cases: Golden cases naming it. Zero on a delegated requirement.
        passed: Of those, cases where every check passed.
        failed: Cases where at least one check failed.
        unscored: Cases where nothing failed and something was not measured.
        delegated_to: Where it is measured instead, or empty. A requirement
            with a target here is neither covered nor a gap; see the module
            docstring.
    """

    requirement: str
    lane: str
    cases: int = 0
    passed: int = 0
    failed: int = 0
    unscored: int = 0
    delegated_to: str = ""

    @property
    def rate(self) -> float | None:
        """Passes over the cases naming this requirement, or ``None``."""
        return None if not self.cases else self.passed / self.cases

    def as_json(self) -> Mapping[str, Any]:
        """The recorded form."""
        return {
            "requirement": self.requirement,
            "lane": self.lane,
            "cases": self.cases,
            "passed": self.passed,
            "failed": self.failed,
            "unscored": self.unscored,
            "delegated_to": self.delegated_to,
        }


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    """One named experiment, flattened into something a file can hold.

    Attributes:
        experiment: The configuration's name.
        fingerprint: The configuration's fingerprint. Two results with the same
            pair of this and :attr:`dataset_version` scored the same thing.
        configuration: The resolved configuration, as recorded JSON.
        prompt_version: ``v1+3f2a1b9c8d7e``. Carried out of the configuration
            so a reader does not have to reconstruct it.
        dataset: The dataset's name.
        dataset_version: Its content fingerprint.
        source: What answered the rows.
        wiring: Which lanes were wired, as
            :attr:`chip_chat.eval.wiring.Wiring.label` spells it --
            ``account+personalization``, or ``none`` for the unwired slice.
            :data:`~chip_chat.eval.wiring.UNSTATED` where the run did not say,
            which is every file recorded before this column existed and is not
            the same thing as ``none``. A comparison refuses the first and
            allows the second; see :attr:`~chip_chat.eval.experiment.compare.
            Comparison.stated`.
        judge: What settled the judged findings, or empty.
        judge_tokens: What the judging cost. #76 makes this a budget line.
        inert_axes: Configuration axes this run recorded but could not apply.
        ran_at: When, in ISO-8601 UTC to the second.
        rows: Rows run.
        metrics: One per :data:`TARGETS`, in that order.
        lanes: One per lane, in :class:`~chip_chat.eval.golden.lanes.Lane` order.
        requirements: One per PRD requirement, in register order.
        caveat: What these numbers are worth, in prose. Printed above them.
    """

    experiment: str
    fingerprint: str
    configuration: Mapping[str, Any]
    prompt_version: str
    dataset: str
    dataset_version: str
    source: str
    wiring: str = UNSTATED
    judge: str = ""
    judge_tokens: int = 0
    inert_axes: tuple[str, ...] = ()
    ran_at: str = ""
    rows: int = 0
    metrics: tuple[Metric, ...] = ()
    lanes: tuple[LaneResult, ...] = ()
    requirements: tuple[RequirementResult, ...] = ()
    caveat: str = ""

    def metric(self, name: str) -> Metric:
        """One metric by name.

        Args:
            name: The :class:`Target`'s ``metric``.

        Returns:
            The metric, or an unmeasured one where this result does not carry
            it -- an older file missing a metric a newer target names is a gap
            in what was recorded, not a failure.
        """
        for measurement in self.metrics:
            if measurement.metric == name:
                return measurement
        return Metric(metric=name, note="not recorded by this run")

    @property
    def targets_met(self) -> bool | None:
        """Whether every target this run measured was met.

        ``None`` while any target is unmeasured, because PRD section 12 makes
        several of them launch criteria and a criterion nobody measured has not
        passed. A harness that returned ``True`` on four green metrics and one
        blank would be the most flattering possible way to write *not measured*.
        """
        verdicts = [self.metric(target.metric).meets(target) for target in TARGETS]
        if any(verdict is None for verdict in verdicts):
            return None
        return all(verdicts)

    def as_json(self) -> Mapping[str, Any]:
        """The recorded form. Sorted, so two files diff cleanly."""
        return {
            "schema": _SCHEMA,
            "experiment": self.experiment,
            "fingerprint": self.fingerprint,
            "configuration": dict(self.configuration),
            "prompt_version": self.prompt_version,
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "source": self.source,
            "wiring": self.wiring,
            "judge": self.judge,
            "judge_tokens": self.judge_tokens,
            "inert_axes": list(self.inert_axes),
            "ran_at": self.ran_at,
            "rows": self.rows,
            "caveat": self.caveat,
            "metrics": [dict(metric.as_json()) for metric in self.metrics],
            "lanes": [dict(lane.as_json()) for lane in self.lanes],
            "requirements": [dict(item.as_json()) for item in self.requirements],
        }


def build_result(
    configuration: ExperimentConfiguration,
    *,
    dataset: str,
    dataset_version: str,
    source: str,
    golden_scores: Scores,
    trajectory_scores: TrajectoryScores,
    grounding_scores: GroundingScores,
    wiring: str = UNSTATED,
    judge: str = "",
    judge_tokens: int = 0,
    inert_axes: Sequence[str] = (),
    caveat: str = "",
    now: datetime | None = None,
) -> ExperimentResult:
    """Flatten one run into the recorded form.

    Args:
        configuration: The arm that ran, already resolved against the
            environment. An unresolved one would record *whatever was
            configured* as the deployment, which is not a value.
        dataset: The dataset's name.
        dataset_version: Its content fingerprint.
        source: What answered the rows.
        golden_scores: Task completion and the per-case verdicts.
        trajectory_scores: Lane selection and the four failure shapes.
        grounding_scores: Groundedness, citations and both refusal directions.
        wiring: Which lanes were wired. See :attr:`ExperimentResult.wiring`.
        judge: What settled the judged findings, or empty.
        judge_tokens: What that cost.
        inert_axes: Axes recorded but not applied.
        caveat: What these numbers are worth.
        now: The clock, for a test that wants a fixed timestamp.

    Returns:
        The result.
    """
    stamp = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
    return ExperimentResult(
        experiment=configuration.name,
        fingerprint=configuration.fingerprint,
        configuration=configuration.as_json(),
        prompt_version=configuration.prompt().version,
        dataset=dataset,
        dataset_version=dataset_version,
        source=source,
        wiring=wiring,
        judge=judge,
        judge_tokens=judge_tokens,
        inert_axes=tuple(inert_axes),
        ran_at=stamp,
        rows=golden_scores.total,
        metrics=_metrics(golden_scores, trajectory_scores, grounding_scores),
        lanes=_lanes(golden_scores, trajectory_scores, grounding_scores),
        requirements=_requirements(golden_scores),
        caveat=caveat,
    )


def _metrics(
    golden: Scores, trajectory: TrajectoryScores, grounding: GroundingScores
) -> tuple[Metric, ...]:
    """The five headline numbers, each with what it is a number over.

    Tool-selection accuracy comes from the trajectory eval rather than from the
    golden set, and the two are not the same number. The golden set scores
    routing off the loop's own messages; #74 scores it off the ``tool.*`` spans,
    which is what a backend, a dashboard and an online eval will read. Where
    they disagree, the span reading is the one anybody outside this process can
    reproduce, so it is the one the table carries.
    """
    grounded_asked = grounding.asked(Finding.GROUNDED)
    grounded_scored = grounding.scored(Finding.GROUNDED)
    uncited = grounding.uncited_claims
    return (
        Metric(
            "completion",
            golden.completion,
            scored=golden.total - golden.unscored,
            asked=golden.total,
            note=("" if golden.total else "no case produced an observation"),
        ),
        Metric(
            "tool_selection",
            trajectory.tool_selection,
            scored=trajectory.scored,
            asked=trajectory.total,
            note=("" if trajectory.scored else "no trajectory could be read"),
        ),
        Metric(
            "groundedness",
            grounding.groundedness,
            scored=grounded_scored,
            asked=grounded_asked,
            note="" if grounded_scored else "no judge settled a row",
        ),
        Metric(
            "uncited_claims",
            None if uncited is None else float(uncited),
            scored=grounding.scored(Finding.CITED),
            asked=grounding.asked(Finding.CITED),
            note=("" if uncited is not None else "no source reported citations (cc-bap)"),
        ),
        Metric(
            "photo_f1",
            None,
            note=(
                "delegated to eval/photos (#56); one whole-turn case cannot say "
                "whether the salsa was right"
            ),
        ),
    )


def _lanes(
    golden: Scores, trajectory: TrajectoryScores, grounding: GroundingScores
) -> tuple[LaneResult, ...]:
    """One row per lane, joining the three readings on the lane they share."""
    by_lane_trajectory = {lane.lane: lane for lane in trajectory.lanes}
    results: list[LaneResult] = []
    for lane_scores in golden.lanes:
        lane = lane_scores.lane
        moves = by_lane_trajectory.get(lane)
        grounded_scored, grounded_failed, uncited, over, under = _grounding_by_lane(
            grounding, lane
        )
        results.append(
            LaneResult(
                lane=lane.value,
                cases=lane_scores.total,
                passed=lane_scores.passed,
                failed=lane_scores.failed,
                unscored=lane_scores.unscored,
                completion=lane_scores.pass_rate,
                tool_selection=None if moves is None else moves.tool_selection,
                shapes=(
                    {}
                    if moves is None
                    else {shape.value: count for shape, count in moves.shapes.items()}
                ),
                grounded_scored=grounded_scored,
                grounded_failed=grounded_failed,
                uncited=uncited,
                over_refusals=over,
                under_refusals=under,
            )
        )
    return tuple(results)


def _grounding_by_lane(
    grounding: GroundingScores, lane: Lane
) -> tuple[int, int, int | None, int, int]:
    """Groundedness, citations and both refusal directions, for one lane.

    :class:`~chip_chat.eval.grounding.scoring.GroundingScores` partitions by
    *category* -- allergen and dietary against everything else -- because that is
    the split #75 is about. The lane split is this module's, so it is computed
    here from the judgements rather than added to that class: an experiment
    needing a different partition of the same rows is not a reason to give the
    grounding eval a second one.
    """
    from chip_chat.eval.grounding.verdicts import Refusal
    from chip_chat.eval.grounding.verdicts import Verdict as GroundingVerdict

    rows = [j for j in grounding.judgements if j.question.lane is lane]
    grounded = [j for j in rows if j.verdicts.get(Finding.GROUNDED)]
    scored = sum(
        1
        for j in grounded
        if j.verdicts[Finding.GROUNDED] in (GroundingVerdict.PASS, GroundingVerdict.FAIL)
    )
    failed = sum(
        1 for j in grounded if j.verdicts[Finding.GROUNDED] is GroundingVerdict.FAIL
    )
    cited_scored = [
        j
        for j in rows
        if j.verdicts.get(Finding.CITED) in (GroundingVerdict.PASS, GroundingVerdict.FAIL)
    ]
    uncited = (
        None
        if not cited_scored
        else sum(
            1 for j in cited_scored if j.verdicts[Finding.CITED] is GroundingVerdict.FAIL
        )
    )
    over = sum(1 for j in rows if j.refusal is Refusal.OVER_REFUSAL)
    under = sum(1 for j in rows if j.refusal is Refusal.UNDER_REFUSAL)
    return scored, failed, uncited, over, under


def _requirements(golden: Scores) -> tuple[RequirementResult, ...]:
    """One row per PRD requirement, and the delegations beside them."""
    delegated = {item.requirement_id: item.target for item in DELEGATIONS}
    results: list[RequirementResult] = []
    for requirement in REQUIREMENTS:
        covering = [
            result
            for result in golden.results
            if requirement.id in result.case.requirements
        ]
        results.append(
            RequirementResult(
                requirement=requirement.id,
                lane=requirement.lane.value,
                cases=len(covering),
                passed=sum(1 for r in covering if r.verdict is Verdict.PASS),
                failed=sum(1 for r in covering if r.verdict is Verdict.FAIL),
                unscored=sum(1 for r in covering if r.verdict is Verdict.UNSCORED),
                delegated_to=delegated.get(requirement.id, ""),
            )
        )
    return tuple(results)


def write_result(result: ExperimentResult, path: Path) -> None:
    """Record a result where a later comparison can find it.

    Args:
        result: What to write.
        path: Where. Parent directories are created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(result.as_json()), indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")


def load_result(path: Path) -> ExperimentResult:
    """Read a recorded result back.

    Args:
        path: The file.

    Returns:
        The result.

    Raises:
        ResultError: If the file cannot be read, is not JSON, or was written by
            a different schema. A silently-tolerated schema change is a
            comparison of two things that were not measured the same way.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ResultError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ResultError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ResultError(f"{path} is not an object")
    if payload.get("schema") != _SCHEMA:
        raise ResultError(
            f"{path} was written by schema {payload.get('schema')!r}; this build "
            f"reads {_SCHEMA}. Re-run the experiment rather than comparing across "
            "shapes."
        )
    try:
        return ExperimentResult(
            experiment=str(payload["experiment"]),
            fingerprint=str(payload["fingerprint"]),
            configuration=dict(payload.get("configuration") or {}),
            prompt_version=str(payload.get("prompt_version", "")),
            dataset=str(payload.get("dataset", "")),
            dataset_version=str(payload.get("dataset_version", "")),
            source=str(payload.get("source", "")),
            # Absent on every file written before the column existed, and that
            # is exactly what UNSTATED means. Defaulting it to `none` would
            # claim those runs measured the unwired slice, which is probably
            # true and is not something a reader should have to take on trust.
            wiring=str(payload.get("wiring", UNSTATED)),
            judge=str(payload.get("judge", "")),
            judge_tokens=int(payload.get("judge_tokens", 0)),
            inert_axes=tuple(str(axis) for axis in payload.get("inert_axes", ())),
            ran_at=str(payload.get("ran_at", "")),
            rows=int(payload.get("rows", 0)),
            metrics=tuple(_metric(item) for item in payload.get("metrics", ())),
            lanes=tuple(_lane(item) for item in payload.get("lanes", ())),
            requirements=tuple(
                _requirement(item) for item in payload.get("requirements", ())
            ),
            caveat=str(payload.get("caveat", "")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ResultError(f"{path}: {error}") from error


def _metric(payload: Mapping[str, Any]) -> Metric:
    value = payload.get("value")
    return Metric(
        metric=str(payload["metric"]),
        value=None if value is None else float(value),
        scored=int(payload.get("scored", 0)),
        asked=int(payload.get("asked", 0)),
        note=str(payload.get("note", "")),
    )


def _lane(payload: Mapping[str, Any]) -> LaneResult:
    return LaneResult(
        lane=str(payload["lane"]),
        cases=int(payload.get("cases", 0)),
        passed=int(payload.get("passed", 0)),
        failed=int(payload.get("failed", 0)),
        unscored=int(payload.get("unscored", 0)),
        completion=_optional(payload.get("completion")),
        tool_selection=_optional(payload.get("tool_selection")),
        shapes={str(k): int(v) for k, v in (payload.get("shapes") or {}).items()},
        grounded_scored=int(payload.get("grounded_scored", 0)),
        grounded_failed=int(payload.get("grounded_failed", 0)),
        uncited=None if payload.get("uncited") is None else int(payload["uncited"]),
        over_refusals=int(payload.get("over_refusals", 0)),
        under_refusals=int(payload.get("under_refusals", 0)),
    )


def _requirement(payload: Mapping[str, Any]) -> RequirementResult:
    return RequirementResult(
        requirement=str(payload["requirement"]),
        lane=str(payload.get("lane", "")),
        cases=int(payload.get("cases", 0)),
        passed=int(payload.get("passed", 0)),
        failed=int(payload.get("failed", 0)),
        unscored=int(payload.get("unscored", 0)),
        delegated_to=str(payload.get("delegated_to", "")),
    )


def _optional(value: Any) -> float | None:
    return None if value is None else float(value)


def target(metric: str) -> Target:
    """Look a target up by metric name.

    Args:
        metric: The key.

    Returns:
        The target.

    Raises:
        KeyError: If nothing in :data:`TARGETS` carries that key.
    """
    for item in TARGETS:
        if item.metric == metric:
            return item
    raise KeyError(metric)


def delegated(metric: str) -> bool:
    """Whether this metric is measured somewhere other than an experiment."""
    return metric in _DELEGATED_METRICS
