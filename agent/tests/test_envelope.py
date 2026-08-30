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
    citations_from,
    parse,
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


# ---------------------------------------------------------------------------
# Reading the model, which is the half that had no caller (bead chip-2ky)
# ---------------------------------------------------------------------------


def test_the_trailing_json_line_the_deployment_wrote_is_read_and_not_shown() -> None:
    """``chip-2ky``, in one assertion.

    This is what the deployed model did with the prompt's instruction to return
    ids *"in the citations field of your response"*: it wrote the field as a
    line of JSON after the answer. Nothing parsed it, so the visitor read it.
    """
    reply = (
        "Moderately. It's braised with chipotle chiles and cumin.\n"
        '{"claim_class":"food","citations":["chunk_8f21"]}'
    )

    response = parse(reply)

    assert response.text == "Moderately. It's braised with chipotle chiles and cumin."
    assert response.citation_ids == ("chunk_8f21",)
    assert response.claim_class is ClaimClass.FOOD
    assert "{" not in render(response, retrieved=RETRIEVED).text


def test_a_whole_reply_that_is_the_envelope_object_is_read_too() -> None:
    """What a ``response_format`` would produce, should one ever be set.

    Accepting both shapes is what lets that be a deployment change rather than a
    second parser.
    """
    response = parse(
        '{"text":"Moderately.","claim_class":"food","citations":["chunk_8f21"]}'
    )

    assert response.text == "Moderately."
    assert response.citation_ids == ("chunk_8f21",)


def test_an_ordinary_prose_reply_is_left_exactly_as_it_was_written() -> None:
    """The branch that has to be right on every turn that is not about food.

    Most replies are prose and always will be. A parser that damaged them to
    catch the envelope would have traded a visible bug for a subtle one.
    """
    reply = "You have 1,340 points -- enough for a free entree."

    response = parse(reply)

    assert response.text == reply
    assert response.citation_ids == ()
    assert response.claim_class is ClaimClass.NONE


def test_json_the_visitor_asked_to_see_is_not_eaten() -> None:
    """A trailing object is only the envelope when it says something about one.

    Requiring ``citations`` or ``claim_class`` is what keeps this from swallowing
    an object the model deliberately showed somebody.
    """
    reply = 'Here is the shape of a draft: {"draft_id": "d-1", "total": "12.35"}'

    assert parse(reply).text == reply


def test_an_empty_or_missing_reply_does_not_raise() -> None:
    """The loop's fallback is downstream of this and has to be reachable."""
    assert parse(None).text == ""
    assert parse("   ").text == ""


def test_an_unparseable_envelope_still_renders_as_the_answer() -> None:
    """Every failure here fails towards showing the sentence.

    A model that started the field and did not finish it has produced a worse
    answer, not a broken conversation.
    """
    reply = 'Moderately spicy.\n{"claim_class":"food","citations":['

    assert parse(reply).text == reply


def test_an_unknown_claim_class_costs_a_citation_line_and_not_the_answer() -> None:
    reply = 'Moderately.\n{"claim_class":"delicious","citations":["chunk_8f21"]}'

    response = parse(reply)

    assert response.text == "Moderately."
    assert response.claim_class is ClaimClass.NONE


def test_a_fenced_envelope_is_read_and_the_fence_goes_with_it() -> None:
    """Models fence JSON. Leaving the fence behind would swap one artefact for
    a smaller one."""
    reply = 'Moderately.\n```json\n{"claim_class":"food","citations":["chunk_8f21"]}\n```'

    response = parse(reply)

    assert response.text == "Moderately."
    assert response.citation_ids == ("chunk_8f21",)


def test_ids_echoed_as_objects_are_read_and_their_other_fields_ignored() -> None:
    """A model shown ``{"id": ..., "label": ...}`` sometimes hands it back.

    The id is taken and the rest is not, which is the same rule
    :class:`ModelResponse` enforces by having nowhere to put a label.
    """
    reply = (
        "Moderately.\n"
        '{"claim_class":"food","citations":[{"id":"chunk_8f21","label":"Invented"}]}'
    )

    envelope = render(parse(reply), retrieved=RETRIEVED)

    assert envelope.citations == (BARBACOA,)
    assert envelope.citations[0].label == "Menu - Barbacoa"


def test_a_minted_id_read_off_a_real_reply_is_still_dropped() -> None:
    """The parse does not weaken the resolution, which is the security half."""
    reply = 'Moderately.\n{"claim_class":"food","citations":["chunk_invented"]}'

    envelope = render(parse(reply), retrieved=RETRIEVED)

    assert envelope.citations == ()
    assert envelope.dropped_citation_ids == ("chunk_invented",)
    assert envelope.uncited_claim


def test_citations_from_drops_a_passage_that_cannot_be_shown() -> None:
    """A citation pointing nowhere is worse than no citation.

    It renders as evidence and is not, and it would be counted as a cited claim
    by the rule PRD K2's target of zero is measured with.
    """
    resolved = citations_from(
        {
            "good": {
                "id": "good",
                "label": "Menu - Barbacoa",
                "source_url": "https://example.invalid/menu/barbacoa",
                "harvested_at": "2026-08-24T03:11:00Z",
            },
            "urlless": {
                "id": "urlless",
                "label": "Menu - Steak",
                "source_url": "",
                "harvested_at": "2026-08-24T03:11:00Z",
            },
        }
    )

    assert set(resolved) == {"good"}
