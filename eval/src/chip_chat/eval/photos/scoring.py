"""Component-level precision, recall and F1 -- and the two stages worth scoring.

The PRD's metric is *photo → order, component-level F1 ≥ 0.80*, and issue #56
spends a paragraph on why it is component-level: *"a system that nails rice and
beans while guessing protein is a different problem from one that is uniformly
mediocre"*. So the unit here is a ``(slot, term)`` pair rather than a meal, and
a wrong protein costs twice -- one pair the pipeline missed and one it invented.
That is the correct price. A meal right in five slots and wrong in one is not
simply wrong, and it is also not five-sixths right on the slot that failed.

Two stages, and reporting only one of them would answer the wrong question
--------------------------------------------------------------------------

The photo lane has a model in it and a set of confidence floors under it, and
those fail differently:

``described``
    Stage 4's slots, exactly as the model returned them, before any floor. This
    measures **the model**: the prompt, the deployment, the image size.

``believed``
    :attr:`~chip_chat.vision.matcher.Resolution.seen` -- the slots stage 5 was
    willing to act on, after each floor. This measures **the pipeline**, and it
    is the PRD's number, because a slot below its floor never reaches an order.

The gap between them is precisely what the floors cost. A floor that is too
high shows up as ``believed`` recall falling away from ``described`` recall
with the precision barely moving; a floor that is too low shows up as the two
being identical and precision being poor in both. Issue #54 shipped those
floors as an argument from what each mistake costs, and said in as many words
that *"these are the numbers issue #56 exists to move"*. One aggregate F1 could
not move them; this pair, per slot, can.

What is not scored, and why
---------------------------

**Slots the photograph does not answer.** :attr:`~chip_chat.eval.photos.labels.
PhotoLabel.unreadable` -- a wrapped burrito's rice -- is dropped from both the
truth set and the prediction set. Neither credit nor penalty, because there is
no fact to be right about. What *is* reported, separately, is how often the
model filled one anyway: a describer confidently naming the rice inside a
sealed foil wrapper is guessing, and that is worth a line in the report even
though it cannot be a false positive.

**Components on a frame that is not one Chipotle-style meal.** Those frames
have no per-meal ground truth at all -- see
:mod:`chip_chat.eval.photos.labels`. They are scored on whether the *behaviour*
was right, which :class:`DetectionScore` and :class:`OutcomeScore` do.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from chip_chat.catalog.records import Slot
from chip_chat.eval.photos.labels import LabeledSet, PhotoLabel
from chip_chat.eval.photos.run import PhotoRun
from chip_chat.vision.matcher import Outcome

__all__ = [
    "F1_TARGET",
    "ComponentScore",
    "DetectionScore",
    "OutcomeScore",
    "Scores",
    "SlotScore",
    "Stage",
    "expected_outcome",
    "score",
    "slot_rows",
]

F1_TARGET: Final = 0.80
"""PRD §05: *photo → order, component-level F1 ≥ 0.80*.

