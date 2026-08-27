"""The numbers: top-3 recall, MRR, top-1 precision, and the two things beside them.

#50 asks for three metrics per category per configuration, and says why in a
sentence worth keeping: *report numbers, not impressions*. What follows is the
definition of each, because a recall figure with no definition attached is an
impression with a decimal point on it.

**Recall is counted over labels, not over chunks.** A question's ground truth is
the *places* that answer it -- see
:mod:`chip_chat.eval.retrieval.questions` -- and ``recall@3`` is how many of
them appeared in the top three passages. Counting chunks instead has two
failure modes and this has one. A label like ``item_type=Bowl`` resolves to
every bowl row in the corpus, so chunk-level recall would put a nine-row
denominator on one question and let it dominate the category mean; and any
question with more than three relevant chunks would have a ceiling below 1.0
that reads as a failing score. The one this has is that a label matching nine
interchangeable rows is satisfied by any one of them -- which is the correct
reading of *"what beans do you serve"*, and the wrong reading of a question
where the nine were genuinely all needed. The set holds none of the latter, and
:mod:`chip_chat.eval.retrieval.coverage` is where that stays true.

**A label that does not resolve is in no numerator and no denominator.** The
corpus under test may not carry the place a label names -- a slice of the
corpus, an allergen caveat the harvest has not published yet. Scoring that as a
miss would blame a retriever for a harvest. So the denominator is
:meth:`~chip_chat.eval.retrieval.corpus.Resolution.scored_labels`, and the
unresolved ones are counted out loud above the rates. This is the golden set's
third verdict, applied to ground truth rather than to checks.

**Both recall@3 and hit@3 are reported, and the pair is the interesting part.**
``recall@3`` is the proportion of a question's places that came back; ``hit@3``
is whether *any* did. They differ only on the questions with more than one
place, and where they differ, they say different true things: for
*"which has fewer calories, the chicken bowl or the steak burrito"* a hit is a
half-answer, and for *"can I cancel an order"* -- where two published entries
answer the same question about two channels -- a hit is enough to answer and
both is better. Neither number subsumes the other, so neither is dropped.

**A question with more than three places has a ceiling**, and the report prints
it rather than letting the reader assume 1.0 was available. Three slots cannot
hold four places.

**The negative set is scored on restraint, and restraint is not accuracy.** The
correct behaviour for a question the corpus cannot answer is a retrieval that is
not :attr:`~chip_chat.search.retrieve.Confidence.GROUNDED` -- #49's own
requirement, and PRD K3's. It is kept in its own table because averaging it into
recall would let a retriever that returns nothing for everything score well on
half the set.

**Constraints are scored on the filter, not on the ranking.** #49 answers
*"without any dairy"* with an OData filter, and a filter is exact. Two things
are checked: that the constraint was read out of the sentence at all, and that
every returned passage honours it. The second is the one that matters and it is
scored as a count of *violations* rather than as a rate, for the reason the
adversarial suite keeps its gates as counts: one item wrongly offered to
somebody avoiding dairy is not diluted by nineteen offered correctly.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from chip_chat.eval.retrieval.configurations import Configuration
from chip_chat.eval.retrieval.corpus import Resolution, fields_of
from chip_chat.eval.retrieval.questions import Category, Question, RetrievalSet
from chip_chat.eval.retrieval.run import Answer
from chip_chat.search.retrieve import Confidence

__all__ = [
    "RECALL_AT",
    "ArmScores",
    "CategoryScores",
    "ConstraintScore",
    "Judgement",
    "NegativeScore",
    "score_arm",
    "score_sweep",
]

RECALL_AT: Final = 3
"""The ``k`` the demo criterion is stated at: *top-3 recall on your allergen
questions, measured, with numbers*.

