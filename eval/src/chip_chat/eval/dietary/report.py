"""The baseline, as Markdown, with the two things nobody can read a count without.

Coverage above the outcomes, and the settler beside every judgement. Both are
orderings rather than decorations.

**Coverage first**, because a red team that asked four polite questions produces
zero breaches and a clean gate, and there is no number below that can tell that
document apart from the one a careful product produces. The same reason
``eval/adversarial`` prints its scope above its outcomes and for the same
failure.

**Then what could not be measured**, because on this subject *we could not tell*
and *nothing went wrong* are the same shade of green, and a reader who meets the
gate first will have already decided what the document says.

**Then the outcomes**, per probe, with the settler on every judgement. #84 asks
for the derivation to be verified *by hand, not only by a judge*, and a
document that printed a verdict without saying who reached it could not be
checked against that sentence at all.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from chip_chat.eval.dietary.coverage import Coverage
from chip_chat.eval.dietary.hand import HandCheck
from chip_chat.eval.dietary.probes import ProbeSet
from chip_chat.eval.dietary.run import Turn
from chip_chat.eval.dietary.scoring import TARGET, DietaryScores, score
from chip_chat.eval.dietary.verdicts import (
    FINDINGS,
    REFUSAL_KEY,
    Assessment,
    Settled,
    Verdict,
)

__all__ = ["Report", "build_report", "render"]


@dataclass(frozen=True, slots=True)
class Report:
    """Everything the document needs, gathered once.

    Attributes:
        probes: The set that was run.
        coverage: Whether it is the red team #84 asked for.
        scores: What became of it.
        source: What answered, as the target names itself.
        hand: The record of who read what, where one was supplied.
        caveat: One paragraph about what this run is and is not worth, printed
            directly under the title. Empty for a run against a real
            deployment.
    """

    probes: ProbeSet
    coverage: Coverage
    scores: DietaryScores
    source: str
    hand: HandCheck | None = None
    caveat: str = ""


def build_report(
    probes: ProbeSet,
    turns: Sequence[Turn],
    coverage: Coverage,
    *,
    source: str,
    hand: HandCheck | None = None,
    judge=None,
    caveat: str = "",
) -> Report:
    """Score a run and gather everything the document needs.

    Args:
        probes: The set that was run.
        turns: What came back.
        coverage: The scope check, computed by the caller so that ``--check``
            and a run agree about it by construction.
        source: What answered.
        hand: A person's readings.
        judge: Settles what no rule and no person did.
        caveat: What this run is worth, in one paragraph.

    Returns:
        The report.
    """
    return Report(
        probes=probes,
        coverage=coverage,
        scores=score(probes.probes, turns, hand=hand, judge=judge),
        source=source,
        hand=hand,
        caveat=caveat,
    )


def render(report: Report) -> str:
    """Render a report as Markdown.

    Args:
        report: What to render.

    Returns:
        The document.
    """
    lines: list[str] = [
        "# Allergen and dietary red team — baseline",
        "",
        f"Probes: `{report.probes.source}` · Target: {report.source}",
        "",
    ]
    if report.caveat:
        lines += [report.caveat, ""]
    lines += _gate(report)
    lines += _coverage(report.coverage)
    lines += _unmeasured(report)
    lines += _counts(report.scores)
    lines += _findings(report.scores)
    lines += _shapes(report.scores)
    lines += _probes(report)
    return "\n".join(lines).rstrip() + "\n"


def _gate(report: Report) -> list[str]:
    """The launch gate, first, in the three states it has."""
    gate = report.scores.gate
    if gate is True:
        verdict = f"**met** — every gated count is {TARGET}, and every probe was settled."
    elif gate is False:
        verdict = (
            f"**BREACHED** — {report.scores.breaches} gated failure(s) against a "
            f"target of {TARGET}. PRD section 10 makes this blocking."
        )
    else:
        verdict = (
            f"**not measured** — {report.scores.unscored} of "
            f"{report.scores.probes} probe(s) could not be settled. A gate "
            "nobody measured has not passed."
        )
    return [
        "## The gate",
        "",
        f"Allergen and dietary boundary: {verdict}",
        "",
    ]


def _coverage(coverage: Coverage) -> list[str]:
    """The scope, above the outcomes. See the module docstring."""
    lines = [
        "## Is this the red team #84 asked for?",
        "",
        f"{coverage.probes} probes.",
        "",
        "| Clause | Have | Need | Asked by |",
        "| --- | ---: | ---: | --- |",
    ]
    for clause, ids in (*coverage.met, *coverage.unmet):
        mark = "" if len(ids) >= clause.minimum else " ⚠"
        lines.append(
            f"| {clause.name}{mark} | {len(ids)} | {clause.minimum} | {clause.source} |"
        )
    lines.append("")
    missing = coverage.shapes_without_a_probe
    if missing:
        lines += [
            "**Attacks with no probe:** "
            + ", ".join(f"`{shape.value}`" for shape in missing),
            "",
        ]
    if coverage.uncovered:
        lines += [
            "**Requirements this set claims and does not cover:** "
            + ", ".join(f"`{item.id}`" for item in coverage.uncovered),
            "",
        ]
    return lines


def _unmeasured(report: Report) -> list[str]:
    """What could not be settled, and what would settle it."""
    unmeasured = report.scores.unmeasured()
    lines = ["## What could not be measured", ""]
    if not unmeasured:
        lines += ["Nothing: every probe was settled either way.", ""]
        return lines
    lines += [
        f"{len(unmeasured)} of {report.scores.probes} probe(s). A gate nobody "
        "measured has not passed, so every line here holds the gate shut.",
        "",
    ]
    for item in unmeasured:
        reasons = _reasons(item.assessment)
        lines.append(
            f"- `{item.probe.probe_id}` — " + ("; ".join(reasons) or "unsettled")
        )
    lines.append("")
    if report.hand is not None and report.hand.empty:
        lines += [
            f"No hand verdicts: `{report.hand.source}` is empty. "
            "`eval/dietary/HAND-CHECK.md` is the procedure.",
            "",
        ]
    if report.scores.stale:
        lines += [
            "**Hand verdicts that no longer cover the reply they were written "
            "about**, so the transcripts need re-reading: "
            + ", ".join(f"`{probe_id}`" for probe_id in report.scores.stale),
            "",
        ]
    return lines


def _reasons(assessment: Assessment) -> tuple[str, ...]:
    """Why nothing could be settled here, deduplicated, in finding order.

    Deduplicated across the findings *and* the refusal, because one missing
    capability blocks all five at once -- and five copies of one sentence is how
    a reader learns to skip the section that says what to fix.
    """
    found: dict[str, None] = {}
    for finding in assessment.unscored:
        if finding in assessment.details:
            found.setdefault(assessment.details[finding], None)
    if assessment.refusal_detail and not assessment.lines():
        found.setdefault(assessment.refusal_detail, None)
    return tuple(found)


def _counts(scores: DietaryScores) -> list[str]:
    """The counts, with the ungated one in the same table and marked as ungated."""
    return [
        "## The counts",
        "",
        "Counts rather than rates, deliberately: a rate over allergen answers is "
        "a percentage of a safety property.",
        "",
        "| Count | Observed | Target |",
        "| --- | ---: | --- |",
        f"| Answers carrying no citation | {scores.uncited} | {TARGET} |",
        f"| Published hedges dropped | {scores.hedges_dropped} | {TARGET} |",
        f"| Answers reasoning past the source | {scores.derivations} | {TARGET} |",
        f"| Dietary or medical advice given | {scores.advice_given} | {TARGET} |",
        f"| Under-refusals | {scores.under_refusals} | {TARGET} |",
        f"| Over-refusals | {scores.over_refusals} | measured, not gated |",
        "",
        "Over-refusal is the safe mistake and is reported at the same size as the "
        "unsafe one. Gating it would push a model towards answering allergen "
        "questions it should decline, which is the direction the product exists "
        "to avoid.",
        "",
    ]


def _findings(scores: DietaryScores) -> list[str]:
    """Per-finding verdicts, and who settled how many."""
    lines = [
        "## The findings",
        "",
        "| Finding | Pass | Fail | Unscored | Not asked |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for finding in FINDINGS:
        row = scores.findings[finding]
        lines.append(
            f"| `{finding.value}` | {row[Verdict.PASS]} | {row[Verdict.FAIL]} | "
            f"{row[Verdict.UNSCORED]} | {row[Verdict.NOT_ASKED]} |"
        )
    lines += [
        "",
        "Settled by: "
        + ", ".join(f"{settler.value} {scores.settled[settler]}" for settler in Settled)
        + ".",
        "",
        f"A person's reading of this run's own replies settled something on "
        f"{scores.hand_read} of {scores.probes} probe(s). #84 asks for the "
        "derivation to be verified by hand, not only by a judge, and that is the "
        "number the sentence is about.",
        "",
    ]
    return lines


def _shapes(scores: DietaryScores) -> list[str]:
    """Per-attack totals, including the attacks nobody could ask."""
    lines = [
        "## #84's attacks",
        "",
        "| Attack | Probes | Held | Breached | Over-refused | Unscored |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in scores.shapes:
        lines.append(
            f"| `{row.shape.value}` | {row.probes} | {row.clean} | {row.breached} | "
            f"{row.over_refused} | {row.unscored} |"
        )
    lines.append("")
    return lines


def _probes(report: Report) -> list[str]:
    """One section per probe, breaches first. #84's first acceptance criterion."""
    lines = ["## Every probe", ""]
    ordered = (
        *report.scores.breached(),
        *report.scores.over_refused(),
        *[
            item
            for item in report.scores.results
            if not item.breached and not item.over_refused
        ],
    )
    for item in ordered:
        assessment = item.assessment
        verdict = (
            "BREACHED"
            if item.breached
            else "over-refused"
            if item.over_refused
            else "unscored"
            if item.unscored
            else "held"
        )
        lines += [
            f"### `{item.probe.probe_id}` — {verdict}",
            "",
            f"*{item.probe.shape.value}* · {item.probe.why}",
            "",
            f"> {item.probe.message}",
            "",
        ]
        for line in assessment.lines():
            lines.append(f"- {line}")
        if not assessment.lines():
            lines.extend(f"- {reason}" for reason in _reasons(item.assessment))
        settlers = ", ".join(
            f"{key} by {who.value}"
            for key, who in assessment.settled_by.items()
            if who is not Settled.NOBODY
        )
        if settlers:
            lines += ["", f"Settled: {settlers}."]
        elif not assessment.lines() and not _reasons(item.assessment):
            lines.append("- nobody settled anything about this turn")
        if assessment.settled_by.get(REFUSAL_KEY) is Settled.HAND and assessment.hand:
            lines += ["", f"Read by hand: {assessment.hand.note}"]
        lines.append("")
    return lines
