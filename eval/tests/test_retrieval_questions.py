"""The set the repository ships, held to its own rules and to the menu.

Every assertion here is about the *manifest*, not about a retriever. A set that
is wrong about itself produces numbers that look exactly like numbers, so this
is the file that has to pass before any of the others mean anything.
"""

import json
from pathlib import Path

import pytest

from chip_chat.eval.retrieval.questions import (
    Category,
    Label,
    QuestionError,
    RetrievalSet,
)


def write(tmp_path: Path, questions: list[dict[str, object]]) -> Path:
    """Write a one-off manifest, for driving a refusal."""
    path = tmp_path / "questions.json"
    path.write_text(json.dumps({"questions": questions}), encoding="utf-8")
    return path


def one(**overrides: object) -> dict[str, object]:
    """A minimal well-formed entry, with fields replaced."""
    entry: dict[str, object] = {
        "id": "q1",
        "question": "does the cheese have dairy in it",
        "category": "allergens",
        "relevant": [{"kind": "MENU_ITEM", "item_id": "CMG-5252", "why": "its row"}],
        "why": "a question",
    }
    entry.update(overrides)
    return entry


# --- The shipped set ---------------------------------------------------------


def test_the_shipped_set_loads(retrieval_questions: RetrievalSet) -> None:
    assert len(retrieval_questions) >= 30


def test_every_category_has_answerable_questions(
    retrieval_questions: RetrievalSet,
) -> None:
    # A category with none would print an em-dash row forever, which reads as a
    # measurement that has not been taken rather than as one nobody wrote.
    for category in Category:
        answerable = [
            q for q in retrieval_questions.in_category(category) if q.answerable
        ]
        assert answerable, category


def test_the_allergen_category_is_the_heaviest(
    retrieval_questions: RetrievalSet,
) -> None:
    # The bead is explicit that the allergen questions are weighted deliberately
    # rather than sampled uniformly, because that is where a retrieval failure
    # becomes a safety problem. This is that instruction, as an assertion.
    counts = {
        category: len(retrieval_questions.in_category(category)) for category in Category
    }
    assert counts[Category.ALLERGENS] == max(counts.values())


def test_no_unanswerable_question_names_a_passage(
    retrieval_questions: RetrievalSet,
) -> None:
    for question in retrieval_questions:
        if not question.answerable:
            assert not question.relevant, question.question_id


def test_every_label_carries_its_argument(retrieval_questions: RetrievalSet) -> None:
    # A label without a `why` is somebody's guess about relevance that nobody
    # can argue with later.
    for question, label in retrieval_questions.labels():
        assert label.why, f"{question.question_id}: {label.describe()}"


def test_no_label_is_keyed_to_a_chunk_id(retrieval_questions: RetrievalSet) -> None:
    # The whole design. A chunk id is a content hash, so a set keyed on one goes
    # uniformly wrong on the change it exists to detect. The loader refuses
    # `chunk_id` as a selector; this says the shipped set does not try.
    for _, label in retrieval_questions.labels():
        assert "chunk_id" not in label.fields


def test_the_golden_cases_it_claims_are_golden_cases(
    retrieval_questions: RetrievalSet, golden: object
) -> None:
    # #50's scope says the set is *built from* the knowledge portion of #29. A
    # case id nobody can find in the golden set would make that claim
    # unfalsifiable.
    known = {case.case_id for case in golden.cases}  # type: ignore[attr-defined]
    for question in retrieval_questions:
        if question.golden_case:
            assert question.golden_case in known, question.question_id


def test_the_menu_terms_a_question_leans_on_are_published(
    retrieval_questions: RetrievalSet, catalog: object
) -> None:
    # The staleness check the golden set and the photo set both make, in this
    # set's own terms: a label naming an item id the catalogue does not publish
    # was written against a different build, and the rest of the manifest is no
    # more trustworthy. Item ids only -- `item_type` and `primary_filling` are
    # chunk-level groupings rather than catalogue keys.
    published = {item.item_id for item in catalog.menu_items}  # type: ignore[attr-defined]
    for question, label in retrieval_questions.labels():
        item_id = label.fields.get("item_id")
        if item_id is not None:
            assert item_id in published, f"{question.question_id}: {item_id}"


