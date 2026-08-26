"""What the harvest asks for, and what its documents are parsed into.

The assertions are on real published values — Chipotle really does publish
steak at 150 calories for a four-ounce portion and really does mark its
Monterey Jack with the dairy code — because a parser test with invented
numbers in it proves only that the parser agrees with itself.

Most of these tests are about restraint. That an item nobody published
allergen data for says so rather than saying nothing; that a figure of zero
and a figure that was never published stay different all the way into the
bytes; that a code with no published label keeps a null one; that two
documents which disagree about a safety fact stop the harvest instead of
being averaged. Those are the properties issue #20 exists to guarantee, and
each of them is a way of not turning a silence into a reassurance.
"""

import json
from collections.abc import Sequence
from typing import Any

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.sources.chipotle import (
    NUTRITION_TABLES,
    AllergenStatus,
    ChipotleSourceError,
    DietStatus,
    MissingDocumentError,
    NutritionDataset,
    TagKind,
    harvest_nutrition,
    load_nutrition,
    parse_nutrition,
    to_jsonl,
)
from chip_chat.harvest.testing import FakeClock, FakeTransport, fake_response

CHEESE = "CMG-5252"
"""Monterey Jack. Marked with the dairy code."""

STEAK = "CMG-2"
"""Steak. Described by both documents, marked with nothing."""

CHICKEN = "CMG-1"
"""Chicken. On the allergen chart and absent from the metadata fixture, which
is how the chart's turn to answer gets exercised."""

NAPKINS = "CMG-6110"
"""Napkins & Utensils. Orderable, and described by no allergen document."""

GUAVA = "CMG-2022"
"""Jarritos Guava, whose Vitamin C figure the fixture removes."""

EXTRA_CHICKEN = "CMG-1101"
"""An extra portion, published with a portion size of zero."""


def build(transport: FakeTransport, blobs: InMemoryBlobStore) -> Harvester:
    """A harvester on its own gate and clock."""
    clock = FakeClock()
    return Harvester(
        blobs,
        transport,
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(2.0, clock), 1),
    )


def dataset(
    blobs: InMemoryBlobStore, transport: FakeTransport | None = None
) -> NutritionDataset:
    """Harvest the fixture site and parse what came back."""
    harvester = build(transport if transport is not None else site.site(), blobs)
    return parse_nutrition(harvest_nutrition(harvester, [site.REFERENCE]))


def one(table: Sequence[Any], **match: Any) -> dict[str, Any]:
    """Return the single row matching every named column."""
    found = [
        row
        for row in site.rows(table)
        if all(row[column] == value for column, value in match.items())
    ]
    assert len(found) == 1, f"expected exactly one row for {match}, got {len(found)}"
    return found[0]


# --- What the harvest costs ------------------------------------------------


def test_one_restaurant_costs_five_documents(blobs: InMemoryBlobStore) -> None:
    transport = site.site()

    documents = harvest_nutrition(build(transport, blobs), [site.REFERENCE])

    fetched = [url for url in transport.urls if not url.endswith("robots.txt")]
    assert fetched == [
        site.HOME_URL,
        site.NUTRITION_URL,
        site.ALLERGEN_CHART_URL,
        site.ALLERGENS_PAGE_URL,
        site.menu_url(site.REFERENCE),
    ]
    assert len(documents.menus) == 1


def test_a_warm_landing_zone_makes_no_requests(blobs: InMemoryBlobStore) -> None:
    harvest_nutrition(build(site.site(), blobs), [site.REFERENCE])

    warm_transport = site.site()
    warm = build(warm_transport, blobs)
    harvest_nutrition(warm, [site.REFERENCE])

    assert warm_transport.requests == []
    assert warm.requests_made == 0


def test_the_offline_path_reads_what_the_harvest_wrote(
    blobs: InMemoryBlobStore,
) -> None:
    harvested = harvest_nutrition(build(site.site(), blobs), [site.REFERENCE])

    loaded = load_nutrition(DocumentCache(blobs), [site.REFERENCE])

    assert loaded.nutrition.content == harvested.nutrition.content
    assert loaded.allergen_chart.content == harvested.allergen_chart.content
    assert loaded.allergen_page.content == harvested.allergen_page.content


