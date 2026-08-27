"""The arithmetic, driven against numbers computed by hand.

Every case here starts from a retrieval built in the test rather than from a
run, because what is under test is the definition of a metric and a definition
is checkable only against a number somebody worked out. The end-to-end sweep is
``test_retrieval_run.py``.
"""

from chip_chat.eval.retrieval.configurations import ABLATION, HYBRID, RERANKED
from chip_chat.eval.retrieval.corpus import Place, Resolution
from chip_chat.eval.retrieval.questions import (
    Category,
    Constraint,
    Label,
    Question,
    RetrievalSet,
)
from chip_chat.eval.retrieval.run import Answer
from chip_chat.eval.retrieval.scoring import RECALL_AT, score_arm
from chip_chat.search.retrieve import Confidence, Passage, Retrieval

CHEESE = Label(kind="MENU_ITEM", fields={"item_id": "CMG-5252"}, why="cheese")
CHIPS = Label(kind="MENU_ITEM", fields={"item_id": "CMG-1002"}, why="chips")
GUAC = Label(kind="MENU_ITEM", fields={"item_id": "CMG-1001"}, why="guac")


def passage(item_id: str, *, allergens: tuple[str, ...] = ()) -> Passage:
    """One passage carrying just enough for a selector and a constraint."""
    return Passage(
        id=f"chunk-{item_id}",
        text=f"{item_id}.",
        heading=item_id,
        kind="MENU_ITEM",
        source_url="https://example.invalid/menu",
        harvested_at="2026-01-01T12:00:00+00:00",
        score=1.0,
        published={
            "item_id": item_id,
            "allergens": list(allergens),
            "allergen_disclosure": "PUBLISHED",
        },
    )


def question(
    *labels: Label,
    answerable: bool = True,
    constraint: Constraint | None = None,
    question_id: str = "q",
) -> Question:
    return Question(
        question_id=question_id,
        text="a question",
        category=Category.ALLERGENS,
        answerable=answerable,
        relevant=labels,
        constraint=constraint,
        why="a test",
    )


def scored(
    question_: Question,
    passages: tuple[Passage, ...],
    *,
    confidence: Confidence = Confidence.GROUNDED,
    resolved: tuple[bool, ...] | None = None,
    without_allergens: tuple[str, ...] = (),
):
    """Score one question under one arm, with its labels resolved as told."""
    from chip_chat.search.query import Constraints

    flags = (True,) * len(question_.relevant) if resolved is None else resolved
    resolution = Resolution(
        places=tuple(
            Place(
                question_id=question_.question_id,
                label=label,
                chunk_ids=(f"chunk-for-{index}",) if flag else (),
            )
            for index, (label, flag) in enumerate(
                zip(question_.relevant, flags, strict=True)
            )
        ),
        run_id="test",
        chunks=99,
    )
    retrieval = Retrieval(
        query=question_.text,
        passages=passages,
        confidence=confidence,
        constraints=Constraints(without_allergens=without_allergens),
    )
    arm = score_arm(
        RetrievalSet(questions=(question_,)),
        resolution,
        (Answer(question_id=question_.question_id, arm=HYBRID, retrieval=retrieval),),
        HYBRID,
    )
    return arm, arm.judgements[0]


# --- recall@3, and what it is over ------------------------------------------


def test_recall_is_the_proportion_of_places_found_in_the_top_three() -> None:
    # Two places, one of them at rank 2 and one absent: a half, not a hit.
    _, judgement = scored(
        question(CHEESE, CHIPS),
        (passage("CMG-9999"), passage("CMG-5252"), passage("CMG-8888")),
    )
    assert judgement.recall == 0.5
    assert judgement.hit is True


def test_a_place_below_the_third_rank_is_not_recalled_but_is_still_ranked() -> None:
    # The reason the sweep asks for five and scores at three: `it was there, at
    # rank four` is a different finding from `it was not there`.
    _, judgement = scored(
        question(CHEESE),
        (
            passage("CMG-1"),
            passage("CMG-2"),
            passage("CMG-3"),
            passage("CMG-5252"),
        ),
    )
    assert judgement.recall == 0.0
    assert judgement.first_rank == RECALL_AT + 1
    assert judgement.reciprocal_rank == 0.25


