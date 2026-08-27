"""Every question through a retriever, once per arm, one failure at a time.

The seam is one method wide, for the reason
:mod:`chip_chat.eval.trajectory.run` gives about trace sources: anything that
can answer a question and hand back a
:class:`~chip_chat.search.retrieve.Retrieval` is a source -- the live alias, an
in-memory index over a committed corpus, or a recording being re-scored after a
label changed.

**It runs the retriever, not the lane and not the agent.** #50's title is the
argument: *evaluate the retriever on its own, before it ever touches the agent*.
:class:`~chip_chat.search.retrieve.Retriever` is the single interface the tool
layer calls, so it is the widest thing that can be scored with no model in the
loop at all. :class:`~chip_chat.search.lane.KnowledgeLane` is one layer further
out and adds a ``retriever.search`` span that the OTel schema requires to sit
inside a ``tool.*`` span -- so running the lane here would mean opening a tool
call for a tool nobody selected, which is a trace that describes something that
did not happen. The lane's other job, turning an outage into a declining lane,
is measured by ``search/tests`` rather than by a sweep of forty questions.

**One question's failure is one question's failure.** A service that refuses the
eleventh question must not cost the other thirty-nine, so every question runs
inside its own ``try`` and a :class:`~chip_chat.search.errors.SearchError`
becomes a recorded :attr:`Answer.error`. Scoring counts those apart from
everything else: an outage is not a retriever ranking badly, and
:mod:`chip_chat.eval.photos.run` makes the same move for the same reason.

**The arms run question-major, not arm-major.** All four configurations for one
question, then the next question. It changes no number and it changes what a
half-finished sweep is worth: interrupted arm-major, you have one complete arm
and three empty ones; interrupted question-major, you have four comparable arms
over the questions that ran, which is the shape the whole ablation is for.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from chip_chat.eval.retrieval.configurations import ABLATION, Configuration
from chip_chat.eval.retrieval.questions import Question, RetrievalSet
from chip_chat.search.errors import SearchError
from chip_chat.search.retrieve import Retrieval, Retriever

__all__ = ["Answer", "RetrievalSource", "RetrieverSource", "run_sweep"]


@runtime_checkable
class RetrievalSource(Protocol):
    """Something that can answer one question under one configuration."""

    @property
    def name(self) -> str:
        """What answered, for the report. An alias, a corpus release, a URL."""
        ...

    def retrieve(self, question: Question, arm: Configuration) -> Retrieval:
        """Run one question under one arm.

        Args:
            question: The question. Its :attr:`~Question.text` is sent verbatim.
            arm: Which halves, and whether to rerank.

        Returns:
            The retrieval. Raising is permitted -- :func:`run_sweep` records it
            against the question -- but a source that knows what went wrong
            should prefer a retrieval whose ``declined`` says so.
        """
        ...


class RetrieverSource:
    """A :class:`RetrievalSource` over a real
    :class:`~chip_chat.search.retrieve.Retriever`.

    The whole adapter, and it is four lines, which is the point: the thing being
    evaluated is the retriever, and anything more here would be a second
    retrieval layer for the report to be about.

    ``top`` is the retriever's own :data:`chip_chat.search.query.TOP` unless a
    caller says otherwise. Five rather than three even though the headline
    metric is a top-3 one: a sweep that asked for exactly three could never
    report the rank of a passage that came fourth, and *the answer was there and
    it was fourth* is a different finding from *it was not there*, with a
    different fix.
    """

    __slots__ = ("_name", "_retriever", "_top")

    def __init__(
        self, retriever: Retriever, *, name: str = "", top: int | None = None
    ) -> None:
        """Point a source at a retriever.

        Args:
            retriever: The retriever. Built once, because the connection pool
                inside it is the whole of this path's latency budget.
            name: What to call it in the report. Defaults to the alias.
            top: Passages to ask for. ``None`` uses the retriever's own.
        """
        self._retriever = retriever
        self._name = name or retriever.alias
        self._top = top

    @property
    def name(self) -> str:
        """What answered, for the report."""
        return self._name

    def retrieve(self, question: Question, arm: Configuration) -> Retrieval:
        """Run one question under one arm."""
        return self._retriever.search(
            question.text, top=self._top, rerank=arm.rerank, halves=arm.halves
        )


@dataclass(frozen=True, slots=True)
class Answer:
    """What one arm made of one question, or why there is nothing.

    Attributes:
        question_id: The question this answers, so a run and a set can be
            matched up without depending on order.
        arm: The configuration that produced it.
        retrieval: What came back, or ``None`` where the source failed.
        error: Why there is nothing, in one line. ``None`` on success.
    """

    question_id: str
    arm: Configuration
    retrieval: Retrieval | None = None
    error: str | None = None

    @property
    def answered(self) -> bool:
        """Whether this arm produced a retrieval at all for this question."""
        return self.error is None and self.retrieval is not None


def run_sweep(
    questions: RetrievalSet,
    source: RetrievalSource,
    *,
    configurations: Sequence[Configuration] = ABLATION,
    only: Sequence[str] | None = None,
) -> tuple[Answer, ...]:
    """Run every question under every configuration.

    Args:
        questions: The labeled set.
        source: What to run them against.
        configurations: The arms to sweep. Defaults to
            :data:`~chip_chat.eval.retrieval.configurations.ABLATION`; a caller
            that only wants the serving arm passes ``(SERVING,)`` and spends a
            quarter of the semantic allowance.
        only: Question ids to run, for iterating on one question. ``None`` runs
            all.

    Returns:
        One :class:`Answer` per question per arm, question-major and then in
        ``configurations`` order.
    """
    return tuple(_answers(questions, source, configurations, only))


def _answers(
    questions: RetrievalSet,
    source: RetrievalSource,
    configurations: Sequence[Configuration],
    only: Sequence[str] | None,
) -> Iterator[Answer]:
    wanted = None if only is None else set(only)
    for question in questions:
        if wanted is not None and question.question_id not in wanted:
            continue
        for arm in configurations:
            yield _run_one(question, arm, source)


def _run_one(question: Question, arm: Configuration, source: RetrievalSource) -> Answer:
    """Run one question under one arm, turning a source failure into a line.

    :class:`~chip_chat.search.errors.SearchError` is caught because it is the
    documented way this layer says *the service refused*, and a refusal is a
    fact about a run rather than about a question. Everything else propagates: a
    ``KeyboardInterrupt`` or a bug in the selector is not a datum about the
    corpus and must not be recorded as one.
    """
    try:
        return Answer(
            question_id=question.question_id,
            arm=arm,
            retrieval=source.retrieve(question, arm),
        )
    except SearchError as error:
        return Answer(
            question_id=question.question_id,
            arm=arm,
            error=f"{type(error).__name__}: {error}",
        )