def test_the_offline_path_says_so_when_nothing_was_harvested(
    blobs: InMemoryBlobStore,
) -> None:
    with pytest.raises(MissingDocumentError, match="run the harvest first"):
        load_nutrition(DocumentCache(blobs), [site.REFERENCE])


def test_two_parses_of_one_cache_produce_the_same_bytes(
    blobs: InMemoryBlobStore,
) -> None:
    """The reproducibility claim, table by table."""
    harvest_nutrition(build(site.site(), blobs), [site.REFERENCE])
    cache = DocumentCache(blobs)

    first = parse_nutrition(load_nutrition(cache, [site.REFERENCE]))
    second = parse_nutrition(load_nutrition(cache, [site.REFERENCE]))

    assert first.manifest() == second.manifest()
    for name in NUTRITION_TABLES:
        assert to_jsonl(first.table(name)) == to_jsonl(second.table(name))


# --- Nutrition -------------------------------------------------------------


def test_a_published_figure_arrives_with_its_unit_and_its_portion(
    blobs: InMemoryBlobStore,
) -> None:
    calories = one(dataset(blobs).item_nutrition, item_id=STEAK, nutrient_key="tcal")

    assert calories["value"] == "150"
    assert calories["unit"] == "cal"
    assert calories["portion_unit"] == "oz"
    assert calories["portion_value"] == "4"
    assert calories["source_url"] == site.NUTRITION_URL
    assert calories["harvested_at"]


def test_a_published_zero_and_an_unpublished_figure_are_different_values(
    blobs: InMemoryBlobStore,
) -> None:
    """The criterion issue #20 turns on, asserted on the bytes themselves.

    Chipotle publishes zero grams of trans fat in a Jarritos Guava and
    publishes no Vitamin C figure for it at all. Those are different facts.
    A dataset that let the second become the first would be reporting a
    measurement nobody took.
    """
    figures = dataset(blobs).item_nutrition

    published_zero = one(figures, item_id=GUAVA, nutrient_key="tran")
    never_published = one(figures, item_id=GUAVA, nutrient_key="vitc")

    assert published_zero["value"] == "0"
    assert never_published["value"] is None
    assert never_published["unit"] == "%", "the unit is still known; the figure is not"

    written = to_jsonl(
        [row for row in figures if row.item_id == GUAVA and row.nutrient_key == "vitc"]
    ).decode()
    assert '"value":null' in written
    assert '"value":0' not in written


def test_a_portion_of_zero_is_a_published_zero_too(blobs: InMemoryBlobStore) -> None:
    """An extra portion is published as zero ounces, not as no portion at all."""
    figure = one(
        dataset(blobs).item_nutrition, item_id=EXTRA_CHICKEN, nutrient_key="tcal"
    )

    assert figure["portion_value"] == "0"
    assert figure["portion_unit"] == "oz"


def test_every_item_is_asked_about_every_nutrient_in_the_vocabulary(
    blobs: InMemoryBlobStore,
) -> None:
    parsed = dataset(blobs)
    keys = {row.nutrient_key for row in parsed.nutrients}

    for item_id in {row.item_id for row in parsed.item_nutrition}:
        asked = {
            row.nutrient_key for row in parsed.item_nutrition if row.item_id == item_id
        }
        assert keys <= asked, f"{item_id} is missing rows rather than null values"


def test_the_nutrient_vocabulary_carries_labels_units_and_sections(
    blobs: InMemoryBlobStore,
) -> None:
    nutrients = dataset(blobs).nutrients

    sodium = one(nutrients, nutrient_key="sodi")
    assert (sodium["name"], sodium["unit"]) == ("Sodium", "mg")
    assert sodium["section_key"] is None

    saturated = one(nutrients, nutrient_key="satu")
    assert (saturated["name"], saturated["unit"]) == ("Saturated Fat", "g")
    assert saturated["section_key"] == "tfat"

    calcium = one(nutrients, nutrient_key="calc")
    assert calcium["unit"] == "%", "a percentage of a daily value, not a gram"
    assert calcium["section_name"] == "Vitamins & Minerals"
    assert calcium["section_key"] is None, "that section publishes no figure of its own"