Three rather than the retriever's :data:`chip_chat.search.query.TOP` of five,
and the gap is deliberate. The sweep asks for five so that a place which came
back fourth is *visible* -- :attr:`Judgement.first_rank` records it -- while the
score is taken at the three the ticket names. A metric and a fetch size that
were the same number would make "it was there, at rank four" unreportable.
"""


@dataclass(frozen=True, slots=True)
class Judgement:
    """One question under one arm, judged.

    Attributes:
        question: What was asked.
        arm: Which configuration answered.
        labels: The question's labels that resolve against the corpus. The
            recall denominator, and empty on a question this corpus cannot
            support.
        found_at: For each resolved label, the one-based rank of the first
            passage that is it, or ``None`` where it never came back.
        first_rank: The rank of the first passage satisfying *any* label, or
            ``None``. What MRR is computed from.
        returned: How many passages came back.
        confidence: What the retriever said about them.
        violations: Passages that came back in breach of the question's
            constraint, by id. Empty where the question has none.
        constraint_read: Whether the constraint was read out of the sentence,
            or ``None`` where the question carries none.
        skew: Passages satisfying a label whose id the corpus export does not
            hold. Should be zero; see :mod:`chip_chat.eval.retrieval.corpus`.
        error: Why nothing came back, where the source failed.
    """

    question: Question
    arm: Configuration
    labels: tuple[str, ...] = ()
    found_at: Mapping[str, int | None] = field(default_factory=dict)
    first_rank: int | None = None
    returned: int = 0
    confidence: Confidence = Confidence.NONE
    violations: tuple[str, ...] = ()
    constraint_read: bool | None = None
    skew: int = 0
    error: str | None = None

    @property
    def scored(self) -> bool:
        """Whether this question's ranking can be scored under this arm at all."""
        return self.error is None and bool(self.labels)

    @property
    def recalled(self) -> int:
        """How many resolved labels appeared within :data:`RECALL_AT`."""
        return sum(
            1 for rank in self.found_at.values() if rank is not None and rank <= RECALL_AT
        )

    @property
    def recall(self) -> float | None:
        """Proportion of this question's places found in the top three."""
        return None if not self.scored else self.recalled / len(self.labels)

    @property
    def hit(self) -> bool | None:
        """Whether any of them was found in the top three."""
        return None if not self.scored else self.recalled > 0

    @property
    def reciprocal_rank(self) -> float | None:
        """``1 / rank`` of the first relevant passage anywhere in what came back."""
        if not self.scored:
            return None
        return 0.0 if self.first_rank is None else 1.0 / self.first_rank

    @property
    def precise(self) -> bool | None:
        """Whether the very first passage was one of this question's places."""
        return None if not self.scored else self.first_rank == 1

    @property
    def ceiling(self) -> float:
        """The best ``recall@3`` this question could have scored.

        Below 1.0 exactly when it has more than three places, because three
        slots cannot hold four. Printed rather than assumed: a category whose
        mean recall is 0.83 because one of its questions could not exceed 0.75
        is not a category with a retrieval problem.
        """
        return 1.0 if not self.labels else min(1.0, RECALL_AT / len(self.labels))

    @property
    def restrained(self) -> bool | None:
        """Whether an unanswerable question was answered without confidence.

        ``None`` on an answerable question and on one nothing came back for --
        a source failure is not restraint.
        """
        if self.question.answerable or self.error is not None:
            return None
        return self.confidence is not Confidence.GROUNDED


@dataclass(frozen=True, slots=True)
class CategoryScores:
    """One category's questions under one arm.

    Every rate here is a **macro** average -- the mean over questions, each
    counting once, rather than over labels. A question with three places would
    otherwise weigh three times as much as one with a single place, which would
    make the rewards category a report about ``rew-points-expire``.

    Attributes:
        category: Which category.
        judgements: Its answerable, label-carrying questions under this arm.
    """

    category: Category
    judgements: tuple[Judgement, ...]

    @property
    def total(self) -> int:
        """Questions in this category that carry places at all."""
        return len(self.judgements)

    @property
    def scored(self) -> int:
        """Those whose places resolve against the corpus under test."""
        return sum(1 for judgement in self.judgements if judgement.scored)

    @property
    def unscored(self) -> int:
        """Those the corpus cannot support, or the source failed on."""
        return self.total - self.scored

    @property
    def recall(self) -> float | None:
        """Mean ``recall@3``, or ``None`` where nothing could be scored."""
        return self._mean([j.recall for j in self.judgements if j.recall is not None])

    @property
    def hit_rate(self) -> float | None:
        """Proportion of questions with any place in the top three."""
        return self._mean(
            [1.0 if j.hit else 0.0 for j in self.judgements if j.hit is not None]
        )

    @property
    def mrr(self) -> float | None:
        """Mean reciprocal rank of the first relevant passage."""
        return self._mean(
            [j.reciprocal_rank for j in self.judgements if j.reciprocal_rank is not None]
        )

    @property
    def precision_at_one(self) -> float | None:
        """Proportion of questions whose first passage was relevant."""
        return self._mean(
            [1.0 if j.precise else 0.0 for j in self.judgements if j.precise is not None]
        )

    @property
    def ceiling(self) -> float:
        """The best mean ``recall@3`` this category's questions allow."""
        scored = [j for j in self.judgements if j.scored]
        return 1.0 if not scored else sum(j.ceiling for j in scored) / len(scored)

    @staticmethod
    def _mean(values: Sequence[float]) -> float | None:
        return None if not values else sum(values) / len(values)


