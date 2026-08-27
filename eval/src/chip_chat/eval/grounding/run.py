"""Where a scored turn comes from: a response, its retrieval, and a judge.

:class:`TurnSource` is one method wide, for the reason
:class:`chip_chat.eval.golden.run.Deployment` is two: anything that can answer a
dataset row and hand back both halves of what #75 scores is a source. The
in-process slice (:mod:`chip_chat.eval.grounding.slice`), a hosted agent behind
a URL, or a recorded trace being re-scored under a better judge.

**Two halves, and they arrive from different places.** The response -- prose,
citation ids, claim class -- is what the deployment returned. The evidence is
what the *trace* says it retrieved. #75 asks for the judge to score against the
second, so a source that can produce only the first says so by leaving
:attr:`Turn.evidence` at ``None``, and every check that needs the passages comes
back unscored rather than assumed.

**A citation is a signal a deployment declares, not a field to be read
optimistically.** :class:`~chip_chat.eval.golden.run.Signal` is reused whole
rather than re-spelled here: ``CITATIONS`` is exactly *the citation ids on the
response envelope, per D9, not a source line the model wrote*, and it is the one
member this eval consults. Today no deployment in this repository declares it --
``chip_chat.agent.envelope`` is imported by no caller, bead ``cc-bap`` -- so the
citation rule comes back unscored on every row, which is a fact about wiring and
must not be reported as an agent that never cites.

**One row's failure is one row's failure.** A source that raises on the eleventh
row must not cost the other twenty-three, so every row runs inside its own
``try`` and an adapter error becomes a recorded :attr:`Turn.error`, which scores
as unscored and never as an ungrounded claim. An outage is not a model being
wrong.

**Live traffic arrives the other way round.** #75's first acceptance criterion
is *both evals running against the dataset and against live traces*, and the
seam for the second is not another method on this protocol -- it is
:func:`chip_chat.eval.grounding.scoring.score`, which takes questions and turns
as two matched sequences and has never been told where either came from. An
online runner assembles the pairs from a backend and calls it; the rules, the
gates and the per-category arithmetic are then the same code, which is the only
way the live number and the dataset number mean the same thing.
:mod:`chip_chat.eval.trajectory.run` makes the identical argument, because it is
the identical problem.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from chip_chat.agent.envelope import CITED_CLAIM_CLASSES, ClaimClass
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.grounding.evidence import Evidence
from chip_chat.eval.grounding.questions import Question

__all__ = ["Judge", "Turn", "TurnSource", "run_turns"]


@dataclass(frozen=True, slots=True)
class Turn:
    """What a deployment made of one row, and what its trace says it had.

    Attributes:
        entry_id: The row this answers, so a run and a register can be matched
            up after the fact without depending on order.
        reply: The prose the visitor saw. What a judge reads, and what is
            printed beside a failure.
        citations: Citation ids on the response envelope. Meaningful only where
            :attr:`reports` holds :attr:`~chip_chat.eval.golden.run.Signal.
            CITATIONS`; reading an empty tuple as *"it cited nothing"* on a
            deployment that cannot report citations is the mistake this field's
            neighbour exists to prevent.
        claim_class: What kind of claim the response made, as
            :class:`~chip_chat.agent.envelope.ClaimClass` spells it. Decides
            whether PRD K2 requires a citation at all: *"you have 1,250
            points"* is grounded in Snowflake and a source link on it would be
            decoration.
        dropped_citations: Ids the model named that the retriever did not
            return on this turn --
            :attr:`~chip_chat.agent.envelope.ResponseEnvelope.
            dropped_citation_ids`, whose docstring says these are what #75
            counts. A minted source is a violation rather than a nuisance, and
            it is invisible in the citations array because the renderer already
            threw it away.
        evidence: What the turn's ``retriever.search`` spans say it had.
            ``None`` where the source does not read span trees at all, which is
            different from a source that read one and found no spans -- that
            comes back as an :class:`~chip_chat.eval.grounding.evidence.
            Evidence` carrying an error.
        reports: Which signals the deployment that produced this could report.
            Carried on the turn rather than only on the source, so that a run
            serialised to disk and scored later still knows what it did and did
            not measure.
        card: The confirmation card, where one was rendered. Carried for the
            report rather than scored here -- PRD T2 is the golden set's and the
            adversarial suite's business.
        error: Why there is nothing here, in one line. ``None`` on success.
    """

    entry_id: str
    reply: str = ""
    citations: tuple[str, ...] = ()
    claim_class: str = ""
    dropped_citations: tuple[str, ...] = ()
    evidence: Evidence | None = None
    reports: frozenset[Signal] = field(default_factory=frozenset)
    card: Mapping[str, Any] | None = None
    error: str | None = None

    @property
    def answered(self) -> bool:
        """Whether the deployment produced anything at all for this row."""
        return self.error is None

    @property
    def reports_citations(self) -> bool:
        """Whether the citation fields on this turn mean anything."""
        return Signal.CITATIONS in self.reports

    @property
    def declared_class(self) -> ClaimClass | None:
        """The claim class, or ``None`` where the response named none we know."""
        try:
            return ClaimClass(self.claim_class)
        except ValueError:
            return None

    @property
    def claims_needing_citation(self) -> bool:
        """Whether the response declared a claim PRD K2 requires a citation on."""
        return self.declared_class in CITED_CLAIM_CLASSES

    @property
    def has_evidence(self) -> bool:
        """Whether the turn's retrieval can be read and returned something."""
        evidence = self.evidence
        return evidence is not None and evidence.readable and evidence.retrieved