def test_a_published_calorie_range_stays_a_range(blobs: InMemoryBlobStore) -> None:
    """Narrowing '170-250 cal' to one number would invent the number."""
    lemonade = one(dataset(blobs).item_group_calories, group_key="TractorLemonade")

    assert lemonade["display_name"] == "Organic Lemonade"
    assert (lemonade["calories_min"], lemonade["calories_max"]) == ("170", "250")
    assert lemonade["display_range_format"] is True


# --- Allergens -------------------------------------------------------------


def test_a_marked_allergen_says_contains(blobs: InMemoryBlobStore) -> None:
    row = one(dataset(blobs).item_allergens, item_id=CHEESE, allergen_code="dair")

    assert row["status"] == AllergenStatus.CONTAINS
    assert row["source_url"] == site.NUTRITION_URL


def test_an_unmarked_allergen_says_not_listed_rather_than_absent(
    blobs: InMemoryBlobStore,
) -> None:
    """Not the same as free of it, and not the same as nothing being known."""
    row = one(dataset(blobs).item_allergens, item_id=STEAK, allergen_code="dair")

    assert row["status"] == AllergenStatus.NOT_LISTED


def test_an_item_no_document_describes_says_not_published(
    blobs: InMemoryBlobStore,
) -> None:
    """Napkins are orderable, so they get rows; nothing is published, so the
    rows say that rather than not existing."""
    parsed = dataset(blobs)

    napkins = [
        row for row in site.rows(parsed.item_allergens) if row["item_id"] == NAPKINS
    ]

    assert len(napkins) == len(parsed.allergen_codes)
    assert {row["status"] for row in napkins} == {AllergenStatus.NOT_PUBLISHED}


def test_the_three_statuses_are_three_different_values(
    blobs: InMemoryBlobStore,
) -> None:
    """The whole point, in one assertion: no pair of them collapses."""
    allergens = dataset(blobs).item_allergens
    statuses = {
        one(allergens, item_id=CHEESE, allergen_code="dair")["status"],
        one(allergens, item_id=STEAK, allergen_code="dair")["status"],
        one(allergens, item_id=NAPKINS, allergen_code="dair")["status"],
    }

    assert statuses == {"CONTAINS", "NOT_LISTED", "NOT_PUBLISHED"}


def test_every_item_on_the_menu_gets_a_statement_about_every_allergen(
    blobs: InMemoryBlobStore,
) -> None:
    parsed = dataset(blobs)
    codes = set(parsed.allergen_codes)

    answered: dict[str, set[str]] = {}
    for row in parsed.item_allergens:
        answered.setdefault(row.item_id, set()).add(row.allergen_code)

    assert NAPKINS in answered
    assert all(covered == codes for covered in answered.values())


def test_an_item_only_the_chart_describes_is_answered_from_the_chart(
    blobs: InMemoryBlobStore,
) -> None:
    row = one(dataset(blobs).item_allergens, item_id=CHICKEN, allergen_code="dair")

    assert row["status"] == AllergenStatus.NOT_LISTED
    assert row["source_url"] == site.ALLERGEN_CHART_URL


def test_which_codes_are_allergens_is_read_from_the_published_classification(
    blobs: InMemoryBlobStore,
) -> None:
    parsed = dataset(blobs)

    assert parsed.allergen_codes == ("dair", "glut", "soy", "sulp")
    gluten = one(parsed.dietary_tags, tag_code="glut")
    assert gluten["kind"] == TagKind.ALLERGEN
    assert gluten["tag_name"] == "Gluten"
    assert gluten["group_name"] == "I'm Avoiding"
    assert gluten["group_subheader"] == "Tagged items contain your selection."
    assert one(parsed.dietary_tags, tag_code="vega")["kind"] == TagKind.DIET


