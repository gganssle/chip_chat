"""One run, written down: the numbers, the frames that produced them, and the holes.

A baseline is a document somebody reads in six months, so this renders Markdown
rather than returning a dictionary. Four properties are deliberate, and each of
them is a way an evaluation report can lie.

**Coverage is printed above the scores, not below them.** A set that is missing
its hard cases produces a good F1 and a false conclusion, and the reader has to
meet that fact before they meet the number.

**Both stages are always printed.** The ``described`` and ``believed`` tables
answer "is the model wrong" and "are the floors wrong", and printing only the
one that looks better is how a tuning run becomes a press release.

**A number that does not exist is printed as an em dash.** Never as zero. A slot
the set never labeled has no recall, and a nought in that cell reads as a slot
that failed.

**The failures are named.** Every false positive, every wrong outcome, every
frame the lane could not answer for arrives with its ``photo_id``, because the
next action after reading this report is opening one of those photographs.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from chip_chat.eval.photos.coverage import MINIMUM_PHOTOS, Coverage, coverage
from chip_chat.eval.photos.labels import LabeledSet
from chip_chat.eval.photos.run import PhotoRun
from chip_chat.eval.photos.scoring import (
    F1_TARGET,
    ComponentScore,
    DetectionScore,
    Scores,
    Stage,
    score,
    slot_rows,
)
from chip_chat.vision.describe import ConfidenceProfile, confidence_profile
from chip_chat.vision.matcher import Outcome, SlotRules

__all__ = ["Report", "build_report", "render"]


@dataclass(frozen=True, slots=True)
class Report:
    """Everything a baseline has to say, before it is a string.

    Attributes:
        deployment: Which vision deployment answered. Configuration, so that a
            report from two months ago says what it was measuring.
        content_version: The catalogue build the vocabulary came from. Two
            reports from different builds are not comparable, and this is the
            only way to know.
        rules: The floors in force. The tuning surface #56 exists to move, so a
            report that did not record them would be a measurement of an
            unknown configuration.
        coverage: Whether the set is the set the ticket asked for.
        scores: What the run produced.
        confidence: How the run's slot confidences were distributed -- issue
            #53's fourth acceptance criterion, checked here against real
            photographs for the first time.
        photos: How many frames were run.
    """

    deployment: str
    content_version: str | None
    rules: SlotRules
    coverage: Coverage
    scores: Scores
    confidence: ConfidenceProfile
    photos: int


def build_report(
    labels: LabeledSet,
    runs: Sequence[PhotoRun],
    *,
    deployment: str,
    content_version: str | None,
    rules: SlotRules,
) -> Report:
    """Score a run and gather everything the report needs.

    Args:
        labels: The ground truth.
        runs: What the lane produced.
        deployment: The vision deployment's name.
        content_version: The catalogue build.
        rules: The floors the run used.

    Returns:
        The :class:`Report`.
    """
    return Report(
        deployment=deployment,
        content_version=content_version,
        rules=rules,
        coverage=coverage(labels),
        scores=score(labels, runs),
        confidence=confidence_profile(
            run.description.meal for run in runs if run.description is not None
        ),
        photos=len(runs),
    )


def render(report: Report) -> str:
    """Render the report as Markdown.

    Args:
        report: The gathered numbers.

    Returns:
        The document, ready to be written next to the set as a baseline.
    """
    lines: list[str] = [
        "# Labeled photo set — baseline",
        "",
        f"- Deployment: `{report.deployment}`",
        f"- Catalogue build: `{report.content_version or 'unrecorded'}`",
        f"- Frames run: {report.photos}",
        "",
    ]
    lines += _floors(report.rules)
    lines += _coverage(report.coverage)
    lines += _components(report.scores.described)
    lines += _components(report.scores.believed)
    lines += _verdict(report.scores)
    lines += _detection(report.scores.several_meals)
    lines += _detection(report.scores.not_chipotle)
    lines += _outcomes(report.scores)
    lines += _confidence(report.confidence)
    lines += _errors(report.scores)
    return "\n".join(lines).rstrip() + "\n"


def _floors(rules: SlotRules) -> list[str]:
    lines = [
        "## Floors in force",
        "",
        "| Slot | Floor | Required |",
        "| --- | --- | --- |",
    ]
    for slot, rule in rules.rules.items():
        lines.append(
            f"| {slot.value} | {rule.floor:.2f} | {'yes' if rule.required else 'no'} |"
        )
    lines.append("")
    return lines


def _coverage(cover: Coverage) -> list[str]:
    lines = [
        "## Coverage",
        "",
        f"{cover.photos} frames, against a floor of {MINIMUM_PHOTOS}: "
        f"**{'met' if cover.enough_photos else 'NOT met'}**.",
        "",
    ]
    if not cover.unmet:
        lines += ["Every scope requirement is met.", ""]
        return lines
    lines += [
        "**These scope requirements are unmet, so the scores below are not yet a "
        "baseline for the pipeline as a whole — only for the frames present.**",
        "",
        "| Requirement | Have | Need | Source |",
        "| --- | --- | --- | --- |",
    ]
    for requirement, ids in cover.unmet:
        lines.append(
            f"| {requirement.name} | {len(ids)} | {requirement.minimum} | "
            f"{requirement.source} |"
        )
    lines.append("")
    return lines


def _components(score_: ComponentScore) -> list[str]:
    heading = {
        Stage.DESCRIBED: (
            "## Components — as described (before the floors)",
            "What the model said, scored against the labels. This is the model: the "
            "prompt, the deployment, the image the pipeline sent it.",
        ),
        Stage.BELIEVED: (
            "## Components — as believed (after the floors)",
            "What stage 5 was willing to act on. This is the PRD's "
            "*photo → order* number, and the gap from the table above is what the "
            "floors cost.",
        ),
    }[score_.stage]
    lines = [
        heading[0],
        "",
        heading[1],
        "",
        f"Scored over {score_.photos} single-meal frames.",
        "",
        "| Slot | TP | FP | FN | Precision | Recall | F1 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in slot_rows(score_):
        name = row.slot.value if row.slot is not None else "**overall**"
        lines.append(
            f"| {name} | {row.true_positives} | {row.false_positives} | "
            f"{row.false_negatives} | {_number(row.precision)} | "
            f"{_number(row.recall)} | {_number(row.f1)} |"
        )
    lines.append("")
    if score_.unreadable_filled:
        lines += [
            f"The pipeline filled a slot the photograph does not answer "
            f"{score_.unreadable_filled} time(s). Not scored in either direction — "
            "there is no fact to be right about — but a describer that does this "
            "often is guessing rather than reading.",
            "",
        ]
    return lines


def _verdict(scores: Scores) -> list[str]:
    believed = scores.believed.overall.f1
    described = scores.described.overall.f1
    if believed is None:
        return [
            "## Against the target",
            "",
            f"No frame was scored, so there is no F1 to compare with {F1_TARGET:.2f}. "
            "The target is **unverified**, which is not the same as unmet.",
            "",
        ]
    verdict = "met" if scores.believed.meets_target else "NOT met"
    lines = [
        "## Against the target",
        "",
        f"PRD §05 asks for component-level F1 ≥ {F1_TARGET:.2f} on photo → order. "
        f"Believed F1 is **{believed:.3f}**: target **{verdict}**.",
        "",
    ]
    if described is not None:
        gap = described - believed
        lines += [
            f"Described F1 is {described:.3f}, so the floors cost {gap:+.3f}. "
            "A large positive gap is a floor set too high — the model had the slot "
            "and the pipeline threw it away; a gap near zero with poor precision in "
            "both is a floor set too low.",
            "",
        ]
    return lines


def _detection(detection: DetectionScore) -> list[str]:
    lines = [
        f"## Detection — {detection.event}",
        "",
        "| Detected and real | Detected, not real | Real, missed |",
        "| --- | --- | --- |",
        f"| {detection.true_positives} | {detection.false_positives} | "
        f"{detection.false_negatives} |",
        "",
        f"Precision {_number(detection.precision)}, recall "
        f"{_number(detection.recall)}, F1 {_number(detection.f1)}.",
        "",
    ]
    if detection.false_positive_ids:
        lines += [
            f"Flagged and should not have been: {_ids(detection.false_positive_ids)}.",
            "",
        ]
    if detection.false_negative_ids:
        lines += [
            f"Should have been flagged and was not: "
            f"{_ids(detection.false_negative_ids)}.",
            "",
        ]
    return lines


def _outcomes(scores: Scores) -> list[str]:
    outcomes = scores.outcomes
    lines = [
        "## Which path each frame took",
        "",
        "Issue #55 built three paths off the happy one. These are them, measured "
        "rather than assumed.",
        "",
        f"Expected path taken on {outcomes.correct} of {outcomes.scored} frames "
        f"({_number(outcomes.accuracy)}).",
        "",
    ]
    if outcomes.wrong:
        lines += ["| Frame | Expected | Took |", "| --- | --- | --- |"]
        lines += [
            f"| {photo_id} | {expected.value} | {observed.value} |"
            for photo_id, expected, observed in outcomes.wrong
        ]
        lines.append("")
        lines += _outcome_note(outcomes.wrong)
    return lines


def _outcome_note(wrong: Sequence[tuple[str, Outcome, Outcome]]) -> list[str]:
    """Say which of the wrong paths is the expensive one, where it happened.

    A frame that should have declined and instead resolved is the failure the
    multi-meal decision record calls *"confident, well-formed, and only
    detectable by the visitor"*. It is worth a sentence rather than a row, and
    a report that listed it beside a harmless over-cautious clarification would
    be flattening the difference the whole decision rests on.
    """
    fabricated = [
        photo_id
        for photo_id, expected, observed in wrong
        if expected is not Outcome.RESOLVED and observed is Outcome.RESOLVED
    ]
    if not fabricated:
        return []
    return [
        f"**{len(fabricated)} frame(s) produced a draft where the correct behaviour "
        f"was to decline or ask: {_ids(tuple(fabricated))}.** This is the expensive "
        "direction — a well-formed order nobody in the photograph asked for.",
        "",
    ]


def _confidence(profile: ConfidenceProfile) -> list[str]:
    distributed = profile.is_meaningfully_distributed()
    return [
        "## Are the confidences worth thresholding on?",
        "",
        "Issue #53's fourth acceptance criterion, against real photographs. D3 moves "
        "the failure *into a slot confidence we can threshold on*, which is only "
        "true if the confidences carry information.",
        "",
        f"- Filled slots: {profile.slots}",
        f"- Pinned at 1.0: {profile.pinned} ({profile.pinned_fraction:.1%})",
        f"- Distinct values: {profile.distinct}",
        f"- Spread (population standard deviation): {profile.spread:.3f}",
        "",
        f"`is_meaningfully_distributed()`: **{distributed}**."
        + (
            ""
            if distributed
            else " A model reporting that it answered rather than how sure it was "
            "makes every floor above arbitrary, whatever the F1 says."
        ),
        "",
    ]


def _errors(scores: Scores) -> list[str]:
    if not scores.errors:
        return []
    lines = [
        "## Frames the lane could not answer for",
        "",
        "Counted apart from every score above: a deployment that is down is not a "
        "model that is wrong. Each of these scored as a miss on every labeled "
        "component, so a run with many of them has a depressed recall that is not "
        "about accuracy.",
        "",
        "| Frame | Why |",
        "| --- | --- |",
    ]
    lines += [f"| {photo_id} | {why} |" for photo_id, why in scores.errors]
    lines.append("")
    return lines


def _number(value: float | None) -> str:
    """Three decimals, or an em dash where the quantity does not exist."""
    return "—" if value is None else f"{value:.3f}"


def _ids(ids: Sequence[str]) -> str:
    return ", ".join(f"`{photo_id}`" for photo_id in ids)
