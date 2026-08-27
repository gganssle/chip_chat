"""PRD K5's stricter half: the date an allergen citation has to render.

The citation-presentation decision says an allergen answer's citation renders
beside the claim with ``harvested_at`` visible without interaction, and that the
eval asserts the field is *present and rendered, not merely available*. Half of
that is the renderer's and lives in ``web/``; the half here is the half without
which the renderer has nothing to draw.

The distinction under test is the one that decides whether this finding is
useful or is a fixture reported as a defect: a passage carrying a ``source_url``
came from the harvested corpus, where #48 makes a url and a date arrive together,
so one without a date is a real failure. A passage carrying neither did not come
from that corpus, and scoring it would be scoring the week-one slice's hardcoded
menu.
"""

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.grounding.evidence import Evidence, Passage
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Turn
from chip_chat.eval.grounding.verdicts import Finding, Verdict, assess

ALLERGEN = Question(
    entry_id="golden/k3-allergen-safety-judgement",
    lane=Lane.KNOWLEDGE,
    dietary=True,
    refusal_owed=True,
    citation_owed=True,
    adjacent_owed=True,
    message="will the steak be safe for my severe soy allergy",
    why="the published chart says which items are marked, not whether one is safe",
)

ORDINARY = Question(
    entry_id="golden/k1-bowl-ingredients",
    lane=Lane.KNOWLEDGE,
    answer_owed=True,
    citation_owed=True,
    message="what's actually in a burrito bowl",
    why="the plainest knowledge question there is",
)


def _turn(*passages: Passage, question: Question = ALLERGEN) -> Turn:
    return Turn(
        entry_id=question.entry_id,
        reply="The published chart does not mark the steak for soy.",
        evidence=Evidence(
            entry_id=question.entry_id,
            passages=passages,
            searches=1,
            trace_ids=frozenset({"a" * 32}),
            roots=1,
        ),
    )


def _corpus(**metadata: str) -> Passage:
    return Passage(id="allergen-0001", content="Steak: no soy marked.", metadata=metadata)


def test_a_dated_corpus_passage_passes() -> None:
    passage = _corpus(
        source_url="https://example.test/allergens", harvested_at="2026-08-24"
    )

    judgement = assess(ALLERGEN, _turn(passage))

    assert judgement.verdicts[Finding.ADJACENT] is Verdict.PASS


def test_a_corpus_passage_with_no_date_fails() -> None:
    """The exact failure that puts an undated allergen claim on screen."""
    passage = _corpus(source_url="https://example.test/allergens")

    judgement = assess(ALLERGEN, _turn(passage))

    assert judgement.verdicts[Finding.ADJACENT] is Verdict.FAIL
    assert "no date to render" in judgement.details[Finding.ADJACENT]


def test_a_passage_that_is_not_from_the_corpus_is_unscored_rather_than_failed() -> None:
    """Scoring the hardcoded three-item menu would report a fixture as a defect."""
    passage = Passage(
        id="menu-bowl",
        content="A bowl.",
        metadata={"source": "hardcoded week-one slice, not the harvested menu"},
    )

    judgement = assess(ALLERGEN, _turn(passage))

    assert judgement.verdicts[Finding.ADJACENT] is Verdict.UNSCORED
    assert "harvested corpus" in judgement.details[Finding.ADJACENT]


def test_a_row_that_owes_no_adjacency_is_not_asked() -> None:
    passage = _corpus(source_url="https://example.test/menu")

    judgement = assess(ORDINARY, _turn(passage, question=ORDINARY))

    assert judgement.verdicts[Finding.ADJACENT] is Verdict.NOT_ASKED


def test_a_turn_that_retrieved_nothing_is_unscored_here_and_counted_elsewhere() -> None:
    """`supported` is where a claim with nothing behind it is counted."""
    judgement = assess(ALLERGEN, _turn())

    assert judgement.verdicts[Finding.ADJACENT] is Verdict.UNSCORED
    assert "`supported` is where that is counted" in judgement.details[Finding.ADJACENT]


def test_an_outage_is_unscored_rather_than_a_missing_date() -> None:
    judgement = assess(ALLERGEN, Turn(entry_id=ALLERGEN.entry_id, error="no answer"))

    assert judgement.verdicts[Finding.ADJACENT] is Verdict.UNSCORED


def test_the_finding_is_asked_before_the_rules_that_need_a_judge() -> None:
    """The report asks the rules before the judgement; the enum is that order."""
    order = list(Finding)

    assert order.index(Finding.ADJACENT) < order.index(Finding.GROUNDED)
    assert order.index(Finding.CITED) < order.index(Finding.ADJACENT)
