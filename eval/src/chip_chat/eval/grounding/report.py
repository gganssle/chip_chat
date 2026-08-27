"""One run, written down: a rate, two gates, and a category held to counts.

Markdown, for the reason ``eval/golden/report.py`` gives: a baseline is a
document somebody reads in six months, not a dictionary somebody parses. Five
properties are deliberate here, and each is a way a report on *these* two
metrics could mislead a reader who is doing nothing wrong.

**What could not be measured is printed before what could.** Three of the five
findings are unmeasured on every run this repository can make today, and a
document that led with *groundedness: --* under a heading and explained
underneath would be read as a system with a problem rather than as a system with
a missing wire.

**The two gates are counts with their targets beside them, never rates.** PRD
section 05 says zero and #75 repeats it in bold. A launch gate rendered as a
percentage is a launch gate somebody argues about at the wrong moment.

**The allergen and dietary category prints counts and no percentage.** That is
:mod:`chip_chat.eval.grounding.scoring`'s decision rather than this module's,
and the report states the argument where the table is: a rate over allergen
answers is a percentage of a safety property, and there is no acceptable value
for it below one.

**Over-refusal sits in the same table as under-refusal, at the same size.** #75
is explicit that measuring only one produces a system that hedges everything and
scores beautifully. A report that put over-refusal in a footnote would be that
system's report.

**A rate that does not exist prints as an em dash.** Never as zero, never as
100%. An unscored finding has no rate, and a nought in that cell reads as a
system that got everything wrong.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from chip_chat.eval.grounding.coverage import RATE_NEEDS, Coverage, coverage
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Judge, Turn
from chip_chat.eval.grounding.scoring import (
    GROUNDEDNESS_TARGET,
    UNCITED_TARGET,
    Category,
    CategoryScores,
    GroundingScores,
    score,
)
from chip_chat.eval.grounding.verdicts import (
    FINDINGS,
    Finding,
    Judgement,
    Refusal,
)

__all__ = ["FINDING_MEANS", "Report", "build_report", "render"]

_EM_DASH = "--"

FINDING_MEANS: dict[Finding, str] = {
    Finding.CITED: (
        "a response made a claim PRD K2 requires a citation on and carried "
        "none. A rule rather than a judgement, because D9 made a citation an id "
        "the retriever returned -- so its absence is a fact about a payload. "
        "Target: zero."
    ),
    Finding.ADJACENT: (
        "an allergen or dietary answer whose citation has no harvest date to "
        "render beside the claim. PRD K5's stricter half, and the half of it "
        "that is checkable from a span: the renderer draws the date, and #48 "
        "makes a source url and a date arrive together, so a corpus passage "
        "without one is an undated allergen claim waiting to happen. Target: "
        "zero."
    ),
    Finding.MINTED: (
        "the model named a passage the retriever never returned on that turn. "
        "The renderer dropped it rather than showing a source that does not "
        "exist, which is the design working -- and an agent minting sources is "
        "worth counting even when nothing reaches the visitor. Target: zero."
    ),
    Finding.SUPPORTED: (
        "a claim that had to be grounded, on a turn whose `retriever.search` "
        "spans returned nothing. The floor under groundedness: no judge can "
        "call a claim supported by passages that do not exist. Needs no judge "
        "and no credentials, which is why it is the one finding a free run "
        "produces."
    ),
    Finding.GROUNDED: (
        "a food or policy claim the retrieved passages do not support. Judged, "
        "and judged against what the turn actually retrieved rather than "
        "against the corpus -- a judge handed the corpus would score a system "
        "that never opened it as grounded."
    ),
}
"""What each finding means, printed beside its count. See the module docstring."""


@dataclass(frozen=True, slots=True)
class Report:
    """Everything a grounding baseline has to say, before it is a string.

    Attributes:
        source: What answered.
        dataset: The dataset's name.
        version: Its version. The whole reason the rows come from a dataset:
            two scores are comparable only if this is the same string.
        judged_by: What settled the two judged findings, or empty where nothing
            did.
        caveat: What this run's numbers are and are not worth, in prose,
            rendered above everything else.
        coverage: Whether the rows can support the numbers.
        scores: What the run produced.
    """

    source: str
    dataset: str
    version: str
    coverage: Coverage
    scores: GroundingScores
    judged_by: str = ""
    caveat: str = ""


def build_report(
    rows: Sequence[Question],
    turns: Sequence[Turn],
    *,
    source: str,
    dataset: str,
    version: str,
    judge: Judge | None = None,
    judged_by: str = "",
    caveat: str = "",
) -> Report:
    """Score a run and assemble everything the document needs.

    Args:
        rows: The dataset rows that were run.
        turns: What came back.
        source: What answered.
        dataset: The dataset's name.
        version: The dataset's version.
        judge: Settles groundedness and the refusal, where there is one.
        judged_by: What to call that judge in the document.
        caveat: What this run's numbers are worth. See :attr:`Report.caveat`.

    Returns:
        The report.
    """
    return Report(
        source=source,
        dataset=dataset,
        version=version,
        coverage=coverage(rows),
        scores=score(rows, turns, judge=judge),
        judged_by=judged_by,
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
        "# Groundedness and citation-presence baseline",
        "",
        f"- **Answered by** — {report.source}",
        f"- **Dataset** — {report.dataset} `{report.version}`, "
        f"{report.coverage.rows} rows",
        "- **Judged by** — "
        + (report.judged_by or "nothing; both judged findings are unscored"),
        f"- **Targets** — groundedness ≥ {_percent(GROUNDEDNESS_TARGET)}; "
        f"uncited menu claims = {UNCITED_TARGET} (PRD §05, K2)",
        "",
    ]
    if report.caveat:
        lines.extend(f"> {line}".rstrip() for line in report.caveat.splitlines())
        lines.append("")
    lines.extend(_coverage_section(report.coverage))
    lines.extend(_unmeasured_section(report.scores))
    lines.extend(_headline_section(report.scores))
    lines.extend(_findings_section(report.scores))
    lines.extend(_refusal_section(report.scores))
    lines.extend(_dietary_section(report.scores, report.coverage))
    lines.extend(_failure_section(report.scores))
    return "\n".join(lines) + "\n"


def _coverage_section(cover: Coverage) -> list[str]:
    """What the rows can and cannot support. Above the numbers, deliberately."""
    lines = ["## Coverage", ""]
    lines.append(
        f"{cover.rows} rows, {cover.stated} of which the set states something "
        f"about; {cover.dietary} in the allergen and dietary category. "
        f"{len(cover.met)} of {len(cover.met) + len(cover.unmet)} scope clauses met."
    )
    lines.append("")
    for clause, ids in cover.unmet:
        lines.append(
            f"- **MISSING** {clause.name}: {len(ids)}/{clause.minimum} ({clause.source})"
        )
    if cover.thin_category:
        lines.append(
            f"- **Thin** — fewer than {RATE_NEEDS} allergen and dietary rows. "
            "The category is held to counts rather than to a rate, so this does "
            "not move its verdict; it does mean the counts below are over a "
            "very small set."
        )
    if not cover.unmet and not cover.thin_category:
        lines.append("Every scope clause is met.")
    lines.append("")
    return lines


def _unmeasured_section(scores: GroundingScores) -> list[str]:
    """What could not be observed, and why. Before the numbers, deliberately."""
    unmeasured = [
        (finding, scores.unscored(finding))
        for finding in FINDINGS
        if scores.unscored(finding)
    ]
    if not unmeasured and not scores.errors and not scores.unreadable_traces:
        return []
    lines = ["## What this run could not measure", ""]
    for finding, judgements in unmeasured:
        counted: dict[str, int] = {}
        for judgement in judgements:
            reason = judgement.details.get(finding, "no reason recorded")
            counted[reason] = counted.get(reason, 0) + 1
        reasons = sorted(counted.items(), key=lambda pair: (-pair[1], pair[0]))
        lines.append(
            f"- **{finding.value}** — {len(judgements)} row(s) unscored. "
            + " ".join(f"{count}: {reason}." for reason, count in reasons)
        )
    if scores.refusals_asked and not scores.refusals_scored:
        lines.append(
            f"- **refusal** — {scores.refusals_asked} row(s) state which way the "
            "turn should have gone and none could be judged; whether a reply "
            "declines is a property of prose, and a keyword rule would produce "
            "a number measuring the keyword rule."
        )
    if scores.split_traces:
        lines.append(
            f"- **{scores.split_traces} split trace(s)** — the retrieval and the "
            "response arrived under different trace ids, so nothing can show "
            "the passages belong to the answer. Issue #103, checked with "
            "`make trace-boundary`."
        )
    elif scores.unreadable_traces:
        lines.append(
            f"- **{scores.unreadable_traces} unreadable recording(s)** — see the "
            "failures below for which and why."
        )
    if scores.errors:
        lines.append(
            f"- **{len(scores.errors)} row(s) the source could not answer** — "
            + ", ".join(f"`{entry_id}`" for entry_id in scores.errors)
            + ". An outage is not a model being wrong, so these are in no rate."
        )
    lines.append("")
    return lines


def _headline_section(scores: GroundingScores) -> list[str]:
    """The rate, then the gates. Counts and rates never share a column."""
    lines = ["## The two metrics", "", "| | | |", "| --- | --- | --- |"]
    lines.append(
        f"| Turns run | {scores.total} | of the register's rows; `--only` runs fewer |"
    )
    lines.append(
        f"| **Groundedness** | **{_rate(scores.groundedness)}** | "
        f"target ≥ {_percent(GROUNDEDNESS_TARGET)}, over "
        f"{scores.scored(Finding.GROUNDED)} of {scores.asked(Finding.GROUNDED)} "
        "rows it was asked on |"
    )
    lines.append(
        f"| **Uncited menu claims** | **{_count(scores.uncited_claims)}** | "
        f"target {UNCITED_TARGET}; a count, never a rate |"
    )
    lines.append(
        f"| Minted citations | {_count(scores.minted_citations)} | "
        f"target {UNCITED_TARGET} |"
    )
    lines.append(
        f"| Claims with nothing retrieved | {_count(scores.unsupported_claims)} | "
        "the floor under groundedness |"
    )
    lines.append(f"| Over-refusals | {scores.over_refusals} | measured, not gated |")
    lines.append(f"| Under-refusals | {scores.under_refusals} | in the dietary gate |")
    lines.append("")
    lines.extend(_verdicts(scores))
    return lines


def _verdicts(scores: GroundingScores) -> list[str]:
    """The three verdicts, each of which can be *unverified* rather than false."""
    lines: list[str] = []
    if scores.meets_target is None:
        lines.append(
            "**Groundedness unverified, which is not the same as unmet.** "
            "Nothing was scored, so nothing has failed and nothing has passed."
        )
    elif scores.meets_target:
        lines.append(
            f"**Groundedness target met.** {_rate(scores.groundedness)} against "
            f"≥ {_percent(GROUNDEDNESS_TARGET)}."
        )
    else:
        lines.append(
            f"**Groundedness target not met.** {_rate(scores.groundedness)} "
            f"against ≥ {_percent(GROUNDEDNESS_TARGET)} — a gap of "
            f"{_rate(scores.gap)}."
        )
    lines.append("")
    lines.append(_gate_line("Citation gate", scores.citation_gate))
    lines.append("")
    lines.append(_gate_line("Allergen and dietary gate", scores.dietary_gate))
    lines.append("")
    return lines


def _gate_line(name: str, passed: bool | None) -> str:
    """One gate, as the third value it is allowed to take.

    A gate nobody measured has not passed, and saying so is the whole reason
    this returns three strings rather than two.
    """
    if passed is None:
        return f"**{name}: unmeasured.** A gate nobody measured has not passed."
    return f"**{name}: {'pass' if passed else 'FAIL'}.**"


def _findings_section(scores: GroundingScores) -> list[str]:
    """The four verdict-carrying findings, with what each one means."""
    lines = [
        "## The findings",
        "",
        "| Finding | Asked | Scored | Failed | What it means |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for finding in FINDINGS:
        lines.append(
            f"| `{finding.value}` | {scores.asked(finding)} | "
            f"{scores.scored(finding)} | {scores.failed(finding)} | "
            f"{FINDING_MEANS[finding]} |"
        )
    lines.append("")
    lines.append(
        "*Asked* is the rows the finding could apply to; *scored* is the rows "
        "something could be observed on, and the gap between them is the "
        "wiring rather than the model. The two rules are not the same rule: "
        "`cited` and `minted` are asked of **every** turn, because PRD K2's "
        "count is over turns and any turn can make a claim; `supported` and "
        "`grounded` are asked only where the set says a grounded claim was owed "
        "or the response declared one, because a rate whose denominator holds "
        "every turn is a rate diluted by the turns that had nothing to be "
        "wrong about."
    )
    lines.append("")
    return lines


def _refusal_section(scores: GroundingScores) -> list[str]:
    """Both directions, at the same size. See the module docstring."""
    lines = [
        "## The refusal, in both directions",
        "",
        "| | Rows | |",
        "| --- | ---: | --- |",
        f"| Correct | {_refusals(scores, Refusal.CORRECT)} | answered what the "
        "published data answers, declined what it does not |",
        f"| **Over-refusal** | {scores.over_refusals} | declined where the "
        "corpus plainly had the answer |",
        f"| **Under-refusal** | {scores.under_refusals} | answered where it "
        "does not support one |",
        f"| Unscored | {_refusals(scores, Refusal.UNSCORED)} | no judge, or "
        "nothing came back |",
        f"| Not asked | {_refusals(scores, Refusal.NOT_ASKED)} | the set says "
        "neither, so neither is a mistake this row could show |",
        "",
        "Measuring only under-refusal produces a system that hedges everything "
        "and scores beautifully. That is why both rows are here and why "
        "neither is a footnote.",
        "",
    ]
    return lines


def _dietary_section(scores: GroundingScores, cover: Coverage) -> list[str]:
    """The stricter category, as counts and no percentage."""
    dietary = scores.category(Category.DIETARY)
    ordinary = scores.category(Category.ORDINARY)
    lines = [
        "## Allergen and dietary questions",
        "",
        f"{dietary.total} of {scores.total} rows, reported apart because that "
        "is where a confident wrong answer is a safety issue rather than an "
        "accuracy issue.",
        "",
        "**Held to counts, not to a rate.** A percentage over allergen answers "
        "is a percentage of a safety property — it says how often the promise "
        "held, which is a sentence nobody would sign. So the bar is that every "
        "count below is zero, and there is no value of *groundedness on "
        "allergen questions* between 0 and 1 that would be acceptable.",
        "",
    ]
    lines.extend(_category_table(dietary, ordinary))
    lines.append(_gate_line("Allergen and dietary gate", scores.dietary_gate))
    lines.append("")
    if dietary.over_refusals:
        lines.append(
            f"Beside the gate and deliberately outside it: {dietary.over_refusals} "
            "over-refusal(s) in this category. A turn that declined to guess "
            "about an allergy did the safe thing badly rather than the unsafe "
            "thing, and gating it would push in exactly the direction #75 warns "
            "about."
        )
        lines.append("")
    return lines


def _category_table(dietary: CategoryScores, ordinary: CategoryScores) -> list[str]:
    """The counts, with the ordinary category beside them for scale."""
    rows: list[tuple[str, int | None, int | None]] = [
        ("Uncited claims", dietary.uncited_claims, ordinary.uncited_claims),
        ("Minted citations", dietary.minted_citations, ordinary.minted_citations),
        (
            "Claims with nothing retrieved",
            dietary.unsupported_claims,
            ordinary.unsupported_claims,
        ),
        (
            "Ungrounded claims",
            _ungrounded(dietary),
            _ungrounded(ordinary),
        ),
        ("Under-refusals", dietary.under_refusals, ordinary.under_refusals),
        ("Over-refusals", dietary.over_refusals, ordinary.over_refusals),
    ]
    lines = [
        "| | Allergen and dietary | Everything else |",
        "| --- | ---: | ---: |",
    ]
    for name, mine, theirs in rows:
        lines.append(f"| {name} | {_count(mine)} | {_count(theirs)} |")
    lines.append("")
    return lines


def _ungrounded(scores: CategoryScores) -> int | None:
    """Claims a judge said the passages do not support, or ``None`` if unjudged."""
    return scores.failed(Finding.GROUNDED) if scores.scored(Finding.GROUNDED) else None


def _failure_section(scores: GroundingScores) -> list[str]:
    """Every turn that was wrong about something. What to read after the numbers."""
    failures = scores.failures()
    if not failures:
        return ["## Failures", "", "None — over what could be measured.", ""]
    lines = ["## Failures", ""]
    for judgement in failures:
        lines.extend(_failure(judgement))
    return lines


def _failure(judgement: Judgement) -> list[str]:
    """One failure, with the argument for the row attached."""
    question = judgement.question
    named = [finding.value for finding in judgement.failed]
    if judgement.refusal in (Refusal.OVER_REFUSAL, Refusal.UNDER_REFUSAL):
        named.append(judgement.refusal.value)
    lines = [
        f"### `{question.entry_id}` — {', '.join(named)}",
        "",
        f"- **Asked** — {question.message!r}",
        f"- **Category** — {'allergen and dietary' if question.dietary else 'ordinary'}"
        f", {question.lane.value} lane",
        f"- **Owed** — {_owed(question)}",
        f"- **Retrieved** — {_retrieved(judgement)}",
    ]
    for line in judgement.lines():
        lines.append(f"- **What went wrong** — {line}")
    if question.why:
        lines.append(f"- **Why this row exists** — {question.why}")
    lines.append("")
    return lines


def _owed(question: Question) -> str:
    """What the set says this row's turn owed, in words."""
    owed: list[str] = []
    if question.answer_owed:
        owed.append("an answer the published data supports")
    if question.refusal_owed:
        owed.append("a refusal")
    if question.citation_owed:
        owed.append("a citation")
    if question.adjacent_owed:
        owed.append("the citation beside the claim (K5)")
    return ", ".join(owed) or "nothing this eval can hold it to"


def _retrieved(judgement: Judgement) -> str:
    """What the turn's retrieval looked like, in one line."""
    evidence = judgement.evidence
    if evidence is None:
        return "not observable -- the source does not read span trees"
    unreadable = evidence.unreadable_because
    if unreadable is not None:
        return unreadable
    confidence = (
        "" if evidence.confidence is None else f", confidence {evidence.confidence}"
    )
    return (
        f"{len(evidence.passages)} passage(s) across {evidence.searches} "
        f"search(es){confidence}"
    )


def _refusals(scores: GroundingScores, outcome: Refusal) -> int:
    """How many rows took one refusal outcome."""
    return sum(1 for j in scores.judgements if j.refusal is outcome)


def _count(value: int | None) -> str:
    """A count, or an em dash where nothing could be counted."""
    return _EM_DASH if value is None else str(value)


def _rate(value: float | None) -> str:
    """A rate as a percentage, or an em dash where there is no rate."""
    return _EM_DASH if value is None else _percent(value)


def _percent(value: float) -> str:
    """One rate, as a percentage with one decimal place."""
    return f"{value * 100:.1f}%"
