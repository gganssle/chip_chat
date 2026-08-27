"""One trajectory run, written down: the metric, the lanes, and the four shapes.

Markdown, for the reason ``eval/golden/report.py`` gives: a baseline is a
document somebody reads in six months, not a dictionary somebody parses. Four
properties are deliberate here, and each is a way a report on *the* headline
metric could mislead a reader who is doing nothing wrong.

**The coverage caveats come before the table.** A lane with one row prints a
percentage that reads like every other percentage. #74 asks for the per-lane
breakdown *because a 95% aggregate can hide a vision lane at 60%*, and a thin
lane hides the same thing one level down.

**Unreadable traces are counted above the rates, with #103 named.** If the turn
arrived as two traces, every number below is over a subset nobody chose. That is
the dependency the bead behind #74 calls out by name, and burying it under the
table would let a green run be quoted while the propagation is broken.

**The shapes are printed with what they mean, not only with counts.** Four
numbers under a heading called *failures* would be read as four flavours of the
same thing. They are four different problems with four different owners, and the
report says which is which where the counts are.

**A rate that does not exist prints as an em dash.** Never as zero, never as
100%. An unscored lane has no accuracy, and a nought in that cell reads as a
lane that got everything wrong.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from chip_chat.eval.trajectory.coverage import RATE_NEEDS, Coverage, coverage
from chip_chat.eval.trajectory.expectations import QUERY_ARGUMENT, Expectation
from chip_chat.eval.trajectory.scoring import (
    TOOL_SELECTION_TARGET,
    LaneTrajectories,
    TrajectoryScores,
    score,
)
from chip_chat.eval.trajectory.shapes import FAILURE_SHAPES, Judgement, Shape
from chip_chat.eval.trajectory.trees import Trajectory

__all__ = ["SHAPE_MEANS", "Report", "build_report", "render"]

_EM_DASH = "--"

SHAPE_MEANS: Mapping[Shape, str] = {
    Shape.WRONG_LANE: (
        "chose, and chose the other thing. A tool-description problem before it "
        "is a prompt one -- `chip_chat.agent.surface` is where the lanes are "
        "separated, and `python -m chip_chat.agent.selection` is how a change "
        "there is measured."
    ),
    Shape.NO_TOOL: (
        "answered from what the model already knew. The quiet killer for "
        "groundedness: the prose reads fine and nothing in it is attached to "
        "anything. Also what a deployment produces when the tool was never "
        "registered, which a span tree cannot tell apart from a choice."
    ),
    Shape.EXTRA_TOOLS: (
        "reached the lane, and paid for more calls than the turn needed. A cost "
        "and latency finding rather than a correctness one; PRD section 05 asks "
        "for cost per conversation, and this is where it leaks."
    ),
    Shape.WRONG_QUERY: (
        "right lane, wrong ask. Only observable on the two tools that take the "
        "question as an argument, and only where the query drifted off the "
        "message entirely -- the subtle paraphrase needs a judge."
    ),
}
"""What each failure shape means, printed beside its count. See the module docstring."""


@dataclass(frozen=True, slots=True)
class Report:
    """Everything a trajectory baseline has to say, before it is a string.

    Attributes:
        source: What produced the traces.
        dataset: The dataset's name.
        version: Its version. The whole reason the rows come from a dataset:
            two scores are comparable only if this is the same string.
        caveat: What this run's numbers are and are not worth, in prose,
            rendered above everything else. Empty where there is nothing to
            say. A run against an oracle needs one badly -- a reader who
            arrives at the table without it will read a fixture's ceiling as a
            model's accuracy, which is the single most misleading thing this
            document could do.
        coverage: Whether the rows can support the numbers.
        scores: What the run produced.
    """

    source: str
    dataset: str
    version: str
    coverage: Coverage
    scores: TrajectoryScores
    caveat: str = ""


def build_report(
    rows: Sequence[Expectation],
    trajectories: Sequence[Trajectory],
    *,
    source: str,
    dataset: str,
    version: str,
    caveat: str = "",
) -> Report:
    """Score a run and assemble everything the document needs.

    Args:
        rows: The dataset rows that were run.
        trajectories: What came back.
        source: What produced the traces.
        dataset: The dataset's name.
        version: The dataset's version.
        caveat: What this run's numbers are worth. See :attr:`Report.caveat`.

    Returns:
        The report.
    """
    return Report(
        source=source,
        dataset=dataset,
        version=version,
        coverage=coverage(rows),
        scores=score(rows, trajectories),
        caveat=caveat,
    )


def render(report: Report) -> str:
    """Render a report as Markdown.

    Args:
        report: What to render.

    Returns:
        The document, ending in a newline.
    """
    lines: list[str] = [
        "# Trajectory and tool-selection baseline",
        "",
        f"- **Traces from** — {report.source}",
        f"- **Dataset** — {report.dataset} `{report.version}`, "
        f"{report.coverage.rows} rows that score routing",
        f"- **Target** — tool-selection accuracy ≥ {_percent(TOOL_SELECTION_TARGET)} "
        "(PRD §05)",
        "",
    ]
    if report.caveat:
        lines.extend(f"> {line}".rstrip() for line in report.caveat.splitlines())
        lines.append("")
    lines.extend(_coverage_section(report.coverage))
    lines.extend(_headline_section(report.scores))
    lines.extend(_lane_section(report.scores, report.coverage))
    lines.extend(_shape_section(report.scores))
    lines.extend(_unreadable_section(report.scores))
    lines.extend(_failure_section(report.scores))
    return "\n".join(lines) + "\n"


def _coverage_section(cover: Coverage) -> list[str]:
    """What the rows can and cannot support. Above the numbers, deliberately."""
    lines = ["## Coverage", ""]
    lines.append(
        f"{cover.rows} rows score routing; "
        f"{len(cover.met)} of {len(cover.met) + len(cover.unmet)} scope clauses met."
    )
    lines.append("")
    for clause, ids in cover.unmet:
        lines.append(
            f"- **MISSING** {clause.name}: {len(ids)}/{clause.minimum} ({clause.source})"
        )
    if cover.thin:
        lines.append(
            f"- **Thin** — {', '.join(lane.value for lane in cover.thin)}: "
            f"fewer than {RATE_NEEDS} rows, so the lane's percentage is a "
            "fraction with a very small denominator rather than a rate."
        )
    if cover.empty_lanes:
        lines.append(
            f"- **Empty** — {', '.join(lane.value for lane in cover.empty_lanes)}: "
            "no rows at all, so nothing below says anything about it."
        )
    if not cover.unmet and not cover.thin and not cover.empty_lanes:
        lines.append("Every lane is represented and every scope clause is met.")
    lines.append("")
    return lines


def _headline_section(scores: TrajectoryScores) -> list[str]:
    """The metric the architecture exists to get right, and the gap under it."""
    lines = ["## The metric", "", "| | |", "| --- | --- |"]
    lines.append(f"| Rows run | {scores.total} |")
    lines.append(f"| …scored | {scores.scored} |")
    lines.append(f"| …unscored (trace could not be believed) | {scores.unscored} |")
    lines.append(f"| Split traces (#103) | {scores.split_traces} |")
    lines.append(
        f"| **Tool-selection accuracy** | **{_rate(scores.tool_selection)}** "
        f"(target ≥ {_percent(TOOL_SELECTION_TARGET)}) |"
    )
    lines.append(f"| Clean trajectories | {_rate(scores.clean)} |")
    lines.append(
        f"| Rows where a wrong query is observable | {scores.query_scoreable} "
        f"of {scores.total} |"
    )
    lines.append("")
    lines.extend(_target_verdict(scores))
    return lines


def _target_verdict(scores: TrajectoryScores) -> list[str]:
    """#74's third acceptance criterion: the gap, with the shapes that made it."""
    if scores.meets_target is None:
        return [
            "**Unverified, which is not the same as unmet.** Nothing was scored, "
            "so nothing has failed and nothing has passed.",
            "",
        ]
    if scores.meets_target:
        return [
            f"**Target met.** {_rate(scores.tool_selection)} against "
            f"≥ {_percent(TOOL_SELECTION_TARGET)}. Read the shapes below anyway: "
            "lane selection can be at target while the trajectories under it are "
            "paying for calls the turn did not need.",
            "",
        ]
    shapes = scores.shapes
    made_of = ", ".join(
        f"{shapes[shape]} {shape.value}" for shape in FAILURE_SHAPES if shapes[shape]
    )
    return [
        f"**Target not met.** {_rate(scores.tool_selection)} against "
        f"≥ {_percent(TOOL_SELECTION_TARGET)} — a gap of "
        f"{_rate(scores.gap)}, made of {made_of or 'nothing this report can name'}.",
        "",
        "The gap's *shape* is the explanation, not its size: the same number "
        "made of `no_tool` and made of `wrong_lane` is two different problems "
        "with two different fixes.",
        "",
    ]