def test_a_code_with_no_published_label_keeps_a_null_one(
    blobs: InMemoryBlobStore,
) -> None:
    """Chipotle spells Whole30 two ways and never writes it out. Neither
    spelling is renamed here, and neither is folded into the other."""
    tags = dataset(blobs).dietary_tags

    from_the_metadata = one(tags, tag_code="wh30")
    from_the_chart = one(tags, tag_code="whol")

    assert from_the_metadata["tag_name"] is None
    assert from_the_metadata["kind"] is None
    assert from_the_chart["tag_name"] is None
    assert from_the_chart["source_url"] == site.ALLERGEN_CHART_URL


def test_the_two_documents_answer_diets_separately(blobs: InMemoryBlobStore) -> None:
    """They disagree about Whole30 and agree about nothing else being wrong,
    so the diet table records who said what instead of picking."""
    diets = dataset(blobs).item_diets

    assert (
        one(diets, item_id=CHICKEN, diet_code="whol", source_url=site.ALLERGEN_CHART_URL)[
            "status"
        ]
        == DietStatus.LISTED
    )
    assert (
        one(diets, item_id=CHICKEN, diet_code="whol", source_url=site.NUTRITION_URL)[
            "status"
        ]
        == DietStatus.NOT_PUBLISHED
    )
    assert (
        one(diets, item_id=CHEESE, diet_code="keto", source_url=site.NUTRITION_URL)[
            "status"
        ]
        == DietStatus.LISTED
    )


def test_the_chart_keeps_two_lines_that_share_an_identifier(
    blobs: InMemoryBlobStore,
) -> None:
    """Chipotle publishes 'Crispy Corn Tortilla' and 'Tortilla Chips' against
    one item identifier. A table keyed on that identifier would lose one."""
    chart = site.rows(dataset(blobs).allergen_chart)

    sharing = [row for row in chart if row["menu_item_id"] == "CMG-1002"]

    assert sorted(row["name"] for row in sharing) == [
        "Crispy Corn Tortilla",
        "Tortilla Chips",
    ]
    assert len({row["sort_order"] for row in sharing}) == 2


def test_the_chart_row_keeps_its_marks_in_published_order(
    blobs: InMemoryBlobStore,
) -> None:
    tortilla = one(dataset(blobs).allergen_chart, name="Flour Tortilla (Burrito)")

    assert tortilla["allergen_codes"] == ["glut", "sulp"]
    assert tortilla["diet_codes"] == ["vege", "vega"]
    assert tortilla["source_url"] == site.ALLERGEN_CHART_URL


# --- The caveats -----------------------------------------------------------


def test_the_published_caveat_survives_verbatim(blobs: InMemoryBlobStore) -> None:
    """The paragraph that makes NOT_LISTED mean 'not marked' rather than
    'free of'. If it stops arriving, the dataset has lost the thing that
    qualifies every negative in it."""
    texts = [row.text for row in dataset(blobs).caveats]

    assert any(
        "Individual foods may come into contact with one another during "
        "preparation, which is not reflected on this chart." in text
        for text in texts
    )
    assert any(
        "Chipotle cannot guarantee the complete absence of these allergens "
        "in its restaurants." in text
        for text in texts
    )
    assert any("all sulphites present" in text.lower() for text in texts)


def test_the_caveats_come_from_the_page_and_not_its_navigation(
    blobs: InMemoryBlobStore,
) -> None:
    parsed = dataset(blobs)

    joined = "\n".join(row.text for row in parsed.caveats)

    assert "ORDER NOW" not in joined
    assert "Privacy Policy" not in joined
    assert [row.position for row in parsed.caveats] == list(range(len(parsed.caveats)))
    assert all(row.source_url == site.ALLERGENS_PAGE_URL for row in parsed.caveats)


