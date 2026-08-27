"""Is the answer attached to anything? The two metrics that make the boundary real.

Issue [#75](https://github.com/gganssle/chip_chat/issues/75) in one sentence:
score **groundedness** -- is every food or policy claim supported by the passages
the turn actually retrieved -- and **citation presence** -- a menu or policy
claim with no citation is a failure, full stop -- over the dataset and, once #76
is on, over live traffic.

============= ================================================ =================
Module        What it holds                                    Answers
============= ================================================ =================
``questions`` the dataset's rows, and what each one is owed    what was owed
``evidence``  the ``retriever.search`` spans, as passages      what it had
``run``       the response, the seam, and the judge            what it said
``verdicts``  five findings, and the order they are asked in   what became of it
``scoring``   one rate, four counts, and the stricter category how bad
``coverage``  #75's scope, as clauses the rows meet or do not  is it askable
``report``    the baseline, as Markdown                        written down
``slice``     the week-one loop, answered and recorded         the first one
``testing``   spans and verdicts by hand, and the ceiling      driven on paper
============= ================================================ =================

.. code-block:: python

    dataset = build_dataset(GoldenSet.load(...), LabeledSet.load(...))
    rows = questions(dataset)
    scores = score(rows, run_turns(rows, source), judge=judge)
    print(scores.groundedness, scores.uncited_claims, scores.under_refusals)

Five decisions are worth having in view before adding a row, a finding or a
source. Each is written up where it lives; this is the map.

**The two metrics have different shapes and are never averaged together.** PRD
section 05 sets groundedness at ≥ 0.95 and uncited menu claims at **0**, and the
second is a count because D9 made it a rule: a citation is an id the retriever
returned, so its absence is a fact about a payload rather than an opinion about
prose. :mod:`chip_chat.eval.grounding.scoring` keeps counts and rates in
different columns, the way the golden set keeps its two launch gates.

**The judge scores against what the turn really had.** #75 says so in as many
words, and :mod:`chip_chat.eval.grounding.evidence` is the consequence: the
passages come off the ``retriever.search`` spans rather than out of the corpus,
and where they cannot be read the groundedness finding is unscored rather than
judged against something easier.

**The stricter bar for allergen and dietary questions is a count, not a higher
percentage.** A rate over allergen answers is a percentage of a safety property.
:class:`~chip_chat.eval.grounding.scoring.Category` and
:attr:`~chip_chat.eval.grounding.scoring.GroundingScores.dietary_gate` are how
that is expressed, and
:attr:`~chip_chat.eval.golden.cases.GoldenCase.dietary` is what puts a row in
the category -- declared on the case, because no requirement id and no word list
settles it.

**Over-refusal is measured beside under-refusal and deliberately not gated.**
Only measuring the second produces a system that hedges everything and scores
beautifully. Only *gating* the first would push in the other direction, on the
questions where hedging is the safe mistake.

**Three of the five findings are unmeasured today, and the report says so
first.** ``chip_chat.agent.envelope`` -- decision D9's response envelope -- is
imported by no caller, so no deployment here reports a citation or a claim class
and no span carries one either; that is bead ``cc-bap``, and until it lands the
citation gate reads *unmeasured* rather than *met*. The two judged findings wait
on #76. What a free run does produce is ``supported``: whether a turn that made
a claim had retrieved anything at all to make it from.

``eval/grounding/README.md`` is the write-up: what each finding means, why the
category is held to counts, and what a run against the routing oracle is and is
not worth.
"""

from chip_chat.eval.grounding.coverage import (
    CLAUSES,
    RATE_NEEDS,
    Clause,
    Coverage,
    coverage,
)
from chip_chat.eval.grounding.evidence import Evidence, Passage, read_evidence
from chip_chat.eval.grounding.questions import Question, QuestionError, questions
from chip_chat.eval.grounding.report import Report, build_report, render
from chip_chat.eval.grounding.run import Judge, Turn, TurnSource, run_turns
from chip_chat.eval.grounding.scoring import (
    GROUNDEDNESS_TARGET,
    UNCITED_TARGET,
    Category,
    CategoryScores,
    GroundingScores,
    score,
)
from chip_chat.eval.grounding.slice import SliceTurnSource
from chip_chat.eval.grounding.verdicts import (
    FINDINGS,
    Finding,
    Judgement,
    Refusal,
    Verdict,
    assess,
)

__all__ = [
    "CLAUSES",
    "FINDINGS",
    "GROUNDEDNESS_TARGET",
    "RATE_NEEDS",
    "UNCITED_TARGET",
    "Category",
    "CategoryScores",
    "Clause",
    "Coverage",
    "Evidence",
    "Finding",
    "GroundingScores",
    "Judge",
    "Judgement",
    "Passage",
    "Question",
    "QuestionError",
    "Refusal",
    "Report",
    "SliceTurnSource",
    "Turn",
    "TurnSource",
    "Verdict",
    "assess",
    "build_report",
    "coverage",
    "questions",
    "read_evidence",
    "render",
    "run_turns",
    "score",
]
