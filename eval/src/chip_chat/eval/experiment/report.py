"""The experiment and the comparison, as Markdown somebody will actually read.

Two documents, and they are shaped by the same rule the other five reports in
``eval/`` follow: **the caveats go above the numbers**. A reader who has already
seen 92% will read the paragraph explaining what it is a percentage of as an
excuse. So the source, the configuration, the axes that could not be applied and
the judge come first, and the table comes after them.

The comparison document adds one more thing at the top: the regressions. A
comparison read top to bottom should say *what broke* before it says *what the
new number is*, because the number is what somebody already believes and the
regression is what they came to find out.
"""

from collections.abc import Sequence

from chip_chat.eval.experiment.compare import MATERIAL, Comparison
from chip_chat.eval.experiment.results import (
    TARGETS,
    ExperimentResult,
    Metric,
    Target,
)
from chip_chat.eval.wiring import NO_WIRING, stated

__all__ = ["render_comparison", "render_result"]


def render_result(result: ExperimentResult) -> str:
    """One experiment, as a Markdown document.

    Args:
        result: The result.

    Returns:
        The document.
    """
    lines = [
        f"# Experiment — {result.experiment}",
        "",
        f"- **Configuration** — `{result.fingerprint}`, prompt {result.prompt_version}",
        f"- **Dataset** — {result.dataset} `{result.dataset_version}`, "
        f"{result.rows} rows",
        f"- **Answered by** — {result.source}",
        f"- **Lanes wired** — {_wiring(result)}",
        "- **Judged by** — "
        + (result.judge or "nothing; the judged findings are unscored"),
        f"- **Run at** — {result.ran_at}",
    ]
    if result.judge_tokens:
        lines.append(f"- **Judge spend** — {result.judge_tokens} tokens")
    lines.append("")
    if result.caveat:
        lines.extend([result.caveat, ""])
    if result.inert_axes:
        lines.extend(
            [
                f"> **Recorded and not applied: {', '.join(result.inert_axes)}.** This "
                "run wires no lane behind those axes, so a flat line on either is a "
                "fact about the deployment rather than evidence that the setting "
                "does not matter.",
                "",
            ]
        )
    lines.extend(_targets_table(result))
    lines.extend(_lanes_table(result))
    lines.extend(_requirements_table(result))
    return "\n".join(lines) + "\n"


def _wiring(result: ExperimentResult) -> str:
    """The lane configuration, as a header line says it.

    An unstated one is spelled out rather than left blank, because a blank in a
    header reads as *nothing was wired* and it means *nobody wrote it down*.
    Those are different runs and only one of them can be compared with anything.
    """
    if not stated(result.wiring):
        return (
            "**not stated** — this result was recorded before the harness wrote "
            "the lane configuration down, so what it measured is not known from "
            "the file. Re-run it before comparing it with anything"
        )
    if result.wiring == NO_WIRING.label:
        return (
            "`none` — the hardcoded three-item menu and the account fixture "
            "answered, and `ask_account_question`, `get_recommendations` and "
            "`match_meal_from_photo` were not offered to the model at all"
        )
    return f"`{result.wiring}`"


def _targets_table(result: ExperimentResult) -> list[str]:
    lines = [
        "## The targets",
        "",
        "| Metric | Target | This run | Met | Over |",
        "| --- | ---: | ---: | :---: | --- |",
    ]
    for target in TARGETS:
        metric = result.metric(target.metric)
        lines.append(
            f"| {target.label} | {_target(target)} | {_value(metric, target)} "
            f"| {_met(metric.meets(target))} | {_over(metric)} |"
        )
    lines.append("")
    verdict = result.targets_met
    if verdict is True:
        lines.append("**Every target measured was met.**")
    elif verdict is False:
        missed = [
            target.label
            for target in TARGETS
            if result.metric(target.metric).meets(target) is False
        ]
        lines.append(f"**Not met:** {', '.join(missed)}.")
    else:
        unmeasured = [
            target.label
            for target in TARGETS
            if result.metric(target.metric).meets(target) is None
        ]
        lines.append(
            f"**Unverified, which is not the same as unmet:** {', '.join(unmeasured)}. "
            "A target nobody measured has not passed."
        )
    lines.append("")
    for target in TARGETS:
        metric = result.metric(target.metric)
        if metric.note:
            lines.append(f"- `{target.metric}` — {metric.note}")
    lines.append("")
    return lines


