"""The allergen boundary, attacked. #84, and the eval PRD section 10 makes blocking.

*"Does this contain dairy?"* is a safety question, it is about to be asked by
strangers on the open internet, and the deliberate decision is that Cilantro
**reports what is published, cites it, and declines to reason past it**. Issue
[#84](https://github.com/gganssle/chip_chat/issues/84) is that decision attacked
from seven directions, and PRD section 10 is why the result is a launch gate
rather than a metric.

============ ================================================== =================
Module       What it holds                                      Answers
============ ================================================== =================
``probes``   the set, and what the honest turn owes each         what was owed
``run``      the reply, the seam, and the two settlers           what it said
``hand``     a person's reading, and the day it expires          who looked
``verdicts`` four findings, and the refusal that has two ways    what became of it
``scoring``  counts that must be zero, and one that must not be  how bad
``coverage`` #84's scope, as clauses the set meets or does not   is it askable
``report``   the baseline, as Markdown                           written down
``slice``    the week-one loop, and what it cannot be asked      the first one
``testing``  targets broken one way each                         driven on paper
============ ================================================== =================

.. code-block:: python

    probes = ProbeSet.load()
    probes.against(catalog)          # the premise, checked against the record
    scores = score(probes.probes, run_probes(probes.probes, target), hand=hand)
    print(scores.derivations, scores.under_refusals, scores.over_refusals)

Five decisions are worth having in view before adding a probe, a finding or a
target. Each is written up where it lives; this is the map.

**A probe is a question with a right answer, which is what makes this not the
adversarial suite.** ``eval/adversarial`` asks *what does it take to get a wrong
answer* and counts what got out; a question with a right answer cannot be
evidence about that. Here the right answer is the subject, so the manifest
records what each turn is **owed** rather than what would count as a breach.
``eval/README.md`` draws the line between all six sets.

**Both directions, and a set that can only measure one of them will not load.**
#84's *what good looks like* ends with *does not refuse questions the corpus
plainly does answer*. A red team of unanswerable questions is passed perfectly
by a deployment that declines everything, so
:meth:`~chip_chat.eval.dietary.probes.ProbeSet.load` refuses a manifest with no
answerable question in it -- a refusal rather than a coverage note, on the
adversarial suite's argument about its concurrency test.

**Over-refusal is measured and deliberately not gated.** It is the safe mistake.
Gating it would push a model towards answering allergen questions it should
decline, which is the direction the product exists to avoid. Same argument
``eval/grounding`` makes, and the two evals have to agree about it or a model
tuned to pass one would fail the other.

**A probe's premise is checked against the published record.**
:meth:`~chip_chat.eval.dietary.probes.ProbeSet.against` walks every
:class:`~chip_chat.eval.dietary.probes.Ground` back to a built catalogue and the
three values ``docs/decisions/allergen-absence.md`` made first-class. A probe
written against *the chart does not mark this* stops being that question the day
the chart marks it, and a set that could not notice would go on scoring an
answer that had moved.

**A person's reading beats a model's, and expires when the reply changes.**
#84's second acceptance criterion is *verified by hand, not only by a judge*, so
:mod:`chip_chat.eval.dietary.hand` is a first-class settler ahead of the judge --
and every verdict carries the fingerprint of the reply it was written about,
because a reading of a reply nobody got is not evidence.

``eval/dietary/README.md`` is the write-up: the seven attacks, why the gate is
counts, what is unmeasured today and what would measure it.
"""

from chip_chat.eval.dietary.coverage import CLAUSES, REQUIRED, Clause, Coverage, coverage
from chip_chat.eval.dietary.hand import (
    HandCheck,
    HandCheckError,
    HandVerdict,
    fingerprint,
)
from chip_chat.eval.dietary.probes import (
    Capability,
    Ground,
    Owed,
    Probe,
    ProbeError,
    ProbeSet,
    Shape,
)
from chip_chat.eval.dietary.report import Report, build_report, render
from chip_chat.eval.dietary.run import Judge, Target, Turn, run_probes
from chip_chat.eval.dietary.scoring import (
    TARGET,
    DietaryScores,
    ProbeScore,
    ShapeScore,
    score,
)
from chip_chat.eval.dietary.slice import SliceTarget
from chip_chat.eval.dietary.verdicts import (
    FINDINGS,
    GATED,
    REFUSAL_KEY,
    Assessment,
    Finding,
    Refusal,
    Settled,
    Verdict,
    assess,
)

__all__ = [
    "CLAUSES",
    "FINDINGS",
    "GATED",
    "REFUSAL_KEY",
    "REQUIRED",
    "TARGET",
    "Assessment",
    "Capability",
    "Clause",
    "Coverage",
    "DietaryScores",
    "Finding",
    "Ground",
    "HandCheck",
    "HandCheckError",
    "HandVerdict",
    "Judge",
    "Owed",
    "Probe",
    "ProbeError",
    "ProbeScore",
    "ProbeSet",
    "Refusal",
    "Report",
    "Settled",
    "Shape",
    "ShapeScore",
    "SliceTarget",
    "Target",
    "Turn",
    "Verdict",
    "assess",
    "build_report",
    "coverage",
    "fingerprint",
    "render",
    "run_probes",
    "score",
]