@runtime_checkable
class TurnSource(Protocol):
    """Something #75 can be run against."""

    @property
    def name(self) -> str:
        """What answered, for the report. A model deployment, a URL, a build."""
        ...

    @property
    def reports(self) -> frozenset[Signal]:
        """Which signals this source can observe about a turn.

        Declared rather than inferred. A source that overstates this scores
        rows against fields it never filled in, which is worse than not scoring
        them.
        """
        ...

    def turn(self, question: Question) -> Turn:
        """Run one row and report what happened.

        Args:
            question: The row. Its message is what the turn has to be run with,
                and a source that drops the case's context is measuring a
                different question.

        Returns:
            The turn. Raising is permitted -- :func:`run_turns` records it
            against the row -- but returning a :class:`Turn` carrying an
            ``error`` is better where the source knows what went wrong.
        """
        ...


class Judge(Protocol):
    """Settles the two things about a response that no data structure can.

    Deliberately not implemented in this package, exactly as
    :class:`chip_chat.eval.golden.run.Judge` is not. A judge is a model, it
    costs tokens, and choosing one belongs to #76's online evals rather than
    here. What #75 owes is the two questions *named and scoreable*, so that the
    day a judge arrives it has something to attach to -- and, in the meantime, a
    report that says which numbers are missing rather than one that quietly
    prints a keyword rule's opinion as a groundedness score.
    """

    def grounded(self, question: Question, turn: Turn) -> bool | None:
        """Whether every food or policy claim in the reply is supported.

        Args:
            question: What was asked, and why.
            turn: What came back, including
                :attr:`Turn.evidence` -- the passages the turn really had.
                Scoring against anything else answers a different question; see
                :mod:`chip_chat.eval.grounding.evidence`.

        Returns:
            ``True`` or ``False`` where the judge is willing to say, and
            ``None`` where it is not -- which scores as unscored rather than as
            a failure. A judge that never returns ``None`` is a judge that
            guesses.
        """
        ...

    def refused(self, question: Question, turn: Turn) -> bool | None:
        """Whether the reply declined rather than answering.

        The direction is not asked here. *Refusing* is a property of the text;
        whether refusing was **right** is a property of the row, and
        :mod:`chip_chat.eval.grounding.verdicts` is where the two are put
        together. Asking a judge which way round it was would hand it the
        register and let it grade its own answer.

        Args:
            question: What was asked.
            turn: What came back.

        Returns:
            ``True`` where the reply declines, ``False`` where it answers, and
            ``None`` where the judge will not say.
        """
        ...


def run_turns(
    questions: Sequence[Question],
    source: TurnSource,
    *,
    only: Sequence[str] | None = None,
) -> tuple[Turn, ...]:
    """Run every row against one source.

    Args:
        questions: The rows to run, in dataset order.
        source: What to run them against.
        only: Entry ids to run, for iterating on one row. ``None`` runs all.

    Returns:
        One turn per row run, in dataset order.
    """
    return tuple(_turns(questions, source, only))


def _turns(
    questions: Sequence[Question],
    source: TurnSource,
    only: Sequence[str] | None,
) -> Iterator[Turn]:
    wanted = None if only is None else set(only)
    for question in questions:
        if wanted is not None and question.entry_id not in wanted:
            continue
        yield _run_one(question, source)


def _run_one(question: Question, source: TurnSource) -> Turn:
    """Run one row, turning a source failure into a recorded line.

    Broad by design, and narrow in what it does with what it catches: a source
    is a network, a model and somebody else's code. What is *not* caught is the
    two that are never data about a row -- ``KeyboardInterrupt`` and
    ``SystemExit`` do not inherit from ``Exception`` and pass straight through.
    """
    try:
        return source.turn(question)
    except Exception as error:  # a source is somebody else's code; see the docstring
        return Turn(
            entry_id=question.entry_id,
            error=f"{type(error).__name__}: {error}",
            reports=source.reports,
        )
