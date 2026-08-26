"""The arithmetic, driven at the size it will really run at.

A scorer is easy to get subtly wrong and hard to notice being wrong: every
version of it returns a plausible number between nought and one. So every test
here starts from a run that is right by construction -- a describer answering
each frame with exactly what its label says -- introduces one known departure,
and checks that the one cell that should have moved is the one that did.

The two-stage split is what most of these are about. ``described`` is the
model's answer before any floor and ``believed`` is what stage 5 would act on
after them, and issue #54 shipped those floors as an argument rather than a
measurement. The test that raises one floor until a slot falls through it is the
shape of the tuning run this package exists to make possible.
"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from chip_chat.catalog.records import Slot
from chip_chat.eval.photos.labels import LabeledSet
from chip_chat.eval.photos.run import PhotoRun, run_set
from chip_chat.eval.photos.scoring import (
    F1_TARGET,
    Scores,
    Stage,
    expected_outcome,
    score,
)
from chip_chat.eval.photos.testing import (
    FIXTURE_CONFIDENCES,
    ScriptedVisionModel,
    lane_over,
    truthful_answers,
)
from chip_chat.vision.describe import DescribeUnavailableError
from chip_chat.vision.matcher import Outcome, SlotRule, SlotRules


def _run(
    labels: LabeledSet,
    answers: Sequence[str],
    *,
    rules: SlotRules | None = None,
    errors: dict[int, Exception] | None = None,
) -> tuple[PhotoRun, ...]:
    model = ScriptedVisionModel(answers, errors=errors or {})
    return run_set(labels, lane_over(labels, model, rules=rules))


def _scored(
    labels: LabeledSet,
    answers: Sequence[str],
    *,
    rules: SlotRules | None = None,
    errors: dict[int, Exception] | None = None,
) -> Scores:
    return score(labels, _run(labels, answers, rules=rules, errors=errors))


def _index_of(labels: LabeledSet, photo_id: str) -> int:
    return [label.photo_id for label in labels].index(photo_id)


def _labeled_pairs(labels: LabeledSet) -> int:
    """Every component pair the set claims, over the frames that have components."""
    return sum(len(label.pairs()) for label in labels if label.orderable)


# --- the run that is right by construction ----------------------------------


def test_a_describer_that_agrees_with_every_label_scores_one(
    labels: LabeledSet, spans: object
) -> None:
    scores = _scored(labels, truthful_answers(labels))

    for stage in (scores.described, scores.believed):
        assert stage.overall.false_positives == 0
        assert stage.overall.false_negatives == 0
        assert stage.overall.true_positives == _labeled_pairs(labels)
        assert stage.overall.f1 == 1.0
        assert stage.meets_target
    assert scores.errors == ()
    assert scores.outcomes.wrong == ()
    assert scores.outcomes.accuracy == 1.0


def test_only_single_meal_frames_contribute_components(
    labels: LabeledSet, spans: object
) -> None:
    """The frames with no per-meal truth are scored on behaviour, not on slots."""
    scores = _scored(labels, truthful_answers(labels))
    orderable = sum(1 for label in labels if label.orderable)

    assert scores.described.photos == orderable
    assert orderable < len(labels)


# --- one mistake at a time --------------------------------------------------


def test_a_wrong_protein_costs_a_miss_and_an_invention(
    labels: LabeledSet, spans: object
) -> None:
    """The price issue #56 argues for: one pair missed, one pair invented.

    Not "one meal wrong". A bowl right in five slots and wrong in one is not
    simply wrong, and it is not five-sixths right on the slot that failed
    either.
    """
    answers = truthful_answers(labels)
    index = _index_of(labels, "frame-00")  # a chicken bowl
    payload = json.loads(answers[index])
    payload["protein"] = {"value": "steak", "confidence": 0.88}
    answers[index] = json.dumps(payload)

    scores = _scored(labels, answers)
    protein = scores.described.per_slot[Slot.PROTEIN]

    assert protein.false_positives == 1
    assert protein.false_negatives == 1
    assert protein.true_positives == _protein_pairs(labels) - 1
    assert scores.described.overall.false_positives == 1
    assert scores.described.overall.false_negatives == 1


def _protein_pairs(labels: LabeledSet) -> int:
    return sum(
        1
        for label in labels
        if label.orderable
        for slot, _ in label.pairs()
        if slot is Slot.PROTEIN
    )


def test_a_slot_the_model_leaves_out_is_a_miss_and_not_an_invention(
    labels: LabeledSet, spans: object
) -> None:
    answers = truthful_answers(labels)
    index = _index_of(labels, "frame-00")
    payload = json.loads(answers[index])
    del payload["toppings"]
    answers[index] = json.dumps(payload)

    toppings = _scored(labels, answers).described.per_slot[Slot.TOPPINGS]

    assert toppings.false_negatives == 1
    assert toppings.false_positives == 0


def test_a_topping_that_is_not_there_is_an_invention_and_not_a_miss(
    labels: LabeledSet, spans: object
) -> None:
    answers = truthful_answers(labels)
    index = _index_of(labels, "frame-00")
    payload = json.loads(answers[index])
    payload["toppings"] = [
        *payload["toppings"],
        {"value": "guacamole", "confidence": 0.79},
    ]
    answers[index] = json.dumps(payload)

    toppings = _scored(labels, answers).described.per_slot[Slot.TOPPINGS]

    assert toppings.false_positives == 1
    assert toppings.false_negatives == 0


def test_a_slot_the_photograph_does_not_answer_is_scored_in_neither_direction(
    labels: LabeledSet, spans: object
) -> None:
    """The wrapped burrito, and the reason it is not simply excluded.

    Filling the rice slot on a sealed foil wrapper is a guess. It cannot be a
    false positive -- there is no fact to contradict -- but a describer that
    does it is not reading the photograph, so the count is reported rather than
    discarded.
    """
    answers = truthful_answers(labels)
    index = _index_of(labels, "frame-16")  # contents hidden
    payload = json.loads(answers[index])
    payload["rice"] = {"value": "white_rice", "confidence": 0.9}
    answers[index] = json.dumps(payload)

    scores = _scored(labels, answers)

    assert scores.described.per_slot[Slot.RICE].false_positives == 0
    assert scores.described.per_slot[Slot.RICE].false_negatives == 0
    assert scores.described.unreadable_filled == 1


# --- what the floors cost, which is the number #54 is waiting for -----------


def test_a_floor_above_the_model_shows_up_as_a_gap_between_the_two_stages(
    labels: LabeledSet, spans: object
) -> None:
    """Raise the beans floor past what the model reports, and watch the cost.

    ``described`` does not move: the model said what it said. ``believed`` loses
    every beans pair, because stage 5 will not act on a slot below its floor.
    That difference, per slot, is what tuning a floor is reading.
    """
    rules = SlotRules(
        rules={
            **SlotRules.defaults().rules,
            Slot.BEANS: SlotRule(
                floor=FIXTURE_CONFIDENCES[Slot.BEANS] + 0.1, required=True
            ),
        }
    )
    scores = _scored(labels, truthful_answers(labels), rules=rules)

    assert scores.described.per_slot[Slot.BEANS].f1 == 1.0
    assert scores.believed.per_slot[Slot.BEANS].true_positives == 0
    assert scores.believed.per_slot[Slot.BEANS].false_negatives > 0
    assert scores.believed.per_slot[Slot.BEANS].false_positives == 0
    believed = scores.believed.overall.f1
    described = scores.described.overall.f1
    assert believed is not None
    assert described is not None
    assert believed < described


def test_a_required_slot_below_its_floor_becomes_a_question(
    labels: LabeledSet, spans: object
) -> None:
    """The other half of the same change: a frame that should resolve now asks."""
    rules = SlotRules(
        rules={
            **SlotRules.defaults().rules,
            Slot.BEANS: SlotRule(
                floor=FIXTURE_CONFIDENCES[Slot.BEANS] + 0.1, required=True
            ),
        }
    )
    scores = _scored(labels, truthful_answers(labels), rules=rules)
    took = {photo_id: observed for photo_id, _, observed in scores.outcomes.wrong}

    assert took["frame-00"] is Outcome.CLARIFY


# --- the count that the whole multi-meal behaviour rests on -----------------


def test_a_single_meal_called_several_is_named_as_a_false_positive(
    labels: LabeledSet, spans: object
) -> None:
    """The likeliest one, on the most ordinary photograph anyone will send."""
    answers = truthful_answers(labels)
    index = _index_of(labels, "frame-24")  # a bowl beside a bag of chips
    payload = json.loads(answers[index])
    payload["meals_visible"] = 2
    answers[index] = json.dumps(payload)

    detection = _scored(labels, answers).several_meals

    assert detection.false_positive_ids == ("frame-24",)
    assert detection.false_negative_ids == ()


def test_several_meals_called_one_is_named_as_a_false_negative(
    labels: LabeledSet, spans: object
) -> None:
    """The expensive direction: a well-formed order nobody in the frame asked for."""
    answers = truthful_answers(labels)
    index = _index_of(labels, "frame-26")
    payload = json.loads(answers[index])
    payload["meals_visible"] = 1
    payload["vessel"] = {"value": "bowl", "confidence": 0.95}
    payload["protein"] = {"value": "chicken", "confidence": 0.88}
    payload["rice"] = {"value": "white_rice", "confidence": 0.72}
    payload["beans"] = {"value": "black_beans", "confidence": 0.66}
    answers[index] = json.dumps(payload)

    scores = _scored(labels, answers)

    assert scores.several_meals.false_negative_ids == ("frame-26",)
    assert ("frame-26", Outcome.SEVERAL_MEALS, Outcome.RESOLVED) in scores.outcomes.wrong


def test_food_this_restaurant_does_not_serve_is_scored_as_its_own_event(
    labels: LabeledSet, spans: object
) -> None:
    answers = truthful_answers(labels)
    index = _index_of(labels, "frame-29")
    payload = json.loads(answers[index])
    payload["is_chipotle_style"] = True
    answers[index] = json.dumps(payload)

    detection = _scored(labels, answers).not_chipotle

    assert detection.false_negative_ids == ("frame-29",)
    assert detection.true_positives == 1  # frame-30 still detected


# --- the expected path, derived rather than written down --------------------


@pytest.mark.parametrize(
    ("photo_id", "outcome"),
    [
        ("frame-00", Outcome.RESOLVED),
        ("frame-16", Outcome.CLARIFY),
        ("frame-26", Outcome.SEVERAL_MEALS),
        ("frame-29", Outcome.NOT_ORDERABLE),
    ],
)
def test_the_expected_outcome_follows_from_the_label(
    labels: LabeledSet, photo_id: str, outcome: Outcome
) -> None:
    """One frame per path, so #55's three branches are measured rather than assumed."""
    label = next(item for item in labels if item.photo_id == photo_id)

    assert expected_outcome(label) is outcome


