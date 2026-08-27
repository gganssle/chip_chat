"""One sweep, written down: the ablation table, the demo bar, and the holes.

A baseline is a document somebody reads in six months, so this renders Markdown
rather than returning a dictionary. Five properties are deliberate, and each of
them is a way a retrieval report can mislead.

**The corpus is named at the top, above everything.** Two reports taken against
two harvests are not comparable, and a run id is the only thing that says which
is which. A report whose numbers moved because the corpus moved would otherwise
read as a retrieval regression.

**Coverage and resolution are printed above the scores.** Coverage says whether
these were the questions the ticket asked for; resolution says which of their
labels the corpus under test actually holds. A reader has to meet both before
they meet a rate, because a good number over the wrong set or over a set half of
which went unscored is a good number about nothing.

**The demo bar has its own section, above the full table.** #50 states one
criterion -- *top-3 recall on your allergen questions, measured, with numbers* --
and a criterion buried in a row of a five-by-four grid is a criterion nobody
reads. It is printed with the questions that produced it, because the next
action after reading a bad one is opening one of those questions.

**A number that does not exist is printed as an em dash.** Never as zero. A
category whose labels do not resolve against this corpus has no recall, and a
nought in that cell reads as a category that failed.

**The failures are named.** Every unresolved label, every overconfident negative,
every constraint breach and every question an arm could not run arrives with its
id.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from chip_chat.eval.retrieval.configurations import SERVING, Configuration
from chip_chat.eval.retrieval.corpus import Resolution
from chip_chat.eval.retrieval.coverage import (
    MINIMUM_QUESTIONS,
    Coverage,
    coverage,
)
from chip_chat.eval.retrieval.questions import RetrievalSet
from chip_chat.eval.retrieval.run import Answer
from chip_chat.eval.retrieval.scoring import (
    RECALL_AT,
    ArmScores,
    CategoryScores,
    score_sweep,
)

__all__ = ["Report", "build_report", "render"]

_DASH = "—"


@dataclass(frozen=True, slots=True)
class Report:
    """Everything a baseline has to say, before it is a string.

    Attributes:
        source: What answered the questions. An alias, or the offline index.
        measured: Whether a real retrieval service produced these numbers.
            ``False`` marks a run against
            :class:`~chip_chat.eval.retrieval.testing.OfflineIndex`, whose
            vector half carries no relevance -- and the first paragraph of the
            rendered document says so, because a table looks the same either
            way.
        resolution: The labels, against the corpus under test.
        coverage: Whether the set is the set the ticket asked for.
        evaluates_filters: Whether the source applies a query's ``filter`` at
            all. ``False`` against
            :class:`~chip_chat.eval.retrieval.testing.OfflineIndex`, which does
            not — so the constrained questions are printed unscored rather than
            with a violation count that would really be a count of the
            fixture's omission.
        arms: One per configuration, in ablation order.
        floor: The reranker floor the run judged confidence against. The one
            number in the retrieval layer that has not been measured, so a
            report that did not record it would be a measurement of an unknown
            configuration.
    """

    source: str
    measured: bool
    evaluates_filters: bool
    resolution: Resolution
    coverage: Coverage
    arms: tuple[ArmScores, ...]
    floor: float

    @property
    def serving(self) -> ArmScores | None:
        """The arm the product actually runs, where the sweep included it."""
        return next((arm for arm in self.arms if arm.arm.name == SERVING.name), None)


def build_report(
    questions: RetrievalSet,
    resolution: Resolution,
    answers: Sequence[Answer],
    configurations: Sequence[Configuration],
    *,
    source: str,
    measured: bool,
    floor: float,
    evaluates_filters: bool = True,
) -> Report:
    """Score a sweep and gather everything the report needs.

    Args:
        questions: The labeled set.
        resolution: Its labels, resolved against the corpus under test.
        answers: Everything the sweep produced.
        configurations: The arms that were run, in the order to print them.
        source: What answered.
        measured: Whether a real service did.
        floor: The reranker floor in force.
        evaluates_filters: Whether it applies a query's ``filter``.

    Returns:
        The :class:`Report`.
    """
    return Report(
        source=source,
        measured=measured,
        evaluates_filters=evaluates_filters,
        resolution=resolution,
        coverage=coverage(questions),
        arms=score_sweep(questions, resolution, answers, configurations),
        floor=floor,
    )


def render(report: Report) -> str:
    """Render the report as Markdown.

    Args:
        report: The gathered numbers.

    Returns:
        The document, ready to be written beside the set as a baseline.
    """
    lines: list[str] = [
        "# Retrieval eval — baseline",
        "",
        f"- Source: `{report.source}`",
        f"- Corpus release: `{report.resolution.run_id}` "
        f"({report.resolution.chunks} chunks)",
        f"- Reranker floor: `{report.floor}`",
        f"- Questions: {report.coverage.questions}",
        "",
    ]
    lines += _preamble(report)
    lines += _coverage(report.coverage)
    lines += _resolution(report.resolution)
    lines += _demo_bar(report)
    lines += _ablation(report)
    lines += _negatives(report)
    lines += _constraints(report)
    lines += _errors(report)
    return "\n".join(lines).rstrip() + "\n"


def _preamble(report: Report) -> list[str]:
    """The sentence that has to be read before the tables."""
    if report.measured:
        return [
            "These numbers come from a real retrieval service over the corpus "
            "release named above. They are comparable with another run against "
            "the same release and with no other run.",
            "",
        ]
    return [
        "> **These numbers were not measured against a retrieval service.** The "
        "sweep ran against `chip_chat.eval.retrieval.testing.OfflineIndex`, an "
        "in-memory index whose lexical half is a word-overlap fraction and whose "
        "vector half is an order by chunk id carrying no relevance at all. So "
        "the *vector only* column is a floor rather than a score, every column "
        "containing a vector half is partly scored against noise, and the "
        "reranker cannot reorder anything the keyword half did not already know.",
        ">",
        "> What this run does measure, and measures properly: the **resolution** "
        "below — which of the set's labels name a place the committed corpus "
        "actually holds — and the harness itself, at full size. The ablation "
        "table is here because the arithmetic that produced it is the same "
        "arithmetic the credentialed run uses, not because its cells are "
        "evidence about retrieval.",
        "",
    ]


def _coverage(cover: Coverage) -> list[str]:
    lines = [
        "## Is this the set the ticket asked for",
        "",
        f"{cover.questions} questions (need {MINIMUM_QUESTIONS}).",
        "",
        "| Clause | Have | Need | Source |",
        "|---|---:|---:|---|",
    ]
    for requirement, ids in (*cover.met, *cover.unmet):
        mark = "" if len(ids) >= requirement.minimum else " ⚠"
        lines.append(
            f"| {requirement.name}{mark} | {len(ids)} | {requirement.minimum} "
            f"| {requirement.source} |"
        )
    lines.append("")
    return lines


def _resolution(resolution: Resolution) -> list[str]:
    """Which labels this corpus holds. #50's chunking-regression check.

    Printed as the list of what is *missing* rather than as a count, because a
    count moving from 0 to 1 tells a reader something happened and the name
    tells them what.
    """
    unresolved = resolution.unresolved()
    lines = [
        "## Do the labels still name anything",
        "",
        f"{len(resolution.places) - len(unresolved)} of {len(resolution.places)} "
        f"labels resolve against `{resolution.run_id}`.",
        "",
    ]
    if not unresolved:
        lines += [
            "Every label names a place this corpus holds. A label that stops "
            "resolving after a chunking change is the regression #50's fourth "
            "acceptance criterion is about, and it appears here by name.",
            "",
        ]
        return lines
    lines += [
        "These labels name nothing in this corpus. Each is **unscored** — in no "
        "numerator and no denominator — rather than counted as a miss: a "
        "retriever cannot return a passage the corpus does not hold. If one of "
        "these resolved in the previous baseline, that is a chunking regression "
        "rather than a gap.",
        "",
        "| Question | Place |",
        "|---|---|",
    ]
    lines += [
        f"| `{place.question_id}` | {place.label.describe()} |" for place in unresolved
    ]
    lines.append("")
    return lines


def _demo_bar(report: Report) -> list[str]:
    """#50's own criterion, printed on its own above everything else."""
    lines = [
        f"## The demo bar: top-{RECALL_AT} recall on the allergen questions",
        "",
        '> *"top-3 recall on your allergen questions, measured, with numbers."* '
        "— issue #50",
        "",
        "| Configuration | recall@3 | hit@3 | MRR | P@1 | scored | unscored |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in report.arms:
        allergens = arm.allergens
        lines.append(
            f"| {arm.arm.name} | {_rate(allergens.recall)} "
            f"| {_rate(allergens.hit_rate)} | {_rate(allergens.mrr)} "
            f"| {_rate(allergens.precision_at_one)} | {allergens.scored} "
            f"| {allergens.unscored} |"
        )
    lines.append("")
    serving = report.serving
    if serving is not None:
        allergens = serving.allergens
        if allergens.scored == 0:
            lines += [
                "**The bar is unmeasured.** No allergen question's labels "
                "resolve against this corpus, so there is no number here to "
                "hold anything to.",
                "",
            ]
        else:
            lines += [
                f"**The baseline the rest of the project is held to: "
                f"{_rate(allergens.recall)} on {allergens.scored} allergen "
                f"questions, under `{serving.arm.name}` — the configuration "
                f"production runs.** A chunking change, a prompt change or an "
                f"index rebuild that moves this number down has broken "
                f"something, whatever else it improved.",
                "",
            ]
        if allergens.ceiling < 1.0:
            lines += [
                f"The best `recall@3` these questions allow is "
                f"{_rate(allergens.ceiling)}: one of them is answered in more "
                f"than three published places and three slots cannot hold four.",
                "",
            ]
    return lines


def _ablation(report: Report) -> list[str]:
    """The full grid: every category under every arm."""
    lines = [
        "## The ablation",
        "",
        "Every category under every configuration. `recall@3` is the proportion "
        "of a question's published places that came back in the top three, "
        "meaned over questions; `hit@3` is whether any did. They differ only on "
        "the questions answered in more than one place.",
        "",
    ]
    for arm in report.arms:
        lines += [
            f"### {arm.arm.name}",
            "",
            arm.arm.note,
            "",
            "| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        lines += [_category_row(category) for category in arm.categories]
        lines += [
            f"| **all categories** | **{_rate(arm.recall)}** | | | | | | |",
            "",
        ]
        if arm.skew:
            lines += [
                f"⚠ {arm.skew} passage(s) matched a label and carry an id the "
                "corpus export does not hold. The live index and the export "
                "disagree; this is a defect rather than a data gap.",
                "",
            ]
    return lines


def _category_row(category: CategoryScores) -> str:
    return (
        f"| {category.category.value} | {_rate(category.recall)} "
        f"| {_rate(category.hit_rate)} | {_rate(category.mrr)} "
        f"| {_rate(category.precision_at_one)} | {_rate(category.ceiling)} "
        f"| {category.scored} | {category.unscored} |"
    )


def _negatives(report: Report) -> list[str]:
    """The negative set: restraint, and the questions where there was none."""
    lines = [
        "## The negative set",
        "",
        "Questions the published corpus genuinely cannot answer. The correct "
        "behaviour is a retrieval that is **not grounded** — RFC-001 §10 and "
        "PRD K3 both say the honest reply is that the published data does not "
        "cover it. Scored apart from recall, because a retriever that returned "
        "nothing for everything would score perfectly here and nowhere else.",
        "",
        "| Configuration | restrained | asked | rate |",
        "|---|---:|---:|---:|",
    ]
    for arm in report.arms:
        negatives = arm.negatives
        lines.append(
            f"| {arm.arm.name} | {negatives.restrained} | {negatives.scored} "
            f"| {_rate(negatives.rate)} |"
        )
    lines.append("")
    for arm in report.arms:
        overconfident = arm.negatives.overconfident()
        if not overconfident:
            continue
        lines += [
            f"Reported as grounded under `{arm.arm.name}`, and unanswerable:",
            "",
        ]
        lines += [
            f"- `{j.question.question_id}` — {j.question.text}" for j in overconfident
        ]
        lines.append("")
    return lines


def _constraints(report: Report) -> list[str]:
    """The constrained questions: read, and honoured."""
    lines = [
        "## Constraints",
        "",
        "Questions answered by a filter rather than by a ranking. Two things "
        "are checked: that the constraint was read out of the sentence at all, "
        "and that every returned passage honours it. The second is a **count**, "
        "and it does not average — one item carrying a published dairy mark, "
        "offered to somebody who said they cannot have dairy, is not made "
        "acceptable by nineteen that did not.",
        "",
        "| Configuration | constraint read | asked | passages in breach |",
        "|---|---:|---:|---:|",
    ]
    for arm in report.arms:
        constraints = arm.constraints
        breach = str(constraints.violations) if report.evaluates_filters else _DASH
        lines.append(
            f"| {arm.arm.name} | {constraints.read} | {constraints.total} | {breach} |"
        )
    lines.append("")
    if not report.evaluates_filters:
        lines += [
            "The breach column is unscored: this source does not evaluate a "
            "query's `filter`, so every passage came back unfiltered and a "
            "count taken here would be a count of that rather than of anything "
            "the retriever did. The **constraint read** column is real — it is "
            "`chip_chat.search.query.read` running on the visitor's sentence, "
            "and that is the half of the constrained case a filter cannot fix "
            "later.",
            "",
        ]
        return lines
    for arm in report.arms:
        breached = arm.constraints.breached()
        if not breached:
            continue
        lines += [f"In breach under `{arm.arm.name}`:", ""]
        lines += [
            f"- `{j.question.question_id}` — {len(j.violations)} passage(s): "
            + ", ".join(f"`{chunk_id[:12]}`" for chunk_id in j.violations)
            for j in breached
        ]
        lines.append("")
    return lines


def _errors(report: Report) -> list[str]:
    """Questions no arm could run. An outage is not a retriever ranking badly."""
    rows = [(arm, j) for arm in report.arms for j in arm.errors]
    if not rows:
        return []
    return [
        "## Questions that could not be run",
        "",
        "Neither a hit nor a miss. The source refused, so there is nothing here "
        "about retrieval.",
        "",
        *[
            f"- `{j.question.question_id}` under `{arm.arm.name}` — {j.error}"
            for arm, j in rows
        ],
        "",
    ]


def _rate(value: float | None) -> str:
    """A rate as the report prints it, or an em dash where there is none."""
    return _DASH if value is None else f"{value:.0%}"