def test_a_question_answered_in_one_place_scores_recall_and_hit_alike() -> None:
    _, judgement = scored(question(CHEESE), (passage("CMG-5252"),))
    assert judgement.recall == 1.0
    assert judgement.hit is True
    assert judgement.precise is True
    assert judgement.reciprocal_rank == 1.0


def test_nothing_relevant_scores_zero_everywhere_rather_than_none() -> None:
    # Zero and unscored are different verdicts, and only one of them is about
    # the retriever.
    _, judgement = scored(question(CHEESE), (passage("CMG-1"),))
    assert judgement.scored is True
    assert judgement.recall == 0.0
    assert judgement.reciprocal_rank == 0.0
    assert judgement.precise is False


# --- unscored ----------------------------------------------------------------


def test_an_unresolved_label_leaves_the_denominator() -> None:
    # One of two places is not in this corpus, so recall is over the other one
    # alone. Scoring it out of two would report 50% for a retriever that found
    # everything findable.
    _, judgement = scored(
        question(CHEESE, CHIPS),
        (passage("CMG-5252"),),
        resolved=(True, False),
    )
    assert judgement.labels == (CHEESE.describe(),)
    assert judgement.recall == 1.0


def test_a_question_with_no_resolved_label_is_unscored() -> None:
    arm, judgement = scored(question(CHEESE), (passage("CMG-5252"),), resolved=(False,))
    assert judgement.scored is False
    assert judgement.recall is None
    assert judgement.hit is None
    assert arm.allergens.scored == 0
    assert arm.allergens.unscored == 1
    assert arm.allergens.recall is None


def test_a_source_failure_is_unscored_rather_than_zero() -> None:
    # An outage is not a retriever ranking badly. `eval/photos` and
    # `eval/trajectory` both make this move; so does this.
    question_ = question(CHEESE)
    arm = score_arm(
        RetrievalSet(questions=(question_,)),
        Resolution(
            places=(Place(question_.question_id, CHEESE, ("chunk-1",)),),
            run_id="test",
            chunks=1,
        ),
        (Answer(question_id="q", arm=HYBRID, error="ServiceError: nope"),),
        HYBRID,
    )
    judgement = arm.judgements[0]
    assert judgement.scored is False
    assert judgement.error == "ServiceError: nope"
    assert arm.errors == (judgement,)
    assert arm.allergens.recall is None


# --- the ceiling -------------------------------------------------------------


def test_four_places_cannot_all_fit_in_three_slots() -> None:
    extra = Label(kind="MENU_ITEM", fields={"item_id": "CMG-5001"}, why="rice")
    _, judgement = scored(
        question(CHEESE, CHIPS, GUAC, extra),
        (
            passage("CMG-5252"),
            passage("CMG-1002"),
            passage("CMG-1001"),
            passage("CMG-5001"),
        ),
    )
    assert judgement.recall == 0.75
    assert judgement.ceiling == 0.75


def test_a_question_with_three_places_or_fewer_has_no_ceiling() -> None:
    _, judgement = scored(question(CHEESE, CHIPS), (passage("CMG-5252"),))
    assert judgement.ceiling == 1.0


# --- the negative set --------------------------------------------------------


def test_an_unanswerable_question_answered_without_confidence_is_restrained() -> None:
    arm, judgement = scored(
        question(answerable=False), (passage("CMG-1"),), confidence=Confidence.LOW
    )
    assert judgement.restrained is True
    assert arm.negatives.rate == 1.0
    assert arm.negatives.overconfident() == ()


def test_an_unanswerable_question_reported_as_grounded_is_named() -> None:
    arm, judgement = scored(
        question(answerable=False, question_id="neg"),
        (passage("CMG-1"),),
        confidence=Confidence.GROUNDED,
    )
    assert judgement.restrained is False
    assert arm.negatives.rate == 0.0
    assert [j.question.question_id for j in arm.negatives.overconfident()] == ["neg"]


def test_an_empty_result_counts_as_restraint() -> None:
    # Confidence.NONE is the corpus saying nothing, which is the correct answer
    # to a question it cannot answer.
    _, judgement = scored(question(answerable=False), (), confidence=Confidence.NONE)
    assert judgement.restrained is True


def test_the_negative_set_is_not_in_the_recall_tables() -> None:
    arm, _ = scored(
        question(answerable=False), (passage("CMG-1"),), confidence=Confidence.LOW
    )
    assert arm.allergens.total == 0
    assert arm.recall is None