@dataclass(frozen=True, slots=True)
class NegativeScore:
    """The negative set under one arm. Restraint, not accuracy.

    Attributes:
        judgements: The unanswerable questions, in set order.
    """

    judgements: tuple[Judgement, ...]

    @property
    def total(self) -> int:
        """How many unanswerable questions were asked."""
        return len(self.judgements)

    @property
    def scored(self) -> int:
        """Those something came back for."""
        return sum(1 for j in self.judgements if j.restrained is not None)

    @property
    def restrained(self) -> int:
        """Those answered without confidence, which is the correct behaviour."""
        return sum(1 for j in self.judgements if j.restrained)

    @property
    def rate(self) -> float | None:
        """Proportion answered without confidence, or ``None``."""
        return None if not self.scored else self.restrained / self.scored

    def overconfident(self) -> tuple[Judgement, ...]:
        """Every question the retriever reported as grounded and could not answer.

        The list to read first. Each of these is a passage the agent would be
        told it may draw an answer from, about a question the published corpus
        does not cover -- which is the *plausible near-miss* #49 exists to make
        impossible and the failure mode #50 exists to catch before a model
        starts paraphrasing over it.
        """
        return tuple(j for j in self.judgements if j.restrained is False)


@dataclass(frozen=True, slots=True)
class ConstraintScore:
    """The constrained questions under one arm. Counts, never rates.

    Attributes:
        judgements: The questions carrying a constraint, in set order.
    """

    judgements: tuple[Judgement, ...]

    @property
    def total(self) -> int:
        """How many constrained questions were asked."""
        return len(self.judgements)

    @property
    def read(self) -> int:
        """How many had their constraint read out of the sentence."""
        return sum(1 for j in self.judgements if j.constraint_read)

    @property
    def violations(self) -> int:
        """Passages returned in breach of a constraint, across every question.

        A count rather than a rate, and it does not average with anything. One
        item carrying a published dairy mark, offered to somebody who said they
        cannot have dairy, is not made acceptable by nineteen that did not.
        """
        return sum(len(j.violations) for j in self.judgements)

    def breached(self) -> tuple[Judgement, ...]:
        """Every constrained question that returned something it should not have."""
        return tuple(j for j in self.judgements if j.violations)


@dataclass(frozen=True, slots=True)
class ArmScores:
    """One configuration, over the whole set.

    Attributes:
        arm: Which configuration.
        categories: Per-category scores in
            :class:`~chip_chat.eval.retrieval.questions.Category` order, empty
            categories included -- a category that lost its
            questions has to be visible as an absence rather than as an
            omission.
        negatives: The negative set.
        constraints: The constrained questions.
        judgements: Every judgement under this arm, in set order.
    """

    arm: Configuration
    categories: tuple[CategoryScores, ...]
    negatives: NegativeScore
    constraints: ConstraintScore
    judgements: tuple[Judgement, ...]

    @property
    def allergens(self) -> CategoryScores:
        """The allergen category. **The demo bar**, by name rather than by index.

        #50's demo criterion is stated on this one category, so the report and
        anything downstream reach it here rather than by filtering a tuple --
        a criterion found by a search that quietly returns nothing is a
        criterion that quietly stops being measured.
        """
        for scores in self.categories:
            if scores.category is Category.ALLERGENS:
                return scores
        raise KeyError("the allergen category is not in this run")  # pragma: no cover

    @property
    def recall(self) -> float | None:
        """Mean ``recall@3`` over every scored question, whatever its category.

        Computed from the questions rather than from the categories, so a
        category with two questions does not weigh as much as one with eight.
        The per-category table is the one to read; this exists so the arms have
        a single comparable number in the summary row.
        """
        values = [j.recall for j in self.judgements if j.recall is not None]
        return None if not values else sum(values) / len(values)

    @property
    def errors(self) -> tuple[Judgement, ...]:
        """Every question this arm could not run at all."""
        return tuple(j for j in self.judgements if j.error is not None)

    @property
    def skew(self) -> int:
        """Passages returned that the corpus export does not hold. Should be zero."""
        return sum(j.skew for j in self.judgements)