def _lane_section(scores: TrajectoryScores, cover: Coverage) -> list[str]:
    """The per-lane breakdown. #74's third scope clause."""
    lines = [
        "## By lane",
        "",
        "| Lane | Rows | Scored | Tool selection | Clean | "
        "wrong lane | no tool | extra tools | wrong query |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    thin = set(cover.thin)
    for lane in scores.lanes:
        shapes = lane.shapes
        name = lane.lane.value + (" *" if lane.lane in thin else "")
        lines.append(
            f"| {name} | {lane.total} | {lane.scored} | "
            f"{_rate(lane.tool_selection)} | {_rate(lane.clean_rate)} | "
            f"{shapes[Shape.WRONG_LANE]} | {shapes[Shape.NO_TOOL]} | "
            f"{shapes[Shape.EXTRA_TOOLS]} | {shapes[Shape.WRONG_QUERY]} |"
        )
    lines.append("")
    if thin:
        lines.append(f"`*` fewer than {RATE_NEEDS} rows. See the caveat above.")
        lines.append("")
    lines.extend(_below_target(scores.lanes))
    return lines


def _below_target(lanes: Sequence[LaneTrajectories]) -> list[str]:
    """Name the lanes under the bar, because that is what the breakdown is for."""
    under = [lane for lane in lanes if lane.meets_target is False]
    if not under:
        return []
    named = ", ".join(
        f"{lane.lane.value} at {_rate(lane.tool_selection)}" for lane in under
    )
    return [f"**Below {_percent(TOOL_SELECTION_TARGET)}:** {named}.", ""]


def _shape_section(scores: TrajectoryScores) -> list[str]:
    """The four shapes, with what each one means. See the module docstring."""
    shapes = scores.shapes
    lines = ["## The four failure shapes", ""]
    for shape in FAILURE_SHAPES:
        lines.append(f"**{shape.value}** — {shapes[shape]}. {SHAPE_MEANS[shape]}")
        lines.append("")
    lines.append(
        "`wrong_query` is measured on the "
        f"{len(QUERY_ARGUMENT)} tools that take the ask as an argument "
        f"({', '.join(sorted(tool.value for tool in QUERY_ARGUMENT))}) and on no "
        "others, so a zero here is not evidence that no query drifted elsewhere."
    )
    lines.append("")
    return lines


def _unreadable_section(scores: TrajectoryScores) -> list[str]:
    """Traces nobody can score, and why. The #103 section."""
    unreadable = scores.unreadable()
    if not unreadable:
        return []
    lines = ["## Traces that could not be believed", ""]
    if scores.split_traces:
        lines.append(
            f"**{scores.split_traces} turn(s) arrived as more than one trace.** "
            "The tool spans are all still there and every one of them is "
            "unattached to the turn that caused it, so the numbers above are "
            "over a subset nobody chose. This is issue #103's propagation, not "
            "a model behaviour: check it with `make trace-boundary` before "
            "reading anything else in this document."
        )
        lines.append("")
    for judgement in unreadable:
        lines.append(f"- `{judgement.expectation.entry_id}` — {judgement.detail}")
    lines.append("")
    return lines


def _failure_section(scores: TrajectoryScores) -> list[str]:
    """Every wrong trajectory, worst shape first. What to read after the numbers."""
    failures = scores.failures()
    if not failures:
        return ["## Failures", "", "None.", ""]
    lines = ["## Failures", ""]
    for judgement in failures:
        lines.extend(_failure(judgement))
    return lines


def _failure(judgement: Judgement) -> list[str]:
    """One failure, with the argument for the row attached."""
    expectation = judgement.expectation
    expected = expectation.tool.value if expectation.tool else "no tool"
    called = ", ".join(tool.value for tool in judgement.trajectory.tools) or "nothing"
    lines = [
        f"### `{expectation.entry_id}` — {judgement.shape.value}",
        "",
        f"- **Asked** — {expectation.message!r}",
        f"- **Expected** — {expected} ({expectation.lane.value})",
        f"- **Called** — {called}",
        f"- **What went wrong** — {judgement.detail}",
    ]
    if expectation.why:
        lines.append(f"- **Why this row exists** — {expectation.why}")
    lines.append("")
    return lines


def _rate(value: float | None) -> str:
    """A rate as a percentage, or an em dash where there is no rate."""
    return _EM_DASH if value is None else _percent(value)


def _percent(value: float) -> str:
    """One rate, as a percentage with one decimal place."""
    return f"{value * 100:.1f}%"
