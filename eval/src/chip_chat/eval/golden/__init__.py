"""The golden set: the five lanes, measured, from the first ugly slice onward.

Issue #29 in one sentence: *write the evaluation questions in Phase 2, while the
data shapes are fresh, and run them against whatever exists*. It is trap 6 with
a deadline on it -- *"evaluating last. By then you've made a hundred untested
choices"* -- and the deadline is why this package ships against a slice that
fails most of it.

Seven modules, and the order they run in is the order to read them:

================ =============================================== ================
Module           What it holds                                   Answers
================ =============================================== ================
``requirements`` The PRD's identifiers, and what is delegated    what must be covered
``lanes``        The five lanes, and which tool is in which      where a turn goes
``cases``        The set: shapes, refusals, staleness check      what is asked
``coverage``     #29's scope, as checks                          is this the set
``run``          Every case through a deployment                 what happened
``scoring``      Per-lane pass rates, and the PRD's targets      how well
``report``       The baseline, as Markdown                       written down
================ =============================================== ================

``slice`` is the eighth and is not part of that sequence: it is the first thing
on the far side of the :class:`~chip_chat.eval.golden.run.Deployment` seam, the
week-one agent loop wearing the shape a runner can drive.

.. code-block:: python

    golden = GoldenSet.load(DEFAULT_MANIFEST)
    golden.against(catalog)                    # the set is held to the menu too
    observations = run_set(golden, deployment)
    print(render(build_report(golden, observations, deployment=deployment.name)))

Four things this package will not do, each of which is a way an evaluation
quietly stops measuring anything:

**It will not score a set it cannot believe.** A case referencing a requirement
the PRD does not have, an expected lane that does not hold the expected tool, a
write case that does not check for a confirmation card first -- each is refused
at load.

**It will not call an unmeasured thing a pass, or a failure.** Three verdicts.
A deployment declares which signals it can report and a judge settles what no
payload can; anything neither covers comes back ``UNSCORED`` and stays in its own
column all the way to the document.

**It will not let a good average stand in for a good set.**
:mod:`chip_chat.eval.golden.coverage` holds #29's prose scope as executable
clauses -- every PRD requirement covered, every one of the eleven tools expected
by some case, ten cases sitting on a boundary two tools share -- and the report
prints all of it above the scores.

**It will not average the launch gates.** A write without a confirmation and a
menu claim without a citation are counts with a target of zero, and PRD section
05 means zero.
"""

from chip_chat.eval.golden.cases import (
    ANY_PERSONA,
    DEFAULT_MANIFEST,
    JUDGED,
    CaseError,
    Check,
    GoldenCase,
    GoldenSet,
)
from chip_chat.eval.golden.coverage import SHAPES, Coverage, Shape, coverage
from chip_chat.eval.golden.lanes import LANE_OF, TOOLS_IN, Lane, lane_of
from chip_chat.eval.golden.report import Report, build_report, render
from chip_chat.eval.golden.requirements import (
    DELEGATIONS,
    OUT_OF_SCOPE,
    REQUIREMENTS,
    Delegation,
    Requirement,
    requirement,
)
from chip_chat.eval.golden.run import (
    DEFAULT_SESSION,
    SIGNAL_OF,
    Deployment,
    Judge,
    Observation,
    Signal,
    run_set,
)
from chip_chat.eval.golden.scoring import (
    COMPLETION_TARGET,
    GROUNDEDNESS_TARGET,
    TOOL_SELECTION_TARGET,
    CaseResult,
    LaneScore,
    Scores,
    Verdict,
    score,
)

__all__ = [
    "ANY_PERSONA",
    "COMPLETION_TARGET",
    "DEFAULT_MANIFEST",
    "DEFAULT_SESSION",
    "DELEGATIONS",
    "GROUNDEDNESS_TARGET",
    "JUDGED",
    "LANE_OF",
    "OUT_OF_SCOPE",
    "REQUIREMENTS",
    "SHAPES",
    "SIGNAL_OF",
    "TOOLS_IN",
    "TOOL_SELECTION_TARGET",
    "CaseError",
    "CaseResult",
    "Check",
    "Coverage",
    "Delegation",
    "Deployment",
    "GoldenCase",
    "GoldenSet",
    "Judge",
    "Lane",
    "LaneScore",
    "Observation",
    "Report",
    "Requirement",
    "Scores",
    "Shape",
    "Signal",
    "Verdict",
    "build_report",
    "coverage",
    "lane_of",
    "render",
    "requirement",
    "run_set",
    "score",
]