def score_arm(
    questions: RetrievalSet,
    resolution: Resolution,
    answers: Sequence[Answer],
    arm: Configuration,
) -> ArmScores:
    """Score one arm of the sweep.

    Args:
        questions: The labeled set.
        resolution: The labels, resolved against the corpus under test.
        answers: Every answer from the sweep. Answers from other arms are
            ignored, so a caller may pass the whole run.
        arm: The arm to score.

    Returns:
        Its :class:`ArmScores`. Questions with no answer under this arm are
        skipped rather than failed -- they were not run.
    """
    by_id = {
        answer.question_id: answer for answer in answers if answer.arm.name == arm.name
    }
    judgements = tuple(
        _judge(question, resolution, by_id[question.question_id], arm)
        for question in questions
        if question.question_id in by_id
    )
    return ArmScores(
        arm=arm,
        categories=tuple(
            CategoryScores(
                category=category,
                judgements=tuple(
                    j
                    for j in judgements
                    if j.question.category is category and j.question.ranked
                ),
            )
            for category in Category
        ),
        negatives=NegativeScore(
            judgements=tuple(j for j in judgements if not j.question.answerable)
        ),
        constraints=ConstraintScore(
            judgements=tuple(j for j in judgements if j.question.constraint is not None)
        ),
        judgements=judgements,
    )


def score_sweep(
    questions: RetrievalSet,
    resolution: Resolution,
    answers: Sequence[Answer],
    configurations: Sequence[Configuration],
) -> tuple[ArmScores, ...]:
    """Score every arm, in the order the ablation names them.

    Args:
        questions: The labeled set.
        resolution: The labels, resolved against the corpus under test.
        answers: Everything the sweep produced.
        configurations: The arms that were run.

    Returns:
        One :class:`ArmScores` per arm.
    """
    return tuple(score_arm(questions, resolution, answers, arm) for arm in configurations)


def _judge(
    question: Question,
    resolution: Resolution,
    answer: Answer,
    arm: Configuration,
) -> Judgement:
    """Judge one question under one arm."""
    labels = resolution.scored_labels(question)
    names = tuple(label.describe() for label in labels)
    if answer.retrieval is None:
        return Judgement(
            question=question,
            arm=arm,
            labels=names,
            found_at=dict.fromkeys(names),
            error=answer.error or "the source returned nothing",
        )

    retrieval = answer.retrieval
    passages = retrieval.passages
    published = [fields_of(passage) for passage in passages]

    found: dict[str, int | None] = {}
    for label, name in zip(labels, names, strict=True):
        found[name] = next(
            (
                rank
                for rank, fields in enumerate(published, start=1)
                if label.matches(fields)
            ),
            None,
        )
    ranks = [rank for rank in found.values() if rank is not None]

    known = resolution.ids()
    skew = sum(
        1
        for passage, fields in zip(passages, published, strict=True)
        if passage.id not in known and any(label.matches(fields) for label in labels)
    )

    constraint = question.constraint
    violations: tuple[str, ...] = ()
    constraint_read: bool | None = None
    if constraint is not None:
        constraint_read = set(constraint.without_allergens).issubset(
            set(retrieval.constraints.without_allergens)
        )
        violations = tuple(
            passage.id
            for passage, fields in zip(passages, published, strict=True)
            if not constraint.honoured_by(fields)
        )

    return Judgement(
        question=question,
        arm=arm,
        labels=names,
        found_at=found,
        first_rank=min(ranks) if ranks else None,
        returned=len(passages),
        confidence=retrieval.confidence,
        violations=violations,
        constraint_read=constraint_read,
        skew=skew,
        error=None if retrieval.answered else retrieval.declined,
    )