# --- an outage is not an inaccuracy -----------------------------------------


def test_a_frame_the_lane_declines_on_is_recorded_apart_from_the_scores(
    labels: LabeledSet, spans: object
) -> None:
    scores = _scored(
        labels,
        truthful_answers(labels),
        errors={0: DescribeUnavailableError("the deployment is down")},
    )

    assert scores.errors[0][0] == "frame-00"
    assert "deployment is down" in scores.errors[0][1]
    # It cost the frame's components, which is honest -- nothing was produced.
    assert scores.described.overall.false_negatives == len(labels.photos[0].pairs())
    # And it did not become a detection false positive, which would be a lie.
    assert scores.several_meals.false_positive_ids == ()


def test_a_frame_with_no_run_at_all_is_an_error_and_not_a_pass(
    labels: LabeledSet,
) -> None:
    """A silently skipped frame must not read as a frame that went well."""
    scores = score(labels, [])

    assert len(scores.errors) == len(labels)
    assert scores.described.overall.true_positives == 0
    assert not scores.described.meets_target


# --- the target -------------------------------------------------------------


def test_the_target_is_read_off_the_believed_stage(
    labels: LabeledSet, spans: object
) -> None:
    """A run whose model is right and whose floors throw half of it away.

    The PRD's metric is *photo → order*, so it is the slots that reach an order
    that count -- and a report quoting the described figure here would say the
    target was met by a pipeline that does not meet it.
    """
    rules = SlotRules(
        rules={
            slot: SlotRule(floor=0.99, required=rule.required)
            for slot, rule in SlotRules.defaults().rules.items()
        }
    )
    scores = _scored(labels, truthful_answers(labels), rules=rules)

    assert scores.described.meets_target
    assert not scores.believed.meets_target
    assert scores.believed.overall.f1 is not None
    assert scores.believed.overall.f1 < F1_TARGET