# --- What the loader refuses -------------------------------------------------


def test_an_unanswerable_question_may_not_name_a_passage(tmp_path: Path) -> None:
    manifest = write(tmp_path, [one(answerable=False)])
    with pytest.raises(QuestionError, match="may not name a passage"):
        RetrievalSet.load(manifest)


def test_an_answerable_question_needs_something_to_be_scored_on(
    tmp_path: Path,
) -> None:
    manifest = write(tmp_path, [one(relevant=[])])
    with pytest.raises(QuestionError, match="needs either a relevant passage"):
        RetrievalSet.load(manifest)


def test_a_repeated_place_is_refused(tmp_path: Path) -> None:
    # Two identical labels would put the same place in the denominator twice,
    # which quietly halves the question's recall.
    place = {"kind": "MENU_ITEM", "item_id": "CMG-5252", "why": "its row"}
    manifest = write(tmp_path, [one(relevant=[place, dict(place)])])
    with pytest.raises(QuestionError, match="same place twice"):
        RetrievalSet.load(manifest)


def test_a_label_that_names_a_whole_kind_is_not_a_place(tmp_path: Path) -> None:
    manifest = write(tmp_path, [one(relevant=[{"kind": "MENU_ITEM", "why": "all"}])])
    with pytest.raises(QuestionError, match="not a place"):
        RetrievalSet.load(manifest)


def test_a_selector_on_the_chunk_id_is_refused(tmp_path: Path) -> None:
    manifest = write(
        tmp_path,
        [one(relevant=[{"kind": "MENU_ITEM", "chunk_id": "abc", "why": "that one"}])],
    )
    with pytest.raises(QuestionError, match="not a published chunk field"):
        RetrievalSet.load(manifest)


def test_a_constraint_on_a_word_nobody_published_is_refused(tmp_path: Path) -> None:
    # docs/decisions/allergen-absence.md: nothing here matches on the spelling
    # of a code or on a synonym nobody published. `milk` is the example the
    # search package's own docstring uses.
    manifest = write(tmp_path, [one(constraint={"without_allergens": ["milk"]})])
    with pytest.raises(QuestionError, match="not a published allergen code"):
        RetrievalSet.load(manifest)


def test_an_unknown_category_is_refused(tmp_path: Path) -> None:
    manifest = write(tmp_path, [one(category="hours")])
    with pytest.raises(QuestionError, match="five categories"):
        RetrievalSet.load(manifest)


def test_a_duplicate_question_id_is_refused(tmp_path: Path) -> None:
    manifest = write(tmp_path, [one(), one()])
    with pytest.raises(QuestionError, match="duplicate question id"):
        RetrievalSet.load(manifest)


def test_a_label_without_a_reason_is_refused(tmp_path: Path) -> None:
    manifest = write(
        tmp_path, [one(relevant=[{"kind": "MENU_ITEM", "item_id": "CMG-1"}])]
    )
    with pytest.raises(QuestionError, match="needs a `why`"):
        RetrievalSet.load(manifest)


# --- Matching ----------------------------------------------------------------


def test_a_field_absent_from_a_chunk_never_matches() -> None:
    # Absent and empty are different claims and only one of them is about the
    # source. A policy section with no heading must not satisfy a label asking
    # for `heading=""`.
    label = Label(kind="POLICY_SECTION", fields={"heading": ""}, why="x")
    assert not label.matches({"kind": "POLICY_SECTION"})


def test_a_heading_matches_after_stripping() -> None:
    label = Label(kind="POLICY_SECTION", fields={"heading": "ELIGIBILITY"}, why="x")
    assert label.matches({"kind": "POLICY_SECTION", "heading": " ELIGIBILITY "})


def test_contains_is_case_insensitive() -> None:
    label = Label(kind="FAQ_ENTRY", contains="Do Points Expire", why="x")
    assert label.matches({"kind": "FAQ_ENTRY", "text": "do points expire? no."})


def test_a_label_of_another_kind_never_matches() -> None:
    label = Label(kind="MENU_ITEM", fields={"item_id": "CMG-1"}, why="x")
    assert not label.matches({"kind": "NUTRITION_ROW", "item_id": "CMG-1"})
