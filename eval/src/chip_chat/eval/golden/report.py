"""One run, written down: coverage, the per-lane table, and what was not measured.

Markdown, because a baseline is a document somebody reads in six months rather
than a dictionary somebody parses. Four properties are deliberate, and each is a
way an evaluation report can mislead a reader who is doing nothing wrong.

**Coverage is printed above the scores.** A set that is missing a lane produces a
fine average and a false conclusion, and the reader has to meet that before they
meet the number. The labeled photo set's report does the same thing for the same
reason.

**Unscored has its own column, in every table.** A run against a deployment that
cannot report citations is not a run with poor groundedness -- it is a run in
which groundedness was not a question that was asked. Folding the two together
is the single most flattering thing this report could do, so it does not.

**A rate that does not exist prints as an em dash.** Never as zero, never as
100%. An empty lane has no pass rate, and a nought in that cell reads as a lane
that failed everything.

**The two gates are stated in words, not scored.** PRD section 05 makes them pass
or fail: *"not 'few' -- zero"*. A percentage beside them would invite somebody to
read 99% as nearly passing.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from chip_chat.eval.golden.cases import JUDGED, GoldenSet
from chip_chat.eval.golden.coverage import Coverage, coverage
from chip_chat.eval.golden.run import Observation, Signal
from chip_chat.eval.golden.scoring import (
    COMPLETION_TARGET,
    TOOL_SELECTION_TARGET,
    CaseResult,
    LaneScore,
    Scores,
    score,
)

__all__ = ["Report", "build_report", "render"]

_EM_DASH = "--"


@dataclass(frozen=True, slots=True)
class Report:
    """Everything a baseline has to say, before it is a string.

    Attributes:
        deployment: What answered. Configuration, so that a report from two
            months ago says what it was measuring.
        catalog_version: The catalogue build the menu terms were checked
            against, or ``None`` where they were not checked. Two reports from
            different builds are not comparable and this is the only way to
            know.
        judged: Whether a judge was supplied. With none, every check in
            :data:`~chip_chat.eval.golden.cases.JUDGED` is unscored, and a
            reader has to be told that once rather than infer it from a column
            of dashes.
        signals: What the deployment could report about a turn.
        coverage: Whether the set is the set #29 asked for.
        scores: What the run produced.
        source: Which manifest was run.
    """

    deployment: str
    catalog_version: str | None
    judged: bool
    signals: frozenset[Signal]
    coverage: Coverage
    scores: Scores
    source: str


def build_report(
    golden: GoldenSet,
    observations: Sequence[Observation],
    *,
    deployment: str,
    catalog_version: str | None = None,
    judge_name: str | None = None,
) -> Report:
    """Score a run and assemble everything the document needs.

    Args:
        golden: The set that was run.
        observations: What came back.
        deployment: What answered.
        catalog_version: The catalogue build the terms were checked against.
        judge_name: The judge, where one was used. ``None`` is the state #29
            ships in and the report says so out loud.

    Returns:
        The report.
    """
    return Report(
        deployment=deployment,
        catalog_version=catalog_version,
        judged=judge_name is not None,
        signals=_signals(observations),
        coverage=coverage(golden),
        scores=score(golden, observations),
        source=str(golden.source),
    )


def render(report: Report) -> str:
    """Render a report as Markdown.

    Args:
        report: What to render.

    Returns:
        The document, ending in a newline.
    """
    lines: list[str] = [
        "# Golden set baseline",
        "",
        f"- **Deployment** — {report.deployment}",
        f"- **Set** — `{report.source}`, {report.coverage.cases} cases",
        f"- **Catalogue build** — {report.catalog_version or 'not checked'}",
        f"- **Judge** — {'supplied' if report.judged else 'none; see below'}",
        "- **Signals reported** — "
        + (", ".join(sorted(signal.value for signal in report.signals)) or "none"),
        "",
    ]
    lines.extend(_coverage_section(report.coverage))
    lines.extend(_headline_section(report.scores))
    lines.extend(_lane_section(report.scores))
    lines.extend(_unmeasured_section(report))
    lines.extend(_failure_section(report.scores))
    return "\n".join(lines) + "\n"


def _coverage_section(cover: Coverage) -> list[str]:
    """Coverage, above the scores. See the module docstring."""
    lines = ["## Coverage", ""]
    lines.append(
        f"{len(cover.covered)} requirements covered by a case, "
        f"{len(cover.delegated)} measured elsewhere, "
        f"{len(cover.uncovered)} uncovered."
    )
    lines.append("")
    if cover.uncovered:
        lines.append("**Uncovered requirements** — #29's first acceptance criterion:")
        lines.append("")
        lines.extend(f"- `{item.id}` {item.text}" for item in cover.uncovered)
        lines.append("")
    missing_tools = cover.tools_without_a_case
    if missing_tools:
        lines.append(
            "**Tools no case expects** — "
            + ", ".join(f"`{tool.value}`" for tool in missing_tools)
        )
        lines.append("")
    for shape, ids in cover.unmet:
        lines.append(
            f"- MISSING {shape.name}: {len(ids)}/{shape.minimum} ({shape.source})"
        )
    if cover.unmet:
        lines.append("")
    if cover.complete:
        lines.append(
            "Every requirement covered, every tool exercised, every shape clause met."
        )
        lines.append("")
    return lines


def _headline_section(scores: Scores) -> list[str]:
    """The PRD's own table, with this run's numbers in it."""
    gates = scores.gates_pass
    gate_text = {None: "not measured", True: "pass", False: "**FAIL**"}[gates]
    return [
        "## Against the PRD's targets",
        "",
        "| Metric | Target | This run |",
        "| --- | --- | --- |",
        f"| Task completion | ≥ {COMPLETION_TARGET:.0%} | {_rate(scores.completion)} |",
        f"| Tool-selection accuracy | ≥ {TOOL_SELECTION_TARGET:.0%} | "
        f"{_rate(scores.tool_selection)} |",
        f"| Menu claims without a citation | 0 | {_count(scores.uncited_claims)} |",
        f"| Writes without confirmation | 0 | {_count(scores.unconfirmed_writes)} |",
        f"| Both launch gates | pass | {gate_text} |",
        "",
        f"{scores.passed} passed, {len(scores.failures())} failed, "
        f"{scores.unscored} unscored, of {scores.total} run.",
        "",
    ]


def _lane_section(scores: Scores) -> list[str]:
    """#29's third acceptance criterion: per-lane pass rates."""
    lines = [
        "## Per lane",
        "",
        "| Lane | Cases | Passed | Failed | Unscored | Pass rate | Tool selection |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(_lane_row(lane) for lane in scores.lanes if lane.total)
    lines.append("")
    return lines


def _lane_row(lane: LaneScore) -> str:
    return (
        f"| {lane.lane.value} | {lane.total} | {lane.passed} | {lane.failed} "
        f"| {lane.unscored} | {_rate(lane.pass_rate)} "
        f"| {_rate(lane.tool_selection)} |"
    )


def _unmeasured_section(report: Report) -> list[str]:
    """What this run could not see, and why. Never folded into the failures."""
    lines = ["## What this run did not measure", ""]
    absent = sorted(signal.value for signal in Signal if signal not in report.signals)
    if absent:
        lines.append(
            "The deployment does not report: "
            + ", ".join(f"`{name}`" for name in absent)
            + ". Every check needing one of those is unscored rather than failed."
        )
        lines.append("")
    if not report.judged:
        lines.append(
            "No judge was supplied, so these checks are unscored on every case "
            "carrying them: "
            + ", ".join(f"`{check.value}`" for check in sorted(JUDGED))
            + ". They are judgements about meaning rather than properties of a "
            "payload — see `chip_chat.eval.golden.run.Judge`."
        )
        lines.append("")
    if report.scores.errors:
        lines.append(
            "Cases the deployment could not answer at all: "
            + ", ".join(f"`{case_id}`" for case_id in report.scores.errors)
            + "."
        )
        lines.append("")
    return lines


def _failure_section(scores: Scores) -> list[str]:
    """Every failure by name. The next action after reading this is opening one."""
    failures = scores.failures()
    if not failures:
        return ["## Failures", "", "None.", ""]
    lines = [
        "## Failures",
        "",
        "| Case | Lane | Failed | Why the case exists |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(_failure_row(result) for result in failures)
    lines.append("")
    return lines


def _failure_row(result: CaseResult) -> str:
    return (
        f"| `{result.case.case_id}` | {result.case.lane.value} "
        f"| {', '.join(result.failed_checks)} | {result.case.why} |"
    )


def _signals(observations: Sequence[Observation]) -> frozenset[Signal]:
    """What the deployment reported, read off the run rather than off the object.

    A run scored later from a file has no deployment to ask, and the
    observations carry it. Empty for an empty run, which renders as "none" and
    is the truth about a run that observed nothing.
    """
    reported: set[Signal] = set()
    for observation in observations:
        reported |= observation.reports
    return frozenset(reported)


def _rate(value: float | None) -> str:
    """A percentage, or an em dash where there is no number. Never a zero."""
    return _EM_DASH if value is None else f"{value:.0%}"


def _count(value: int | None) -> str:
    """A count, or an em dash. The same rule, and it matters more here.

    A zero in the uncited-claims cell is the target being met. A zero standing
    in for "nobody looked" would be the most misleading cell in the document.
    """
    return _EM_DASH if value is None else str(value)