def test_a_slot_nothing_was_predicted_for_has_no_precision(
    labels: LabeledSet, spans: object
) -> None:
    """An em dash, never a nought. Zero would read as a slot that always failed."""
    scores = _scored(labels, truthful_answers(labels))
    per_slot = scores.described.per_slot

    for slot in Slot:
        row = per_slot[slot]
        if row.predicted == 0 and row.labeled == 0:
            assert row.precision is None
            assert row.recall is None
            assert row.f1 is None


def test_the_stage_labels_say_which_table_is_which(labels: LabeledSet) -> None:
    scores = score(labels, [])

    assert scores.described.stage is Stage.DESCRIBED
    assert scores.believed.stage is Stage.BELIEVED


def test_the_run_puts_each_frame_in_its_own_turn(
    labels: LabeledSet, spans: object, tmp_path: Path
) -> None:
    """RFC-001 section 09's tree, once per photograph.

    Both halves of the lane under one ``tool.match_meal_from_photo``, which is
    what issue #64's criterion asks for -- and one ``chat.turn`` per frame
    rather than one turn thirty steps deep, because a photograph is a turn.
    """
    _run(labels, truthful_answers(labels))
    names = spans.names()  # type: ignore[attr-defined]

    assert names.count("chat.turn") == len(labels)
    assert names.count("tool.match_meal_from_photo") == len(labels)
    assert names.count("vision.describe") == len(labels)
