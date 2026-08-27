"""The register: what each row is owed, read off the checks the set already carries.

Nothing here invents a field. The point of every test below is that the three
booleans #75 needs -- should it answer, should it decline, must it cite -- are
readings of ``grounded``, ``declines`` and ``cites``, so there is no second copy
of the fact for the first one to disagree with.
"""

from dataclasses import replace

import pytest

from chip_chat.eval.dataset.build import Dataset, build_dataset
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.grounding.questions import Question, QuestionError, questions
from chip_chat.eval.photos.labels import LabeledSet


def _by_id(asked: tuple[Question, ...], entry_id: str) -> Question:
    for question in asked:
        if question.entry_id == entry_id:
            return question
    raise AssertionError(f"{entry_id} is not in the register")


def test_grounded_is_read_as_an_answer_being_owed(asked: tuple[Question, ...]) -> None:
    """A question the published data answers. Refusing it is the over-refusal."""
    question = _by_id(asked, "golden/k1-bowl-ingredients")

    assert question.answer_owed
    assert not question.refusal_owed
    assert question.citation_owed


def test_declines_is_read_as_a_refusal_being_owed(asked: tuple[Question, ...]) -> None:
    """A question the corpus cannot answer. Answering it is the under-refusal."""
    question = _by_id(asked, "golden/k3-halal-not-published")

    assert question.refusal_owed
    assert not question.answer_owed


def test_a_refusal_can_still_owe_a_citation(asked: tuple[Question, ...]) -> None:
    """The case #75 is about: the refusal has to show what it read.

    ``k3-allergen-safety-judgement`` declines *and* cites, so the two are
    independent booleans rather than one enumeration. A model that collapsed
    them would let a refusal out of PRD K2.
    """
    question = _by_id(asked, "golden/k3-allergen-safety-judgement")

    assert question.refusal_owed
    assert question.citation_owed
    assert question.adjacent_owed
    assert question.dietary


def test_a_row_the_set_is_silent_about_states_neither(
    asked: tuple[Question, ...],
) -> None:
    """*How many points do I have* is neither answerable-in-the-corpus nor not.

    It is still in the register -- PRD K2's count is over turns -- and it can
    fail the citation rule. It cannot fail a refusal in either direction, and
    saying so is what keeps the refusal rates off a denominator of silence.
    """
    question = _by_id(asked, "golden/a1-points-balance")

    assert not question.scores_refusal
    assert not question.citation_owed
    assert not question.scores_grounding


def test_the_stricter_category_holds_the_rows_that_declare_it(
    asked: tuple[Question, ...],
) -> None:
    """Allergen and dietary is a flag on the case, not a guess about the words."""
    dietary = {question.entry_id for question in asked if question.dietary}

    assert dietary == {
        "golden/k1-allergen-cheese",
        "golden/k3-preparation-not-published",
        "golden/k3-allergen-safety-judgement",
        "golden/k3-halal-not-published",
        "golden/k4-constrained-vegetarian",
        "golden/k4-constrained-no-dairy",
    }


def test_cross_contact_is_in_the_category_and_no_word_list_would_have_put_it_there(
    asked: tuple[Question, ...],
) -> None:
    """*Are the black beans cooked in the same pot as the chicken.*

    An allergen question with no allergen word in it. It is the reason the flag
    is declared rather than derived, and this test is what stops somebody
    replacing the flag with the word list later.
    """
    question = _by_id(asked, "golden/k3-preparation-not-published")

    assert question.dietary
    assert not {"soy", "dairy", "allergy"} & set(question.message.lower().split())


def test_photographs_are_not_questions(golden: GoldenSet, labels: LabeledSet) -> None:
    """A frame runs the vision lane directly, so there is no response to score.

    ``eval/README.md`` draws this line, and this is it holding on the side #75
    is on: thirty-one frames in the dataset, none of them in the register.
    """
    dataset = build_dataset(golden, labels)

    assert len(dataset.entries) > len(golden)
    assert len(questions(dataset)) == len(golden)


def test_a_dietary_row_that_states_nothing_is_refused(shipped: Dataset) -> None:
    """The stricter category is a promise about what gets measured.

    A row in it that carries none of the three checks would be held to nothing,
    and the report would say ``6 allergen and dietary rows`` over a set where
    one of them cannot fail. Refused at build, the way every register in
    ``eval/`` refuses a set that contradicts itself.
    """
    held = replace(
        shipped,
        entries=tuple(
            replace(entry, checks=(), judged_checks=())
            if entry.entry_id == "golden/k1-allergen-cheese"
            else entry
            for entry in shipped.entries
        ),
    )

    with pytest.raises(QuestionError, match="stricter category"):
        questions(held)
