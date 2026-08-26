"""Running the set against a deployment, and what a deployment has to report.

#29's third acceptance criterion is *a runner executes the set against any
deployment and reports per-lane pass rates*, and "any deployment" is the load
bearing half. A runner written against one code path measures that code path;
the seam here is :class:`Deployment`, which is two methods wide, and
:mod:`chip_chat.eval.golden.slice` is the first thing on the other side of it.

**A deployment says what it can report, and is scored on that.** This is the
part worth reading before writing a second adapter. Cilantro's chat reply today
is prose, a card and a receipt flag -- ``chip_chat.api.app.ChatReply`` -- and it
carries neither the tools that were called nor the citations behind the answer.
So an adapter over the HTTP surface *cannot* score tool selection, and an
adapter that quietly returned an empty tuple would score every routing case as a
miss and call it a result.

:data:`Signal` is the fix: a deployment declares which signals it reports, a
check whose signal is not reported is **unscored** rather than failed, and the
report prints the three outcomes apart. An unmeasured thing that reads as
unmeasured is worth having; one that reads as a failure sends somebody to debug
a model that was never asked the question. Bead ``cc-bap`` holds the work of
putting the tool calls and the citation ids on the reply, which is what turns
today's unscored columns into numbers.

**One case's failure is one case's failure.** A deployment that refuses the
eleventh case must not cost the other forty, so every case runs inside its own
``try`` and an adapter error becomes a recorded :attr:`Observation.error`.
:mod:`chip_chat.eval.golden.scoring` counts those apart from wrong answers: an
outage is not a model being wrong.

**The judge is a seam too, and there is nothing behind it yet.** Three of the
checks are judgements about meaning rather than properties of a payload -- see
:data:`~chip_chat.eval.golden.cases.JUDGED` -- and :class:`Judge` is where an
LLM-judge lands when #72 promotes this set into an Arize dataset with online
evals behind it. Until then those checks are unscored, and the report says so on
every run.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from chip_chat.eval.golden.cases import Check, GoldenCase, GoldenSet

__all__ = [
    "DEFAULT_SESSION",
    "SIGNAL_OF",
    "Deployment",
    "Judge",
    "Observation",
    "Signal",
    "run_set",
]

DEFAULT_SESSION: Final = "golden-set"
"""What a run's turns are grouped under when the caller names nothing else."""


class Signal(StrEnum):
    """Something a deployment may or may not be able to report about a turn.

    Attributes:
        TOOLS: Which tools were called. Everything about lane selection reads
            this, so a deployment that cannot report it cannot be scored on
            the metric the architecture exists to get right.
        CITATIONS: The citation ids on the response envelope -- ids the
            retriever returned, per D9, not a source line the model wrote.
        CARD: The structured confirmation card, where a turn produced one.
        RECEIPT: Whether what came back was a receipt rather than a draft.
        WRITES: Whether a write actually executed on this turn. Distinct from
            :attr:`RECEIPT`: a turn can be refused a write and still return
            something, and the launch gate is about the execution rather than
            about the widget.
    """

    TOOLS = "tools"
    CITATIONS = "citations"
    CARD = "card"
    RECEIPT = "receipt"
    WRITES = "writes"


SIGNAL_OF: Final[Mapping[Check, frozenset[Signal]]] = {
    Check.CITES: frozenset({Signal.CITATIONS}),
    Check.CITES_ADJACENT: frozenset({Signal.CITATIONS}),
    Check.CONFIRMS_FIRST: frozenset({Signal.CARD, Signal.WRITES}),
    Check.SIMULATED: frozenset({Signal.CARD}),
    Check.RECEIPT: frozenset({Signal.RECEIPT}),
    Check.EDITABLE: frozenset({Signal.CARD}),
    Check.NO_WRITE: frozenset({Signal.WRITES}),
}
"""What each deterministic check needs observed to be scoreable at all.

The judged checks are absent: what they need is a :class:`Judge`, not a signal.
"""