Checked against the ``believed`` stage, because that is the one whose slots
reach an order. Reported for ``described`` as well, since a gap between them is
a floor to retune rather than a model to replace.
"""


class Stage(StrEnum):
    """Which of the lane's two outputs a component score is over.

    Attributes:
        DESCRIBED: Stage 4's slots, before any confidence floor. The model.
        BELIEVED: Stage 5's believed slots, after every floor. The pipeline,
            and the PRD's metric.
    """

    DESCRIBED = "described"
    BELIEVED = "believed"


@dataclass(frozen=True, slots=True)
class SlotScore:
    """One slot's tally, and the three numbers computed from it.

    Each of :attr:`precision`, :attr:`recall` and :attr:`f1` is ``None`` where
    the quantity is undefined rather than zero. A slot the pipeline never
    predicted has no precision -- reporting ``0.0`` would say it was always
    wrong, and reporting ``1.0`` would say it was always right, and both are
    claims about predictions that do not exist.

    Attributes:
        slot: Which slot, or ``None`` for a micro-averaged total.
        true_positives: Pairs in both the label and the prediction.
        false_positives: Pairs predicted and not labeled.
        false_negatives: Pairs labeled and not predicted.
    """

    slot: Slot | None
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def predicted(self) -> int:
        """How many pairs the pipeline produced for this slot."""
        return self.true_positives + self.false_positives

    @property
    def labeled(self) -> int:
        """How many pairs the set says are there."""
        return self.true_positives + self.false_negatives

    @property
    def precision(self) -> float | None:
        """Of what it named, how much was there. ``None`` if it named nothing."""
        return self.true_positives / self.predicted if self.predicted else None

    @property
    def recall(self) -> float | None:
        """Of what is there, how much it named. ``None`` if nothing is labeled."""
        return self.true_positives / self.labeled if self.labeled else None

    @property
    def f1(self) -> float | None:
        """Their harmonic mean, or ``None`` where the slot was never in play.

        Computed from the tally rather than from :attr:`precision` and
        :attr:`recall`, which is the same number and stays defined in the case
        where one of them is not: a slot that is labeled and never predicted
        has no precision and an F1 of zero, and zero is the honest answer.
        """
        denominator = (
            2 * self.true_positives + self.false_positives + self.false_negatives
        )
        return 2 * self.true_positives / denominator if denominator else None

    def __add__(self, other: "SlotScore") -> "SlotScore":
        """Micro-sum two tallies. The result belongs to no single slot."""
        return SlotScore(
            slot=None,
            true_positives=self.true_positives + other.true_positives,
            false_positives=self.false_positives + other.false_positives,
            false_negatives=self.false_negatives + other.false_negatives,
        )


@dataclass(frozen=True, slots=True)
class ComponentScore:
    """One stage's component-level result: per slot, and micro-averaged.

    Attributes:
        stage: Which output this scored. See :class:`Stage`.
        per_slot: One tally per slot in schema order, including slots the set
            never labeled -- a missing row would read as a slot that was fine.
        photos: How many frames contributed. Only frames that are one
            Chipotle-style meal do; see the module docstring.
        unreadable_filled: How often the pipeline named a slot the photograph
            does not answer. Not an error, and not scored as one -- but a
            describer that does it often is guessing rather than reading.
    """

    stage: Stage
    per_slot: Mapping[Slot, SlotScore]
    photos: int
    unreadable_filled: int

    @property
    def overall(self) -> SlotScore:
        """Every slot's tally summed. The PRD's component-level number."""
        total = SlotScore(
            slot=None, true_positives=0, false_positives=0, false_negatives=0
        )
        for score_ in self.per_slot.values():
            total = total + score_
        return total

    @property
    def meets_target(self) -> bool:
        """Whether :attr:`overall` reaches :data:`F1_TARGET`.

        ``False`` when the F1 is ``None``, which is the case for a run over no
        frames. Nothing was measured, so nothing was met.
        """
        f1 = self.overall.f1
        return f1 is not None and f1 >= F1_TARGET


@dataclass(frozen=True, slots=True)
class DetectionScore:
    """One binary behaviour, scored in both directions with the frames named.

    ``meals_visible`` carries the whole multi-meal decision, and issue #56's
    comment is explicit about why both directions are reported: *"a false
    positive costs a working order; a false negative costs a fabricated one"*.
    A single accuracy figure hides which of those is happening, and they are not
    equally bad.

    Attributes:
        event: What the positive class is, for the report.
        true_positives: Frames where the event is real and was detected.
        false_positives: Frames where it was detected and is not real -- the
            ordinary photograph that gets refused.
        false_negatives: Frames where it is real and was missed -- the frame
            that goes on to be built into something.
        false_positive_ids: Which frames those were. Named rather than counted,
            because retuning a detector means looking at the frames it failed.
        false_negative_ids: Likewise.
    """

    event: str
    true_positives: int
    false_positives: int
    false_negatives: int
    false_positive_ids: tuple[str, ...] = ()
    false_negative_ids: tuple[str, ...] = ()

    @property
    def tally(self) -> SlotScore:
        """The same three counts, so the P/R/F1 arithmetic is not written twice."""
        return SlotScore(
            slot=None,
            true_positives=self.true_positives,
            false_positives=self.false_positives,
            false_negatives=self.false_negatives,
        )

    @property
    def precision(self) -> float | None:
        """Of the frames it flagged, how many should have been."""
        return self.tally.precision

    @property
    def recall(self) -> float | None:
        """Of the frames it should have flagged, how many it did."""
        return self.tally.recall

    @property
    def f1(self) -> float | None:
        """Their harmonic mean, or ``None`` where the event never came up."""
        return self.tally.f1


@dataclass(frozen=True, slots=True)
class OutcomeScore:
    """Whether each frame took the path issue #55 says it should.

    Stage 5 has four outcomes and three of them are the photo cases #55 built:
    food this restaurant does not serve, a component nobody could read, several
    meals in one frame. Scoring the outcome is what makes those *measured*
    rather than assumed -- the bead's own words.

    Attributes:
        confusion: ``(expected, observed)`` to how many frames did that. A
            mapping rather than a count, because which way a frame went wrong
            is the finding; "82% correct" is not.
        wrong: The frames that did not take the expected path, as
            ``(photo_id, expected, observed)``, in set order.
        scored: How many frames had an outcome at all -- a frame the lane
            declined on has none.
    """

    confusion: Mapping[tuple[Outcome, Outcome], int]
    wrong: tuple[tuple[str, Outcome, Outcome], ...]
    scored: int

    @property
    def correct(self) -> int:
        """Frames whose outcome was the expected one."""
        return sum(
            count
            for (expected, observed), count in self.confusion.items()
            if expected is observed
        )

    @property
    def accuracy(self) -> float | None:
        """The share that took the expected path, or ``None`` over no frames."""
        return self.correct / self.scored if self.scored else None


@dataclass(frozen=True, slots=True)
class Scores:
    """Everything one run over the set produced.

    Attributes:
        described: Component score before the floors. The model.
        believed: Component score after them. The pipeline, and the PRD metric.
        several_meals: Multi-meal detection, both directions.
        not_chipotle: Detection of food this restaurant does not serve.
        outcomes: Which path each frame took.
        errors: Frames the lane could not answer for at all, as
            ``(photo_id, message)``. Counted apart from every score above,
            because a deployment that is down is not a model that is wrong --
            RFC-001 section 10's declining lane -- and folding the two together
            would report an outage as poor accuracy.
    """

    described: ComponentScore
    believed: ComponentScore
    several_meals: DetectionScore
    not_chipotle: DetectionScore
    outcomes: OutcomeScore
    errors: tuple[tuple[str, str], ...]


def expected_outcome(label: PhotoLabel) -> Outcome:
    """What stage 5 should conclude about this photograph.

    Derived from the label rather than written on it, so that the expectation
    cannot drift from the ground truth it is supposed to follow from.

    Args:
        label: The ground truth for one frame.

    Returns:
        The outcome, in the order stage 5 itself decides:

        * not the food this restaurant serves →
          :attr:`~chip_chat.vision.matcher.Outcome.NOT_ORDERABLE`;
        * two or more orderable meals →
          :attr:`~chip_chat.vision.matcher.Outcome.SEVERAL_MEALS`, which is
          checked before resolution because the decision record says the gate
          sits there;
        * a required slot the photograph does not answer →
          :attr:`~chip_chat.vision.matcher.Outcome.CLARIFY`. PRD V5: ask, do
          not guess -- and here the *label* is what says there is nothing to
          guess from;
        * otherwise :attr:`~chip_chat.vision.matcher.Outcome.RESOLVED`.
    """
    if not label.is_chipotle_style:
        return Outcome.NOT_ORDERABLE
    if label.several_meals:
        return Outcome.SEVERAL_MEALS
    if label.unreadable_required():
        return Outcome.CLARIFY
    return Outcome.RESOLVED


def score(labels: LabeledSet, runs: Sequence[PhotoRun]) -> Scores:
    """Score one run over the set.

    Args:
        labels: The ground truth.
        runs: What the lane produced, one per frame. Runs whose ``photo_id`` is
            not in the set are ignored; frames with no run are treated as
            errors, since a frame the experiment silently skipped must not read
            as a frame it got right.

    Returns:
        The :class:`Scores`.
    """
    by_id = {run.photo_id: run for run in runs}
    errors: list[tuple[str, str]] = []
    for label in labels:
        run = by_id.get(label.photo_id)
        if run is None:
            errors.append((label.photo_id, "no run recorded for this photograph"))
        elif run.error is not None:
            errors.append((label.photo_id, run.error))

    return Scores(
        described=_components(labels, by_id, Stage.DESCRIBED),
        believed=_components(labels, by_id, Stage.BELIEVED),
        several_meals=_detection(
            labels,
            by_id,
            event="several meals in frame",
            truth=lambda label: label.several_meals,
            observed=lambda run: (
                run.description is not None and run.description.meal.several_meals
            ),
        ),
        not_chipotle=_detection(
            labels,
            by_id,
            event="not food this restaurant serves",
            truth=lambda label: not label.is_chipotle_style,
            observed=lambda run: (
                run.description is not None and not run.description.meal.is_chipotle_style
            ),
        ),
        outcomes=_outcomes(labels, by_id),
        errors=tuple(errors),
    )


def _components(
    labels: LabeledSet,
    by_id: Mapping[str, PhotoRun],
    stage: Stage,
) -> ComponentScore:
    """Tally ``(slot, term)`` pairs over every frame that has components."""
    tallies = {slot: {"tp": 0, "fp": 0, "fn": 0} for slot in Slot}
    photos = 0
    unreadable_filled = 0
    for label in labels:
        if not label.orderable:
            continue
        photos += 1
        run = by_id.get(label.photo_id)
        predicted = _predicted(run, stage)
        unreadable_filled += sum(1 for slot, _ in predicted if slot in label.unreadable)
        # Dropped from both sides, not just from the truth: a term the model
        # produced for a slot nobody could read is neither right nor wrong.
        predicted = {pair for pair in predicted if pair[0] not in label.unreadable}
        truth = set(label.pairs())
        for slot, _ in truth & predicted:
            tallies[slot]["tp"] += 1
        for slot, _ in predicted - truth:
            tallies[slot]["fp"] += 1
        for slot, _ in truth - predicted:
            tallies[slot]["fn"] += 1

    return ComponentScore(
        stage=stage,
        per_slot={
            slot: SlotScore(
                slot=slot,
                true_positives=counts["tp"],
                false_positives=counts["fp"],
                false_negatives=counts["fn"],
            )
            for slot, counts in tallies.items()
        },
        photos=photos,
        unreadable_filled=unreadable_filled,
    )


def _predicted(run: PhotoRun | None, stage: Stage) -> set[tuple[Slot, str]]:
    """The ``(slot, term)`` pairs one stage of one run produced.

    An absent run, or one that raised, predicts nothing -- which scores as a
    false negative on every labeled pair. That is the right price for a lane
    that could not answer, and :attr:`Scores.errors` says why it could not so
    that an outage is not read as an inaccuracy.
    """
    if run is None:
        return set()
    if stage is Stage.DESCRIBED:
        if run.description is None:
            return set()
        return {(Slot(name), value.value) for name, value in run.description.meal.slots()}
    if run.resolution is None:
        return set()
    return {(seen.slot, seen.term) for seen in run.resolution.seen}


def _detection(
    labels: LabeledSet,
    by_id: Mapping[str, PhotoRun],
    *,
    event: str,
    truth: Callable[[PhotoLabel], bool],
    observed: Callable[[PhotoRun], bool],
) -> DetectionScore:
    """Score one binary behaviour over the whole set, naming the failures."""
    tp = fp = 0
    false_positives: list[str] = []
    false_negatives: list[str] = []
    for label in labels:
        run = by_id.get(label.photo_id)
        real = truth(label)
        # A frame the lane could not answer for did not detect anything. It is
        # a false negative where the event was real, and simply not a detection
        # where it was not -- an outage must not become a false positive.
        detected = run is not None and observed(run)
        if real and detected:
            tp += 1
        elif detected:
            fp += 1
            false_positives.append(label.photo_id)
        elif real:
            false_negatives.append(label.photo_id)
    return DetectionScore(
        event=event,
        true_positives=tp,
        false_positives=fp,
        false_negatives=len(false_negatives),
        false_positive_ids=tuple(false_positives),
        false_negative_ids=tuple(false_negatives),
    )


def _outcomes(labels: LabeledSet, by_id: Mapping[str, PhotoRun]) -> OutcomeScore:
    """Compare each frame's expected path with the one it took."""
    confusion: dict[tuple[Outcome, Outcome], int] = {}
    wrong: list[tuple[str, Outcome, Outcome]] = []
    scored = 0
    for label in labels:
        run = by_id.get(label.photo_id)
        if run is None or run.resolution is None:
            continue
        scored += 1
        expected = expected_outcome(label)
        observed = run.resolution.outcome
        confusion[(expected, observed)] = confusion.get((expected, observed), 0) + 1
        if expected is not observed:
            wrong.append((label.photo_id, expected, observed))
    return OutcomeScore(confusion=confusion, wrong=tuple(wrong), scored=scored)


def slot_rows(score_: ComponentScore) -> Iterable[SlotScore]:
    """Every slot's tally in schema order, then the micro total.

    What a report iterates. Kept here rather than in
    :mod:`chip_chat.eval.photos.report` so that a caller rendering the numbers
    some other way -- a notebook, an Arize experiment -- gets the same order.
    """
    for slot in Slot:
        yield score_.per_slot[slot]
    yield score_.overall