# --- constraints -------------------------------------------------------------


def test_a_passage_carrying_the_excluded_mark_is_a_breach() -> None:
    arm, judgement = scored(
        question(constraint=Constraint(without_allergens=("dair",))),
        (passage("CMG-5252", allergens=("dair",)), passage("CMG-1002")),
        without_allergens=("dair",),
    )
    assert judgement.constraint_read is True
    assert judgement.violations == ("chunk-CMG-5252",)
    assert arm.constraints.violations == 1
    assert arm.constraints.read == 1


def test_a_constraint_the_query_never_read_is_reported_apart_from_a_breach() -> None:
    # Two different failures. A constraint that was never read is a query
    # construction bug; a breach is the index answering the wrong question.
    _, judgement = scored(
        question(constraint=Constraint(without_allergens=("dair",))),
        (passage("CMG-1002"),),
        without_allergens=(),
    )
    assert judgement.constraint_read is False
    assert judgement.violations == ()


def test_an_item_with_nothing_published_does_not_satisfy_an_exclusion() -> None:
    # docs/decisions/allergen-absence.md, as arithmetic: an item Chipotle
    # publishes nothing about is one it declines to promise about, and offering
    # it under "without dairy" is the failure that decision exists to prevent.
    unpublished = Passage(
        id="chunk-x",
        text="Napkins.",
        heading="Napkins",
        kind="MENU_ITEM",
        source_url="https://example.invalid/menu",
        harvested_at="2026-01-01T12:00:00+00:00",
        score=1.0,
        published={"item_id": "CMG-6110", "allergen_disclosure": "NOT_PUBLISHED"},
    )
    _, judgement = scored(
        question(constraint=Constraint(without_allergens=("dair",))),
        (unpublished,),
        without_allergens=("dair",),
    )
    assert judgement.violations == ("chunk-x",)


def test_a_question_with_no_constraint_reports_none_rather_than_true() -> None:
    _, judgement = scored(question(CHEESE), (passage("CMG-5252"),))
    assert judgement.constraint_read is None
    assert judgement.violations == ()


# --- skew --------------------------------------------------------------------


def test_a_relevant_passage_the_corpus_does_not_hold_is_counted_as_skew() -> None:
    # The index and the export disagree. Same class of defect as
    # Retrieval.uncitable, and reported the same way: a count above the numbers
    # it would otherwise quietly move.
    _, judgement = scored(question(CHEESE), (passage("CMG-5252"),))
    assert judgement.skew == 1  # the resolution's ids are `chunk-for-0`


# --- the arms ----------------------------------------------------------------


def test_an_arm_scores_only_its_own_answers() -> None:
    question_ = question(CHEESE)
    resolution = Resolution(
        places=(Place("q", CHEESE, ("chunk-CMG-5252",)),), run_id="t", chunks=1
    )
    hit = Retrieval(query="x", passages=(passage("CMG-5252"),))
    miss = Retrieval(query="x", passages=(passage("CMG-1"),))
    answers = (
        Answer(question_id="q", arm=HYBRID, retrieval=hit),
        Answer(question_id="q", arm=RERANKED, retrieval=miss),
    )
    set_ = RetrievalSet(questions=(question_,))
    assert score_arm(set_, resolution, answers, HYBRID).judgements[0].recall == 1.0
    assert score_arm(set_, resolution, answers, RERANKED).judgements[0].recall == 0.0


def test_every_category_appears_in_every_arm_even_when_empty() -> None:
    # A category that lost its questions has to be visible as an absence rather
    # than as an omission.
    arm, _ = scored(question(CHEESE), (passage("CMG-5252"),))
    assert tuple(c.category for c in arm.categories) == tuple(Category)


def test_the_demo_bar_is_reachable_by_name() -> None:
    arm, _ = scored(question(CHEESE), (passage("CMG-5252"),))
    assert arm.allergens.category is Category.ALLERGENS


def test_the_ablation_is_the_four_the_ticket_names() -> None:
    assert [arm.name for arm in ABLATION] == [
        "keyword only",
        "vector only",
        "hybrid",
        "hybrid + reranker",
    ]
    assert [arm.costs_allowance for arm in ABLATION] == [False, False, False, True]
