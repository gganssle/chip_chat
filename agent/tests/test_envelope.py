"""D9's response format: citation presence is a rule, not a judgement.

``test_sabotage.py`` covers the case that matters most -- a minted id resolving
to nothing. What is here is the rest of the envelope: where the app draws a
citation, and when the citation rule fires at all.
"""

from chip_chat.agent.envelope import (
    Citation,
    CitationPlacement,
    ClaimClass,
    ModelResponse,
    ResponseEnvelope,
    render,
)

BARBACOA = Citation(
    id="chunk_8f21",
    label="Menu - Barbacoa",
    source_url="https://example.invalid/menu/barbacoa",
    harvested_at="2026-08-24T03:11:00Z",
)
RETRIEVED = {BARBACOA.id: BARBACOA}


def test_the_visitor_reads_the_retriever_s_words_not_the_model_s() -> None:
    """Every field of a citation comes off the retrieval payload.

    The model contributes an id. It has no field in which to supply a label, a
    URL or a harvest date, which is what makes "the model cannot mint a source"
    a shape rather than a hope.
    """
    envelope = render(
        ModelResponse(
            text="Moderately.",
            citation_ids=("chunk_8f21",),
            claim_class=ClaimClass.FOOD,
        ),
        retrieved=RETRIEVED,
    )

    assert envelope.citations == (BARBACOA,)
    assert envelope.as_dict()["citations"][0]["label"] == "Menu - Barbacoa"


def test_a_repeated_id_is_cited_once() -> None:
    envelope = render(
        ModelResponse(
            text="...",
            citation_ids=("chunk_8f21", "chunk_8f21"),
            claim_class=ClaimClass.FOOD,
        ),
        retrieved=RETRIEVED,
    )

    assert len(envelope.citations) == 1


def test_a_food_claim_with_no_citation_is_the_k2_violation() -> None:
    """PRD K2's target is zero, and this property is the deterministic half.

    It runs on every live turn rather than on a sample, which is what lets the
    zero mean zero. The groundedness judge stays a judge for the other half.
    """
    envelope = render(
        ModelResponse(text="It is spicy.", claim_class=ClaimClass.FOOD),
        retrieved=RETRIEVED,
    )

    assert envelope.uncited_claim


def test_an_account_answer_is_not_expected_to_cite() -> None:
    """*"You have 1,250 points"* is grounded in Snowflake, not in a published page.

    ``claim_class: account`` exists exactly so the rule does not fire where
    there is nothing to point at, and a source link would be decoration.
    """
    envelope = render(
        ModelResponse(text="You have 1,250 points.", claim_class=ClaimClass.ACCOUNT),
        retrieved={},
    )

    assert not envelope.uncited_claim
    assert envelope.placement is CitationPlacement.NONE


def test_allergen_answers_cite_adjacently_and_are_never_collapsed() -> None:
    """D9's stricter rule, where the citation is doing safety work.

    In an answer covering three items, a deduplicated trailing line leaves it
    ambiguous which source backs which claim -- which is the ambiguity an
    allergen answer cannot afford.
    """
    allergen = ResponseEnvelope(
        text="...", citations=(BARBACOA,), claim_class=ClaimClass.ALLERGEN
    )
    food = ResponseEnvelope(
        text="...", citations=(BARBACOA,), claim_class=ClaimClass.FOOD
    )

    assert allergen.placement is CitationPlacement.ADJACENT
    assert not allergen.deduplicate_by_source
    assert food.placement is CitationPlacement.TRAILING
    assert food.deduplicate_by_source
