"""What a visitor's sentence becomes, and what it deliberately does not.

Every assertion here is on a *string* or on a small dataclass, because that is
where a query's correctness lives. ``fakes.FakeSearchService`` does not evaluate
``filter`` — an OData evaluator in the fake would be a parser marking its own
homework — so the filter is checked as text, exactly as it will be sent.
"""

import json
from pathlib import Path

import pytest

from chip_chat.search import chunks
from chip_chat.search.query import (
    ALLERGENS,
    CAPTIONS,
    DIETS,
    TOP,
    VECTOR_CANDIDATES,
    Bound,
    Constraints,
    Halves,
    body,
    filter_expression,
    overlap,
    read,
    terms,
)
from chip_chat.search.schema import SEMANTIC_CONFIGURATION, VECTOR_FIELD

NUTRITION_FIXTURE = (
    Path(__file__).parents[2]
    / "harvest"
    / "tests"
    / "fixtures"
    / "chipotle"
    / "nutrition.json"
)
"""Chipotle's own tag vocabulary, as the harvester parses it.

Read here rather than imported for the same reason
:mod:`chip_chat.search.chunks` restates the chunk schema: the constant is worth
reading on its own, and a test is what keeps it from drifting.
"""


def published_tags() -> dict[str, str]:
    """Return every published ``tagCode`` to ``tagName`` in the fixture."""
    found: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "tagCode" in node and node.get("tagName"):
                found[str(node["tagCode"])] = str(node["tagName"]).casefold()
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(NUTRITION_FIXTURE.read_text("utf-8")))
    return found


# --- The published vocabulary ------------------------------------------------


def test_every_allergen_code_and_label_is_one_chipotle_publishes() -> None:
    # docs/decisions/allergen-absence.md: nothing here matches on the spelling
    # of a code or on a synonym nobody published. That is only true if the four
    # entries really are the published ones.
    tags = published_tags()
    for code, label in ALLERGENS.items():
        assert tags[code] == label


def test_every_diet_code_this_module_refuses_is_also_a_published_one() -> None:
    tags = published_tags()
    for code, label in DIETS.items():
        assert tags[code] == label


def test_no_allergen_is_also_a_diet() -> None:
    assert not set(ALLERGENS) & set(DIETS)


# --- Constraints the index can express ---------------------------------------


@pytest.mark.parametrize(
    ("question", "value", "inclusive"),
    [
        ("what has under 500 calories", 500.0, False),
        ("something below 400 calories please", 400.0, False),
        ("a bowl with less than 600 calories", 600.0, False),
        ("fewer than 550 calories", 550.0, False),
        ("no more than 700 calories", 700.0, True),
        ("at most 450 calories", 450.0, True),
        ("500 calories or less", 500.0, True),
        ("650 calories or fewer", 650.0, True),
    ],
)
def test_an_upper_calorie_bound_is_read_with_the_right_edge(
    question: str, value: float, inclusive: bool
) -> None:
    # "Under 500" excludes a 500-calorie item and "500 or less" includes it.
    # They differ on exactly one published figure, which is a small thing to
    # get right and a strange thing to get wrong on purpose.
    assert read(question).max_calories == Bound(value, inclusive)


@pytest.mark.parametrize(
    ("question", "value", "inclusive"),
    [
        ("more than 800 calories", 800.0, False),
        ("over 900 calories", 900.0, False),
        ("at least 600 calories", 600.0, True),
        ("700 calories or more", 700.0, True),
    ],
)
def test_a_lower_calorie_bound_is_read_too(
    question: str, value: float, inclusive: bool
) -> None:
    assert read(question).min_calories == Bound(value, inclusive)


def test_an_upper_bound_becomes_a_filter_that_excludes_non_menu_chunks() -> None:
    # `calories` is populated only on MENU_ITEM chunks and an OData comparison
    # against a null is false, so this filter cannot return a policy section.
    # That is the question the visitor asked.
    assert filter_expression(read("anything under 500 calories")) == "calories lt 500"


@pytest.mark.parametrize(
    "question",
    [
        "what is made without dairy",
        "I am avoiding dairy",
        "dairy free options",
        "dairy-free please",
        "I'm allergic to dairy",
        "no dairy for me",
        "can you leave out dairy",
    ],
)
def test_a_negated_allergen_becomes_an_exclusion(question: str) -> None:
    assert read(question).without_allergens == ("dair",)