def test_a_heading_is_kept_beside_the_block_it_heads(
    blobs: InMemoryBlobStore,
) -> None:
    headings = [row.heading for row in dataset(blobs).caveats if row.heading]

    assert "GLUTEN INTOLERANCE & CELIAC DISEASE" in headings


# --- What stops the harvest ------------------------------------------------


def test_a_page_with_no_prose_stops_the_harvest(blobs: InMemoryBlobStore) -> None:
    """Shipping the allergen data without the caveat that qualifies it is
    worse than not shipping it."""
    transport = site.site(
        extra={
            site.ALLERGENS_PAGE_URL: fake_response(
                site.ALLERGENS_PAGE_URL,
                b"<html><body><main></main></body></html>",
                content_type="text/html",
            )
        }
    )

    with pytest.raises(ChipotleSourceError, match="no text inside <main>"):
        dataset(blobs, transport)


def test_a_chart_with_no_allergens_stops_the_harvest(
    blobs: InMemoryBlobStore,
) -> None:
    payload = json.loads(site.read("allergendiets.json"))
    for row in payload["allergens"]:
        row["allergens"] = []
    transport = site.site(
        extra={
            site.ALLERGEN_CHART_URL: fake_response(
                site.ALLERGEN_CHART_URL, json.dumps(payload).encode()
            )
        }
    )

    with pytest.raises(ChipotleSourceError, match="classified none of its"):
        dataset(blobs, transport)


def test_two_chart_lines_that_disagree_about_one_item_stop_the_harvest(
    blobs: InMemoryBlobStore,
) -> None:
    """Two foods share an identifier. If they ever stop sharing an allergen
    set, neither answer is that identifier's answer."""
    payload = json.loads(site.read("allergendiets.json"))
    for row in payload["allergens"]:
        if row["name"] == "Tortilla Chips":
            row["allergens"] = ["glut"]
    transport = site.site(
        extra={
            site.ALLERGEN_CHART_URL: fake_response(
                site.ALLERGEN_CHART_URL, json.dumps(payload).encode()
            )
        }
    )

    with pytest.raises(ChipotleSourceError, match="published twice with different"):
        dataset(blobs, transport)


def test_two_documents_that_disagree_about_an_allergen_stop_the_harvest(
    blobs: InMemoryBlobStore,
) -> None:
    """The single answer in ``item_allergens`` is only honest while the two
    published sources agree. The moment they do not, choosing between them is
    a judgement, and a parser is the wrong place to make it."""
    payload = json.loads(site.read("allergendiets.json"))
    for row in payload["allergens"]:
        if row["name"] == "Steak":
            row["allergens"] = ["dair"]
    transport = site.site(
        extra={
            site.ALLERGEN_CHART_URL: fake_response(
                site.ALLERGEN_CHART_URL, json.dumps(payload).encode()
            )
        }
    )

    with pytest.raises(ChipotleSourceError, match="disagree about a safety fact"):
        dataset(blobs, transport)


def test_a_page_that_stops_publishing_the_allergen_endpoint_stops_the_harvest(
    blobs: InMemoryBlobStore,
) -> None:
    with pytest.raises(ChipotleSourceError):
        dataset(blobs, site.site(home="home-without-config.html"))


# --- The manifest ----------------------------------------------------------


def test_the_manifest_counts_the_silences(blobs: InMemoryBlobStore) -> None:
    """A harvest that quietly stopped seeing allergen data would otherwise
    look like a successful one. The counts are what make the diff say so."""
    coverage = dataset(blobs).manifest()["coverage"]

    assert coverage["allergen_codes"] == 4
    assert coverage["contains"] > 0
    assert coverage["not_listed"] > 0
    assert coverage["not_published"] == 4, "one item, four allergens"
    assert coverage["nutrient_figures_null"] == 1


def test_the_manifest_names_every_table(blobs: InMemoryBlobStore) -> None:
    manifest = dataset(blobs).manifest()

    assert set(manifest["tables"]) == set(NUTRITION_TABLES)
    assert all(entry["rows"] > 0 for entry in manifest["tables"].values())
