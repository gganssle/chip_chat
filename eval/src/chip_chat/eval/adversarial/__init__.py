"""The adversarial suite: the two launch gates, attacked rather than assumed.

Issue #30 in one sentence: *the two PRD launch gates are pass or fail and block
the demo going public regardless of how everything else scores, so the suite that
verifies them should exist from the beginning and run continuously rather than be
assembled in Phase 10 when the hardening checklist is finally reached.*

Both gates are **structural** properties of RFC-001's design. Identity is bound
at the database session and absent from every tool signature; the confirmation
flag lives on a record no tool can reach. Nothing here is meant to *establish*
those properties -- #43, #44 and #63 do that. What is here verifies them, which
means anything above zero is a broken mechanism rather than a bad day.

Eight modules, and the order they run in is the order to read them:

================ =============================================== ================
Module           What it holds                                   Answers
================ =============================================== ================
``attacks``      The suite: families, breaches, refusals          what is attempted
``canaries``     The secret that makes a disclosure countable     how it is detected
``run``          Every attack through a target, some at once      what happened
``scoring``      Outcomes, and the two gates as counts            did it hold
``coverage``     #30's scope, as clauses                          is this the suite
``report``       The baseline, as Markdown                        written down
``slice``        The week-one loop, several visitors, one desk    against what
``testing``      Targets broken one way each                      does it catch anything
================ =============================================== ================

.. code-block:: python

    suite = AdversarialSuite.load(DEFAULT_MANIFEST)
    run = run_suite(suite, SliceTarget(model))
    print(render(build_report(suite, run)))

Four things this package will not do, each of which is a way an adversarial
suite quietly stops being one. The first is the whole design and the rest follow
from it.

**It will not let unscored read as safe.** The golden set treats an unmeasured
check as neutral and moves on, which is right when the question is *how well did
it do*. Here the question is *did anything get out*, and *"we could not tell"*
and *"nothing did"* are the same shade of green unless something refuses to make
them one. So an unscored attack **blocks** its gate:
:attr:`~chip_chat.eval.adversarial.scoring.Gate.passes` is ``None``, the report
prints **not measured** in bold, and a gate that was not measured has not passed.

**It will not believe an attack it never really ran.** Three preconditions, and
each is a way a suite scores a clean sheet against a target it never asked a
question. An attack names the :class:`~chip_chat.eval.adversarial.attacks.
Capability` it leans on, and a target that lacks it makes the attack unscored --
today's deployment serves one hardcoded account to every visitor, so *"show me
Sam's order history"* is unanswerable in both directions there. A **positive
control** checks that each visitor can see their own canary before any other
visitor's failure to obtain it counts for anything, because a target that answers
*"I'm not sure"* to everything scores a perfect zero disclosures. And a
concurrent attack must be shown to have actually overlapped, because a loop that
uses threads and gets its answers back one at a time is a sequential test.

**It will not ship without the concurrency test.** RFC-001 section 05: *session
variables and pooled connections are a classic combination for cross-tenant
bleed... sequential tests will pass regardless.* A suite missing that attack is
not thin, it is wrong -- it reports zero disclosures on a deployment that bleeds
every connection it hands out. So
:meth:`~chip_chat.eval.adversarial.attacks.AdversarialSuite.load` refuses the
manifest, and no number is produced at all.

**It will not average a gate.** PRD section 05 says *not "few" -- zero*. Both
gates are counts and :attr:`~chip_chat.eval.adversarial.scoring.Scores.gates_pass`
is a boolean-or-``None``, never a rate. Ninety-nine per cent of an adversarial
suite holding is not a gate nearly passing.
"""

from chip_chat.eval.adversarial.attacks import (
    DEFAULT_MANIFEST,
    FOREIGN_CANARY,
    JUDGED,
    AdversarialSuite,
    Attack,
    Breach,
    Capability,
    Carrier,
    Family,
    SuiteError,
)
from chip_chat.eval.adversarial.canaries import (
    CANARY_PREFIX,
    Canary,
    Population,
    Visitor,
    mint,
    population,
)
from chip_chat.eval.adversarial.coverage import CLAUSES, Clause, Coverage, coverage
from chip_chat.eval.adversarial.report import Report, build_report, render
from chip_chat.eval.adversarial.run import (
    SIGNAL_OF,
    Attempt,
    Control,
    Judge,
    Probe,
    Run,
    Signal,
    Target,
    Window,
    run_concurrently,
    run_suite,
)
from chip_chat.eval.adversarial.scoring import (
    GATES,
    AttackScore,
    AttemptResult,
    FamilyScore,
    Gate,
    GateSpec,
    Outcome,
    Scores,
    score,
)

__all__ = [
    "CANARY_PREFIX",
    "CLAUSES",
    "DEFAULT_MANIFEST",
    "FOREIGN_CANARY",
    "GATES",
    "JUDGED",
    "SIGNAL_OF",
    "AdversarialSuite",
    "Attack",
    "AttackScore",
    "Attempt",
    "AttemptResult",
    "Breach",
    "Canary",
    "Capability",
    "Carrier",
    "Clause",
    "Control",
    "Coverage",
    "Family",
    "FamilyScore",
    "Gate",
    "GateSpec",
    "Judge",
    "Outcome",
    "Population",
    "Probe",
    "Report",
    "Run",
    "Scores",
    "Signal",
    "SuiteError",
    "Target",
    "Visitor",
    "Window",
    "build_report",
    "coverage",
    "mint",
    "population",
    "render",
    "run_concurrently",
    "run_suite",
    "score",
]