@pytest.mark.parametrize(
    "question",
    [
        "does the cheese have dairy in it",
        "which items contain dairy",
        "is there dairy in the barbacoa",
        "tell me about the dairy in queso",
    ],
)
def test_asking_about_an_allergen_is_not_asking_to_avoid_it(question: str) -> None:
    # The failure this prevents is the loud one: a filter built from the mention
    # alone answers "which items contain dairy" with the items that do not.
    assert read(question).without_allergens == ()


def test_the_exclusion_filter_requires_published_allergen_data() -> None:
    # The disclosure clause is the whole decision. Without it every chunk
    # Chipotle publishes nothing about -- napkins, policy sections, anything new
    # -- arrives inside an answer a visitor reads as "safe".
    # docs/decisions/allergen-absence.md.
    assert filter_expression(read("what has no gluten")) == (
        "allergen_disclosure eq 'PUBLISHED' and not allergens/any(a: a eq 'glut')"
    )


def test_two_avoided_allergens_are_both_excluded() -> None:
    constraints = read("something with no dairy and no gluten")
    assert constraints.without_allergens == ("dair", "glut")
    assert filter_expression(constraints) == (
        "allergen_disclosure eq 'PUBLISHED' "
        "and not allergens/any(a: a eq 'dair') "
        "and not allergens/any(a: a eq 'glut')"
    )


def test_the_american_spelling_of_a_published_label_is_the_same_word() -> None:
    assert read("nothing with sulfites").without_allergens == ("sulp",)
    assert read("nothing with sulphites").without_allergens == ("sulp",)


def test_an_unpublished_synonym_is_not_a_filter() -> None:
    # "Milk" is not a word Chipotle publishes about its allergen marks. The
    # vector half of the query is what carries paraphrase; a *filter* is exact,
    # and an exact answer to a question nobody published is the one kind of
    # wrong this lane cannot be.
    assert read("what has no milk in it").without_allergens == ()
    assert filter_expression(read("what has no milk in it")) is None


def test_an_exclusion_carries_the_caveat_that_not_marked_is_not_free_of() -> None:
    notes = " ".join(read("made without dairy").notes)
    assert "not marked" in notes
    assert "free of" in notes


def test_calories_and_allergens_compose_into_one_filter() -> None:
    constraints = read("a bowl under 600 calories with no dairy")
    assert filter_expression(constraints) == (
        "calories lt 600 "
        "and allergen_disclosure eq 'PUBLISHED' "
        "and not allergens/any(a: a eq 'dair')"
    )


# --- Constraints the index cannot express ------------------------------------


def test_a_calorie_comparison_with_no_figure_is_named_rather_than_guessed() -> None:
    constraints = read("which burrito has fewer calories")
    assert constraints.max_calories is None
    assert filter_expression(constraints) is None
    assert any("no figure" in note for note in constraints.unapplied)


def test_a_calorie_comparison_with_a_figure_is_not_reported_as_unapplied() -> None:
    constraints = read("which bowl has fewer than 500 calories")
    assert constraints.max_calories == Bound(500.0, False)
    assert constraints.unapplied == ()


def test_vegetarian_is_a_published_tag_that_this_index_cannot_filter_on() -> None:
    # The chunk schema carries allergen marks and no dietary marks, so the
    # honest handling of "vegetarian" is to say the passages are unfiltered --
    # not to approximate it from ingredient text, which would be inventing a
    # dietary claim about food.
    constraints = read("what is vegetarian")
    assert not any(entry.name.startswith("diet") for entry in chunks.FIELDS)
    assert filter_expression(constraints) is None
    assert any("NOT filtered" in note for note in constraints.unapplied)


def test_an_unfiltered_query_produces_no_filter_key_at_all() -> None:
    assert "filter" not in body("how do points work", rerank=True)


# --- The request itself ------------------------------------------------------


def test_a_query_always_carries_both_halves() -> None:
    # RFC-001 section 08: keyword recall matters more than usual here, because
    # item names are proper nouns. Neither half is a fallback for the other.
    request = body("barbacoa", rerank=True)
    assert request["search"] == "barbacoa"
    assert request["vectorQueries"] == [
        {
            "kind": "text",
            "text": "barbacoa",
            "fields": VECTOR_FIELD,
            "k": VECTOR_CANDIDATES,
        }
    ]