@dataclass(frozen=True, slots=True)
class Observation:
    """What a deployment made of one case.

    Every field but :attr:`case_id` and :attr:`reply` is meaningful only where
    the deployment declared the matching :class:`Signal`. Reading ``card=None``
    as "no card was rendered" on a deployment that does not report cards is the
    mistake this type is shaped to prevent, and :func:`scoring.score` does not
    make it: it consults ``reports`` first.

    Attributes:
        case_id: The case this answers, so a run and a set can be matched up
            after the fact without depending on order.
        reply: The prose the visitor saw. Carried for the report and for a
            judge; nothing deterministic reads it.
        tools: Tools called on the turn, in call order.
        citations: Citation ids on the response envelope.
        claim_class: What kind of claim the response made, as
            :class:`~chip_chat.agent.envelope.ClaimClass` spells it. Decides
            where a citation renders, which is what PRD K5 is about.
        card: The confirmation card or receipt, where one was rendered.
        receipt: Whether that card was a receipt.
        wrote: Whether a write executed.
        error: Why there is nothing here, in one line. ``None`` on success.
    """

    case_id: str
    reply: str = ""
    tools: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    claim_class: str | None = None
    card: Mapping[str, Any] | None = None
    receipt: bool = False
    wrote: bool = False
    error: str | None = None
    reports: frozenset[Signal] = field(default_factory=frozenset)
    """Which signals the deployment that produced this could report.

    Carried on the observation rather than only on the deployment, so that a
    run serialised to disk and scored later still knows what it did and did not
    measure.
    """

    @property
    def answered(self) -> bool:
        """Whether the deployment produced anything at all for this case."""
        return self.error is None


@runtime_checkable
class Deployment(Protocol):
    """Something the golden set can be run against.

    Two members, and the second one is the interesting one. Anything that can
    answer a visitor message and say what it observed while doing so is a
    deployment: the in-process week-one slice, a Foundry hosted agent, a URL,
    or a recorded transcript being re-scored under a new judge.
    """

    @property
    def name(self) -> str:
        """What answered, for the report. A model deployment, a URL, a build."""
        ...

    @property
    def reports(self) -> frozenset[Signal]:
        """Which signals this deployment can observe about a turn.

        Declared rather than inferred. A deployment that overstates this scores
        cases against fields it never filled in, which is worse than not
        scoring them -- so an adapter should list only what it has actually
        seen come back.
        """
        ...

    def turn(self, case: GoldenCase) -> Observation:
        """Run one case and report what happened.

        Args:
            case: The case. Its :attr:`~chip_chat.eval.golden.cases.GoldenCase.
                context` is prior assistant turns the message presupposes, and
                an adapter that drops them is measuring a different question.

        Returns:
            The observation. Raising is permitted -- :func:`run_set` records it
            against the case -- but returning an :class:`Observation` with an
            ``error`` is better where the adapter knows what went wrong.
        """
        ...


class Judge(Protocol):
    """Settles a check that no data structure can settle.

    Deliberately not implemented in this package. The judge is a model, it
    costs tokens, and picking one is #72's problem rather than #29's -- what
    #29 owes is a set with the judged checks *named*, so that the day a judge
    arrives it has something to attach to.
    """

    def verdict(
        self, check: Check, case: GoldenCase, observation: Observation
    ) -> bool | None:
        """Whether ``observation`` satisfies ``check``.

        Args:
            check: One of :data:`~chip_chat.eval.golden.cases.JUDGED`.
            case: What was asked, and why.
            observation: What came back.

        Returns:
            ``True`` or ``False`` where the judge is willing to say, and
            ``None`` where it is not -- which scores as unscored rather than as
            a failure. A judge that never returns ``None`` is a judge that
            guesses.
        """
        ...


def run_set(
    golden: GoldenSet,
    deployment: Deployment,
    *,
    only: Sequence[str] | None = None,
) -> tuple[Observation, ...]:
    """Run every case in the set against one deployment.

    Args:
        golden: The set to run.
        deployment: What to run it against.
        only: Case ids to run, for iterating on one case. ``None`` runs all.

    Returns:
        One :class:`Observation` per case run, in set order.
    """
    return tuple(_observations(golden, deployment, only))


def _observations(
    golden: GoldenSet, deployment: Deployment, only: Sequence[str] | None
) -> Iterator[Observation]:
    wanted = None if only is None else set(only)
    for case in golden:
        if wanted is not None and case.case_id not in wanted:
            continue
        yield _run_one(case, deployment)


def _run_one(case: GoldenCase, deployment: Deployment) -> Observation:
    """Run one case, turning an adapter failure into a recorded line.

    Broad by design, and narrow in what it does with what it catches. A
    deployment is a network, a model and somebody else's code; the failures it
    can produce are not enumerable from here, and a set that stopped on the
    eleventh case would have spent the first ten calls for nothing. What is
    *not* caught is the two that are never data about a case:
    ``KeyboardInterrupt`` and ``SystemExit`` do not inherit from ``Exception``
    and so pass straight through.
    """
    try:
        return deployment.turn(case)
    except Exception as error:  # see the docstring: a deployment is somebody else's code
        return Observation(
            case_id=case.case_id,
            error=f"{type(error).__name__}: {error}",
            reports=deployment.reports,
        )
