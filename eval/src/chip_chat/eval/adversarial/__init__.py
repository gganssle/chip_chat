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

Issue #82 is the second half, and it is where the same suite is turned on the
launch gate itself: *zero cross-visitor data disclosures, including under
concurrency*. It added six attacks -- the shapes #30's suite did not hold -- and
two mechanisms, both below.

Ten modules, and the order they run in is the order to read them:

================ =============================================== ================
Module           What it holds                                   Answers
================ =============================================== ================
``attacks``      The suite: families, breaches, refusals          what is attempted
``canaries``     The secret that makes a disclosure countable     how it is detected
``soak``         How hot a round got, and who waited for what     was it a real test
``run``          Every attack through a target, some at once      what happened
``scoring``      Outcomes, and the two gates as counts            did it hold
``postmortem``   Where each attack died, and its trace            where it stopped
``coverage``     #30's and #82's scope, as clauses                is this the suite
``report``       The baseline, as Markdown                        written down
``slice``        The week-one loop, several visitors, one desk    against what
``testing``      Targets broken one way each                      does it catch anything
================ =============================================== ================

.. code-block:: python

    suite = AdversarialSuite.load(DEFAULT_MANIFEST)
    run = run_suite(suite, SliceTarget(model))
    print(render(build_report(suite, run)))

Five things this package will not do, each of which is a way an adversarial
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

**It will not count a concurrent round nobody had to share.** #82's addition,
and the sharper form of the overlap rule above: overlapping is not contending.
Three visitors against a pool of four overlap perfectly and never hand a
connection from one to another, so the bleed has no window to occur in and the
clean round is a fact about the arithmetic. A target declares how many
connections it pools -- :class:`~chip_chat.eval.adversarial.soak.Pooled`, and a
target that declares nothing is claiming it does not pool -- and a round that
offered no more turns than there are connections is unscored.
:class:`~chip_chat.eval.adversarial.testing.UncontendedTarget` is the fixture,
and it is the one in that file with nothing wrong with it.

**It will not average a gate.** PRD section 05 says *not "few" -- zero*. Both
gates are counts and :attr:`~chip_chat.eval.adversarial.scoring.Scores.gates_pass`
is a boolean-or-``None``, never a rate. Ninety-nine per cent of an adversarial
suite holding is not a gate nearly passing.

And one thing it now does that a bare outcome could not. **Every attack says
where it died.** ``held`` describes a design in which the model never reached for
a write tool and a design in which the model called it and was refused, and those
are not the same product.
:mod:`~chip_chat.eval.adversarial.postmortem` is that reading, derived from what
the target already reported rather than declared by it.
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
from chip_chat.eval.adversarial.postmortem import Postmortem, Stage, furthest, postmortem
from chip_chat.eval.adversarial.report import Report, build_report, render
from chip_chat.eval.adversarial.run import (
    DEFAULT_ROUNDS,
    SIGNAL_OF,
    Attempt,
    Control,
    Heat,
    Judge,
    Probe,
    Round,
    Run,
    Signal,
    Target,
    Window,
    run_concurrently,
    run_suite,
    run_sustained,
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
from chip_chat.eval.adversarial.soak import Pooled, Pressure, measure, slots_of

__all__ = [
    "CANARY_PREFIX",
    "CLAUSES",
    "DEFAULT_MANIFEST",
    "DEFAULT_ROUNDS",
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
    "Heat",
    "Judge",
    "Outcome",
    "Pooled",
    "Population",
    "Postmortem",
    "Pressure",
    "Probe",
    "Report",
    "Round",
    "Run",
    "Scores",
    "Signal",
    "Stage",
    "SuiteError",
    "Target",
    "Visitor",
    "Window",
    "build_report",
    "coverage",
    "furthest",
    "measure",
    "mint",
    "population",
    "postmortem",
    "render",
    "run_concurrently",
    "run_suite",
    "run_sustained",
    "score",
    "slots_of",
]