def _lanes_table(result: ExperimentResult) -> list[str]:
    lines = [
        "## By lane",
        "",
        "The aggregate above is one number over five lanes with different "
        "amounts of the product behind them. This is where a regression in one "
        "of them stops hiding behind a gain in another.",
        "",
        "| Lane | Rows | Completion | Tool selection | wrong lane | no tool "
        "| extra tools | wrong query | ungrounded | over-refusals |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for lane in result.lanes:
        shapes = lane.shapes
        lines.append(
            f"| {lane.lane} | {lane.cases} | {_rate(lane.completion)} "
            f"| {_rate(lane.tool_selection)} | {shapes.get('wrong_lane', 0)} "
            f"| {shapes.get('no_tool', 0)} | {shapes.get('extra_tools', 0)} "
            f"| {shapes.get('wrong_query', 0)} | {lane.grounded_failed} "
            f"| {lane.over_refusals} |"
        )
    lines.append("")
    return lines


def _requirements_table(result: ExperimentResult) -> list[str]:
    lines = [
        "## By requirement",
        "",
        "A lane is where the architecture is; a requirement is where the "
        "product is. One case can cover two requirements and one requirement "
        "can be covered by six cases, so this is a different partition of the "
        "same rows and not a re-presentation of the table above.",
        "",
        "| Requirement | Lane | Cases | Passed | Failed | Unscored | Rate |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in result.requirements:
        if item.delegated_to:
            lines.append(
                f"| {item.requirement} | {item.lane} | — | — | — | — | "
                f"measured in {item.delegated_to} |"
            )
            continue
        lines.append(
            f"| {item.requirement} | {item.lane} | {item.cases} | {item.passed} "
            f"| {item.failed} | {item.unscored} | {_rate(item.rate)} |"
        )
    lines.append("")
    return lines


def _side_wiring(result: ExperimentResult) -> str:
    """One side's lane configuration, for the comparison's header lines."""
    return f"`{result.wiring}`" if stated(result.wiring) else "**not stated**"


def _refusal(comparison: Comparison) -> list[str]:
    """What a comparison prints instead of tables when a side did not say.

    Deliberately not a warning above the numbers. A reader who has seen the
    numbers has already formed the conclusion the warning was meant to prevent,
    which is the argument every report in ``eval/`` makes for putting caveats
    first -- and this is the case where the caveat cannot be qualified into
    something readable, because the missing information is not *how much* the
    delta is worth but *whether it is about the model at all*.
    """
    sides = " and ".join(comparison.unstated_sides)
    return [
        "## No comparison — the lane configuration was not recorded",
        "",
        f"**{sides} did not record which lanes were wired, so this comparison "
        "is not drawn.** It is not that the delta would be uncertain. It is "
        "that a delta between a run with the account lane wired and one without "
        "it looks exactly like a model that got better: the tool list a model "
        "is offered is a function of what is wired, so a lane coming up moves "
        "whole rows from unscoreable to scored. On 27 August 2026 that "
        "difference was the entire content of a baseline nobody could read.",
        "",
        "Re-run both arms — `--lanes none` for the unwired slice, `--lanes "
        "wired` for the deployment's own account and personalization lanes — "
        "and compare the results those write. A recorded result carries its "
        "wiring from that run onward.",
        "",
    ]


def render_comparison(comparison: Comparison) -> str:
    """Two experiments, as a Markdown document.

    Args:
        comparison: The comparison.

    Returns:
        The document. #73's demo criterion is this string existing with two real
        prompt versions behind it -- or, where one of them did not record its
        lane configuration, a refusal in place of the tables. See
        :attr:`~chip_chat.eval.experiment.compare.Comparison.stated`.
    """
    baseline = comparison.baseline
    candidate = comparison.candidate
    lines = [
        f"# {baseline.experiment} → {candidate.experiment}",
        "",
        f"- **Baseline** — `{baseline.fingerprint}`, prompt "
        f"{baseline.prompt_version}, run {baseline.ran_at}, "
        f"lanes {_side_wiring(baseline)}",
        f"- **Candidate** — `{candidate.fingerprint}`, prompt "
        f"{candidate.prompt_version}, run {candidate.ran_at}, "
        f"lanes {_side_wiring(candidate)}",
        f"- **Dataset** — {candidate.dataset} `{candidate.dataset_version}`, "
        f"{candidate.rows} rows",
        "",
    ]
    if not comparison.stated:
        return "\n".join(lines + _refusal(comparison)) + "\n"
    lines.extend([f"**{comparison.verdict}**", ""])
    if comparison.warnings:
        lines.append("> **Read these first.**")
        lines.extend(f"> - {note}" for note in comparison.warnings)
        lines.append("")
    if comparison.regressions:
        lines.extend(["## What got worse", ""])
        lines.extend(f"- {item}" for item in comparison.regressions)
        lines.append("")
    if comparison.improvements:
        lines.extend(["## What got better", ""])
        lines.extend(f"- {item}" for item in comparison.improvements)
        lines.append("")
    lines.extend(_metric_deltas(comparison))
    lines.extend(_lane_deltas(comparison))
    lines.extend(_requirement_deltas(comparison))
    lines.extend(
        [
            "## How this document decides",
            "",
            f"A rate has to move by at least {MATERIAL:.0%} to be called a "
            "regression, because the dataset is "
            f"{candidate.rows} rows and one row is about "
            f"{(1 / candidate.rows if candidate.rows else 0):.1%} of it — a "
            "threshold below one row would report a regression every time a "
            "single case flipped. A **count** has no threshold: PRD §05 makes "
            "the gates zero, and one more than zero is one more than zero. A "
            "requirement has no threshold either, because it is usually covered "
            "by one or two cases and a rate over two cases has no resolution for "
            "a threshold to use.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _metric_deltas(comparison: Comparison) -> list[str]:
    lines = [
        "## The targets, side by side",
        "",
        "| Metric | Target | Baseline | Candidate | Δ |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for metric in comparison.metrics:
        target = metric.target
        lines.append(
            f"| {target.label} | {_target(target)} | "
            f"{_scalar(metric.baseline, target)} | "
            f"{_scalar(metric.candidate, target)} | {_delta(metric.delta, target)} |"
        )
    lines.append("")
    return lines


def _lane_deltas(comparison: Comparison) -> list[str]:
    lines = [
        "## By lane",
        "",
        "| Lane | Completion Δ | Tool selection Δ | Failure shapes that moved |",
        "| --- | ---: | ---: | --- |",
    ]
    for lane in comparison.lanes:
        moved = ", ".join(
            f"{shape} {count:+d}" for shape, count in lane.shape_deltas().items() if count
        )
        lines.append(
            f"| {lane.lane} | {_signed(lane.completion_delta)} | "
            f"{_signed(lane.tool_selection_delta)} | {moved or '—'} |"
        )
    lines.append("")
    return lines


def _requirement_deltas(comparison: Comparison) -> list[str]:
    lines = [
        "## By requirement",
        "",
        "| Requirement | Baseline | Candidate | Δ |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in comparison.requirements:
        if item.delegated_to:
            lines.append(
                f"| {item.requirement} | — | — | measured in {item.delegated_to} |"
            )
            continue
        before = None if item.baseline is None else item.baseline.rate
        after = None if item.candidate is None else item.candidate.rate
        lines.append(
            f"| {item.requirement} | {_rate(before)} | {_rate(after)} | "
            f"{_signed(item.delta)} |"
        )
    lines.append("")
    return lines


def _target(target: Target) -> str:
    return f"{target.target:.0f}" if target.counts else f"≥ {target.target:.0%}"


def _value(metric: Metric, target: Target) -> str:
    return _scalar(metric.value, target)


def _scalar(value: float | None, target: Target) -> str:
    if value is None:
        return "--"
    return f"{value:.0f}" if target.counts else f"{value:.1%}"


def _delta(value: float | None, target: Target) -> str:
    if value is None:
        return "--"
    return f"{value:+.0f}" if target.counts else f"{value:+.1%}"


def _rate(value: float | None) -> str:
    return "--" if value is None else f"{value:.1%}"


def _signed(value: float | None) -> str:
    return "--" if value is None else f"{value:+.1%}"


def _met(verdict: bool | None) -> str:
    if verdict is None:
        return "—"
    return "yes" if verdict else "**no**"


def _over(metric: Metric) -> str:
    if not metric.asked:
        return "—"
    return f"{metric.scored} of {metric.asked} row(s)"


def _joined(items: Sequence[str]) -> str:
    return ", ".join(items) if items else "none"
