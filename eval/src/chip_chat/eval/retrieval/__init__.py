"""The retriever, scored on its own, before a model can paraphrase over it.

Issue #50 in one sentence, and it is a sentence about **order** rather than
about coverage: *evaluate the retriever on its own before it ever touches the
agent; retrieval bugs are nearly impossible to diagnose once a model is
paraphrasing over them.* Everything here follows from doing it in that order.
Eight modules, and the order they run in is the order to read them:

================== ================================================ ==============
Module             What it holds                                    Answers
================== ================================================ ==============
``questions``      The set: questions, the places that answer them  what is true
``corpus``         Those places, resolved against a corpus release  can it be
``configurations`` The four arms of the ablation                    against what
``run``            Every question through a retriever, per arm      what happened
``scoring``        recall@3, hit@3, MRR, P@1; restraint; breaches   how well
``coverage``       #50's scope, as clauses                          is this the set
``report``         The baseline, as Markdown                        written down
``testing``        An in-memory index, so the sweep is free         and for nothing
================== ================================================ ==============

.. code-block:: python

    questions = RetrievalSet.load(Path("eval/retrieval/questions.json"))
    resolution = resolve(questions, from_release(Path("landing")))
    answers = run_sweep(questions, RetrieverSource(retriever))
    print(render(build_report(questions, resolution, answers, ABLATION, ...)))

Four things this package will not do, each of which is a way a retrieval
evaluation quietly stops measuring retrieval:

**It will not key a label to a chunk id.** ``chunk_id`` is a content hash, so a
re-chunk gives every chunk in the corpus a new one — and a set keyed on ids
would therefore go uniformly wrong on exactly the change #50's fourth acceptance
criterion exists to catch. A label names a *place*: a kind plus the published
fields that identify the passage a person would point at. See
:mod:`chip_chat.eval.retrieval.questions`.

**It will not score a label the corpus does not hold.** A retriever cannot
return a passage that is not there, so an unresolved label is unscored — in no
numerator, in no denominator, and printed by name above the rates. This is the
golden set's third verdict applied to ground truth. See
:mod:`chip_chat.eval.retrieval.corpus`.

**It will not average the negative set into the recall.** *Did it find the
answer* and *did it decline to be confident about a question with no answer* are
two measurements, and a retriever that returned nothing for everything would
score perfectly on the second. See :mod:`chip_chat.eval.retrieval.scoring`.

**It will not report one number.** Five categories by four configurations, and
the interesting cell is never the aggregate: an ablation that shows hybrid
beating both halves everywhere is a nice number, and one that shows the keyword
half winning on menu rows and the vector half winning inside policy documents is
RFC-001 §08's argument confirmed. See
:mod:`chip_chat.eval.retrieval.configurations`.

## What this measures that the golden set cannot

`eval/golden` runs whole turns, so it can say whether a question about the menu
reached ``search_menu_knowledge``, and it is blind to whether the passages that
came back were the right ones — a model can write a plausible answer from the
wrong three passages, and a lane-selection score cannot see it. This runs the
retriever with **no model in the loop at all**, so every number here is a
property of the index, the chunking and the query construction, and nothing here
can be fixed or broken by a prompt.

The division is the same one `eval/photos` draws against the golden set, for the
same reason and in the other lane: the golden set holds one photo case for
routing and delegates component accuracy to the photo set; it holds six
knowledge cases for routing and answer shape and delegates *did retrieval find
it* here. Eight of this set's questions carry the golden case they came from, and
:mod:`chip_chat.eval.retrieval.coverage` requires that they do.

## The one number that was not measured, and what this does about it

:data:`chip_chat.search.retrieve.PROVISIONAL_RERANKER_FLOOR` is labelled in its
own docstring as *the one number in this package that was not measured*, and it
says where the real one comes from: **issue #50**. The floor decides whether a
reranked result is reported to the agent as grounded or as a near-miss, so it is
the difference between an answer and a refusal on every knowledge turn.

This package does not pick it. It makes picking it possible, which is a
different thing and the honest one: the floor is a run parameter (``--floor``),
it is recorded at the top of every report, and the negative set is the half of
the measurement that a recall figure cannot supply. A floor is right when the
answerable questions stay grounded and the unanswerable ones stop being — so the
number to move it to is read off two tables of the same document, at several
floors, against a real service. Nothing in this repository has run that sweep
yet, and ``eval/retrieval/BASELINE.md`` says so where the numbers would be.
"""

from chip_chat.eval.retrieval.configurations import (
    ABLATION,
    HYBRID,
    KEYWORD,
    RERANKED,
    SERVING,
    VECTOR,
    Configuration,
    semantic_requests,
)
from chip_chat.eval.retrieval.corpus import Place, Resolution, fields_of, resolve
from chip_chat.eval.retrieval.coverage import (
    MINIMUM_QUESTIONS,
    REQUIREMENTS,
    Coverage,
    Requirement,
    coverage,
)
from chip_chat.eval.retrieval.questions import (
    MENU_FIELDS,
    Category,
    Constraint,
    Label,
    Question,
    QuestionError,
    RetrievalSet,
)
from chip_chat.eval.retrieval.report import Report, build_report, render
from chip_chat.eval.retrieval.run import (
    Answer,
    RetrievalSource,
    RetrieverSource,
    run_sweep,
)
from chip_chat.eval.retrieval.scoring import (
    RECALL_AT,
    ArmScores,
    CategoryScores,
    ConstraintScore,
    Judgement,
    NegativeScore,
    score_arm,
    score_sweep,
)

__all__ = [
    "ABLATION",
    "HYBRID",
    "KEYWORD",
    "MENU_FIELDS",
    "MINIMUM_QUESTIONS",
    "RECALL_AT",
    "REQUIREMENTS",
    "RERANKED",
    "SERVING",
    "VECTOR",
    "Answer",
    "ArmScores",
    "Category",
    "CategoryScores",
    "Configuration",
    "Constraint",
    "ConstraintScore",
    "Coverage",
    "Judgement",
    "Label",
    "NegativeScore",
    "Place",
    "Question",
    "QuestionError",
    "Report",
    "Requirement",
    "Resolution",
    "RetrievalSet",
    "RetrievalSource",
    "RetrieverSource",
    "build_report",
    "coverage",
    "fields_of",
    "render",
    "resolve",
    "run_sweep",
    "score_arm",
    "score_sweep",
    "semantic_requests",
]