def test_the_vector_half_is_text_so_the_service_embeds_it() -> None:
    # The application never embeds a query: that is the half of integrated
    # vectorization this estate has, and the one that fails silently when it is
    # wrong. docs/retrieval-index.md section 3.
    assert body("anything", rerank=False)["vectorQueries"][0]["kind"] == "text"


def test_a_reranked_query_names_the_index_s_semantic_configuration() -> None:
    request = body("how do I earn points", rerank=True)
    assert request["queryType"] == "semantic"
    assert request["semanticConfiguration"] == SEMANTIC_CONFIGURATION
    assert request["captions"] == CAPTIONS


def test_a_degraded_query_is_still_hybrid() -> None:
    request = body("how do I earn points", rerank=False)
    assert request["queryType"] == "simple"
    assert "semanticConfiguration" not in request
    assert "captions" not in request
    assert request["search"]
    assert request["vectorQueries"]


def test_semantic_answers_are_never_requested() -> None:
    # An extractive answer is a second answer, written by the service,
    # competing with the one the agent is about to write from the same
    # passages.
    assert "answers" not in body("anything", rerank=True)


def test_no_select_is_sent_because_every_field_is_retrievable() -> None:
    assert "select" not in body("anything", rerank=True)
    assert set(chunks.retrievable()) == set(chunks.names())


def test_top_defaults_to_the_module_s_number() -> None:
    assert body("anything", rerank=True)["top"] == TOP


def test_a_constraint_reaches_the_request_as_a_filter() -> None:
    request = body(
        "under 500 calories", constraints=read("under 500 calories"), rerank=True
    )
    assert request["filter"] == "calories lt 500"


def test_constraints_serialise_for_the_span() -> None:
    payload = read("a bowl under 600 calories with no dairy").as_dict()
    assert payload["max_calories"] == 600.0
    assert payload["without_allergens"] == ["dair"]
    assert Constraints().as_dict()["without_allergens"] == []


# --- The lexical floor -------------------------------------------------------


def test_terms_drops_stopwords_and_very_short_words() -> None:
    assert terms("what is in the barbacoa") == frozenset({"barbacoa"})


def test_overlap_is_zero_when_a_passage_shares_no_content_word() -> None:
    # A passage with no query term in it is a vector neighbour and nothing
    # more, which is exactly the near-miss RFC-001 section 08 warns about.
    assert overlap(terms("how do I earn rewards points"), "Chips. Type: Chips.") == 0.0


def test_overlap_counts_a_plural_as_its_singular() -> None:
    assert overlap(terms("do points expire"), "Point expiration") > 0.0


def test_overlap_of_a_query_with_no_content_words_is_zero() -> None:
    assert overlap(terms("is it?"), "anything at all") == 0.0


# --- The ablation's halves ---------------------------------------------------


def test_the_default_query_carries_both_halves() -> None:
    # RFC-001 section 08's design, as the shape of the request rather than as a
    # sentence in a docstring. Nothing on a serving path passes `halves`.
    request = body("barbacoa", rerank=True)
    assert request["search"] == "barbacoa"
    assert request["vectorQueries"][0]["text"] == "barbacoa"


def test_the_keyword_arm_sends_no_vector_half() -> None:
    request = body("barbacoa", rerank=False, halves=Halves.KEYWORD)
    assert request["search"] == "barbacoa"
    assert "vectorQueries" not in request


def test_the_vector_arm_omits_the_search_key_rather_than_emptying_it() -> None:
    # An empty `search` is a lexical query that matches everything, which would
    # leave a second order in the fusion and quietly stop being an ablation.
    request = body("barbacoa", rerank=False, halves=Halves.VECTOR)
    assert "search" not in request
    assert request["vectorQueries"][0]["text"] == "barbacoa"


def test_a_filter_applies_to_every_arm() -> None:
    # The constrained cases are exactly where the arms are expected to differ,
    # so a filter that only fired on one of them would make that difference
    # unreadable.
    constraints = read("under 500 calories")
    for halves in Halves:
        request = body(
            "under 500 calories", constraints=constraints, rerank=False, halves=halves
        )
        assert request["filter"] == "calories lt 500"
