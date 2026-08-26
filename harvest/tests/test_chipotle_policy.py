"""Harvesting and parsing the policy half of the corpus.

Everything here runs against the fixture site through the framework's fake
transport. No test in this file may reach a real network — the point of the
fetch-once cache is a property that can only be *proved* by a transport that
records every call and is then asserted to have recorded the right ones.
"""

from dataclasses import replace
from decimal import Decimal

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.sources.chipotle import (
    ChipotleSourceError,
    MissingDocumentError,
    PolicyDataset,
    PolicyDocuments,
    harvest_policy,
    load_policy,
    parse_policy,
)
from chip_chat.harvest.sources.chipotle.policy import bundle_url, catering_key
from chip_chat.harvest.testing import FakeClock, FakeTransport, fake_response

STORE_COUNT = 32
"""Two more than issue #21's floor, so the tests exercise the check rather
than sit exactly on it, and so a run stays quick."""


@pytest.fixture
def transport() -> FakeTransport:
    """The fixture site, recording every request made of it."""
    return site.site()


@pytest.fixture
def harvester(blobs: InMemoryBlobStore, transport: FakeTransport) -> Harvester:
    """A harvester wired to the fixture site and a clock that never really sleeps."""
    clock = FakeClock()
    return Harvester(
        blobs,
        transport,
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(2.0, clock), 1),
    )


@pytest.fixture
def documents(harvester: Harvester) -> PolicyDocuments:
    """The harvested documents, from a cold cache."""
    return harvest_policy(harvester, store_count=STORE_COUNT)


@pytest.fixture
def dataset(documents: PolicyDocuments) -> PolicyDataset:
    """The parsed dataset."""
    return parse_policy(documents)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def test_a_warm_cache_costs_the_site_nothing(
    harvester: Harvester, transport: FakeTransport, documents: PolicyDocuments
) -> None:
    made = len(transport.requests)

    harvest_policy(harvester, store_count=STORE_COUNT)

    assert len(transport.requests) == made


def test_the_offline_load_returns_what_the_harvest_landed(
    blobs: InMemoryBlobStore, transport: FakeTransport, documents: PolicyDocuments
) -> None:
    made = len(transport.requests)

    reloaded = load_policy(DocumentCache(blobs), store_count=STORE_COUNT)

    assert len(transport.requests) == made
    assert parse_policy(reloaded).manifest() == parse_policy(documents).manifest()


def test_the_offline_load_says_so_when_nothing_was_harvested() -> None:
    with pytest.raises(MissingDocumentError, match="run the harvest first"):
        load_policy(DocumentCache(InMemoryBlobStore()), store_count=STORE_COUNT)


def test_the_catering_key_is_read_rather_than_remembered(
    documents: PolicyDocuments, transport: FakeTransport
) -> None:
    """It is not the key the www page publishes, and neither is in this repo."""
    key = catering_key(documents.catering_bundle.text, "bundle")

    assert key == site.CATERING_KEY
    assert key != documents.services.app_key
    call = next(
        request for request in transport.requests if request.url == site.CATERING_MENU_URL
    )
    assert call.headers["Ocp-Apim-Subscription-Key"] == site.CATERING_KEY


def test_the_bundle_address_is_read_off_the_catering_page() -> None:
    html = site.read("catering-home.html").decode()

    assert bundle_url(html, site.CATERING_URL) == site.CATERING_BUNDLE_URL


def test_a_catering_page_with_no_bundle_raises() -> None:
    with pytest.raises(ChipotleSourceError, match="no application bundle"):
        bundle_url("<html><body></body></html>", site.CATERING_URL)


def test_a_bundle_with_no_key_raises() -> None:
    with pytest.raises(ChipotleSourceError, match="no longer publishes a catering"):
        catering_key("function noop(){}", "bundle")


def test_the_restaurant_endpoint_comes_from_the_page_that_advertises_it(
    documents: PolicyDocuments,
) -> None:
    assert documents.services.restaurant_path == "/restaurant/v3/restaurant"
    assert documents.store_profiles[0].source_url.startswith(
        f"{site.SERVICES}/restaurant/v3/restaurant/"
    )


def test_a_page_that_stopped_advertising_the_restaurant_endpoint_raises(
    documents: PolicyDocuments,
) -> None:
    services = replace(documents.services, restaurant_path=None)

    with pytest.raises(ChipotleSourceError, match="nowhere left to read a store's name"):
        services.restaurant_url("679")


# --------------------------------------------------------------------------
# Rewards
# --------------------------------------------------------------------------


def test_the_published_rewards_keep_their_point_costs(dataset: PolicyDataset) -> None:
    assert [(reward.name, reward.point_cost) for reward in dataset.rewards] == [
        ("SIDE TORTILLA", 85),
        ("Chips", 350),
        ("ENTRÉE", 1625),
    ]


def test_a_reward_keeps_the_picture_it_was_published_with(
    dataset: PolicyDataset,
) -> None:
    """Kept verbatim, and deliberately not resolved into an item identifier."""
    assert dataset.rewards[0].image_path is not None
    assert dataset.rewards[0].image_path.endswith("order.png")


def test_a_rewards_page_with_no_tiles_raises(documents: PolicyDocuments) -> None:
    stripped = replace(
        documents.rewards,
        content=site.read("rewards.html").replace(b"aem-flip-tile", b"x"),
    )

    with pytest.raises(ChipotleSourceError, match="publishes no reward tiles"):
        parse_policy(replace(documents, rewards=stripped))


# --------------------------------------------------------------------------
# Policy documents
# --------------------------------------------------------------------------


def test_the_terms_land_as_sections_rather_than_one_string(
    dataset: PolicyDataset,
) -> None:
    terms = [
        section
        for section in dataset.policy_sections
        if section.document_id == "rewards-terms"
    ]

    assert len(terms) > 1
    assert [section.position for section in terms] == list(range(len(terms)))
    assert "ELIGIBILITY" in {section.heading for section in terms}


def test_every_section_carries_the_url_it_came_from(dataset: PolicyDataset) -> None:
    for section in dataset.policy_sections:
        assert section.source_url.startswith("https://www.chipotle.com/")
        assert section.harvested_at is not None


def test_the_documents_are_labelled_by_what_they_are(dataset: PolicyDataset) -> None:
    kinds = {row.document_id: row.kind for row in dataset.policy_documents}

    assert kinds == {"rewards-terms": "TERMS", "rewards": "OVERVIEW"}


def test_a_document_row_counts_its_own_sections(dataset: PolicyDataset) -> None:
    for row in dataset.policy_documents:
        sections = [
            section
            for section in dataset.policy_sections
            if section.document_id == row.document_id
        ]
        assert row.section_count == len(sections)


# --------------------------------------------------------------------------
# The FAQ
# --------------------------------------------------------------------------


def test_the_faq_keeps_its_two_level_structure(dataset: PolicyDataset) -> None:
    headings = [(row.category, row.subcategory) for row in dataset.faq_categories]

    assert ("Rewards Program", "Rewards Program") in headings
    assert ("Delivery", "Payment") in headings
    assert ("Catering", "GENERAL") in headings


def test_every_faq_heading_counts_its_own_entries(dataset: PolicyDataset) -> None:
    for row in dataset.faq_categories:
        entries = [
            entry
            for entry in dataset.faq_entries
            if entry.category == row.category and entry.subcategory == row.subcategory
        ]
        assert row.entry_count == len(entries)


def test_the_refund_answer_is_in_there(dataset: PolicyDataset) -> None:
    """The question issue #21 says visitors actually ask second."""
    entry = next(
        entry for entry in dataset.faq_entries if "refund requests" in entry.question
    )

    assert entry.category == "Delivery"
    assert "Customer Care" in entry.answer


def test_an_answer_keeps_its_paragraphs_apart(dataset: PolicyDataset) -> None:
    entry = next(
        entry
        for entry in dataset.faq_entries
        if entry.question.startswith("How do I get Chipotle Rewards points")
    )

    assert "\n" in entry.answer
    assert "  " not in entry.answer
    assert entry.answer.startswith("You can earn rewards points for catering if:")


def test_a_url_that_only_existed_in_a_link_survives(dataset: PolicyDataset) -> None:
    entry = next(
        entry
        for entry in dataset.faq_entries
        if entry.question.startswith("Where can I go to learn more")
    )

    assert "https://www.ewg.org/" in entry.links
    assert len(entry.links) == 5


def test_a_bulleted_answer_keeps_its_bullets_apart(dataset: PolicyDataset) -> None:
    entry = next(
        entry
        for entry in dataset.faq_entries
        if entry.question.startswith("Where can I go to learn more")
    )

    assert entry.answer.count("\n") >= 4


def test_an_answer_node_this_source_does_not_know_raises(
    documents: PolicyDocuments,
) -> None:
    mangled = replace(
        documents.faq,
        content=documents.faq.content.replace(b'"paragraph"', b'"interpretive-dance"'),
    )

    with pytest.raises(ChipotleSourceError, match="interpretive-dance"):
        parse_policy(replace(documents, faq=mangled))


def test_a_faq_document_with_no_items_raises(documents: PolicyDocuments) -> None:
    empty = replace(documents.faq, content=b'{"data":{"faqsList":{"items":[]}}}')

    with pytest.raises(ChipotleSourceError, match="published no FAQ items"):
        parse_policy(replace(documents, faq=empty))


# --------------------------------------------------------------------------
# Catering
# --------------------------------------------------------------------------


def test_catering_exists_and_says_roughly_how_it_works(
    dataset: PolicyDataset,
) -> None:
    packages = {row.package_id: row for row in dataset.catering_packages}

    burritos = packages["CMG-4012"]
    assert burritos.display_name == "Burritos by the Box"
    assert burritos.min_quantity == 6
    assert burritos.display_unit == "burrito"


def test_a_catering_price_is_an_exact_decimal(dataset: PolicyDataset) -> None:
    burritos = next(
        row for row in dataset.catering_packages if row.package_id == "CMG-4012"
    )

    assert burritos.min_price == Decimal("8.75")
    assert burritos.max_price == Decimal("12.5")


def test_a_package_with_no_published_price_keeps_none(
    dataset: PolicyDataset,
) -> None:
    """The delivery charge is a line on the order, not a price per unit."""
    delivery = next(
        row for row in dataset.catering_packages if row.package_id == "CMG-4014"
    )

    assert (delivery.min_price, delivery.max_price) == (None, None)


def test_what_a_customer_chooses_and_what_simply_comes_with_it_are_told_apart(
    dataset: PolicyDataset,
) -> None:
    burritos = [
        row for row in dataset.catering_package_options if row.package_id == "CMG-4012"
    ]

    chosen = {row.name for row in burritos if not row.is_included}
    included = {row.name for row in burritos if row.is_included}
    assert "Chicken" in chosen
    assert "Black Beans" in included


def test_a_slot_keeps_the_name_chipotle_published_it_under(
    dataset: PolicyDataset,
) -> None:
    slots = {row.slot for row in dataset.catering_package_options}

    assert "premiumToppings" in slots


def test_how_many_of_each_slot_a_customer_picks_is_recorded(
    dataset: PolicyDataset,
) -> None:
    build_your_own = next(
        row for row in dataset.catering_packages if row.package_id == "CMG-4105"
    )

    assert build_your_own.protein_count == 1
    assert build_your_own.base_count == 2


def test_a_catering_menu_with_no_packages_raises(documents: PolicyDocuments) -> None:
    empty = replace(documents.catering_menu, content=b'{"restaurantNumber":0,"menu":[]}')

    with pytest.raises(ChipotleSourceError, match="publishes no catering packages"):
        parse_policy(replace(documents, catering_menu=empty))


# --------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------


def test_enough_real_stores_land(dataset: PolicyDataset) -> None:
    assert len(dataset.stores) == STORE_COUNT
    assert len({store.region for store in dataset.stores}) > 20


def test_the_restaurant_the_prices_belong_to_is_one_of_them(
    dataset: PolicyDataset,
) -> None:
    assert dataset.reference_restaurant_id == 679
    assert 679 in {store.store_id for store in dataset.stores}


def test_a_store_carries_a_city_and_a_region(dataset: PolicyDataset) -> None:
    lakewood = next(store for store in dataset.stores if store.store_id == 679)

    assert (lakewood.city, lakewood.region) == ("Lakewood", "CA")
    assert lakewood.page_url.endswith("/ca/lakewood/5310-lakewood-blvd")


def test_the_store_name_comes_from_the_endpoint_that_publishes_one(
    dataset: PolicyDataset,
) -> None:
    """The locator calls every restaurant "Chipotle Mexican Grill"."""
    profile = next(row for row in dataset.store_profiles if row.store_id == 679)

    assert profile.name == "Lakewood Mall"
    assert profile.status == "OPEN"
    assert (
        profile.source_url
        != next(store for store in dataset.stores if store.store_id == 679).source_url
    )


def test_every_store_has_a_row_for_every_day(dataset: PolicyDataset) -> None:
    assert len(dataset.store_hours) == len(dataset.stores) * 7


def test_a_day_nobody_published_is_a_row_saying_so(dataset: PolicyDataset) -> None:
    unpublished = [row for row in dataset.store_hours if not row.is_published]

    assert unpublished
    assert all(row.opens is None and row.closes is None for row in unpublished)


def test_a_published_closure_is_not_the_same_row(dataset: PolicyDataset) -> None:
    closed = [
        row for row in dataset.store_hours if row.is_published and row.opens is None
    ]

    assert closed


def test_too_few_stores_raises(harvester: Harvester) -> None:
    documents = harvest_policy(harvester, store_count=5)

    with pytest.raises(ChipotleSourceError, match="fewer than the 30"):
        parse_policy(documents)


def test_losing_the_reference_restaurant_raises(documents: PolicyDocuments) -> None:
    without = tuple(
        document
        for document in documents.stores
        if not document.source_url.endswith("5310-lakewood-blvd")
    )
    profiles = tuple(
        document
        for document in documents.store_profiles
        if not document.source_url.endswith("/679")
    )

    with pytest.raises(ChipotleSourceError, match="no locator page published it"):
        parse_policy(replace(documents, stores=without, store_profiles=profiles))


# --------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------


def test_two_parses_of_one_cache_produce_identical_bytes(
    documents: PolicyDocuments,
) -> None:
    first = parse_policy(documents)
    second = parse_policy(documents)

    assert first.manifest() == second.manifest()


def test_the_tables_are_written_where_the_manifest_says(
    blobs: InMemoryBlobStore, dataset: PolicyDataset
) -> None:
    written = dataset.write(blobs, "parsed/chipotle/policy")

    assert written["stores"] == "parsed/chipotle/policy/stores.jsonl"
    assert written["manifest"] == "parsed/chipotle/policy/manifest.json"
    assert set(dataset.manifest()["tables"]) | {"manifest"} == set(written)


def test_a_table_that_does_not_exist_is_a_key_error(dataset: PolicyDataset) -> None:
    with pytest.raises(KeyError, match="no such table"):
        dataset.table("burritos")


def test_a_document_that_is_not_json_says_which_one(
    documents: PolicyDocuments,
) -> None:
    mangled = replace(documents.catering_menu, content=b"<html>not json</html>")

    with pytest.raises(ChipotleSourceError, match="is not valid JSON"):
        parse_policy(replace(documents, catering_menu=mangled))


def test_the_harvest_reaches_every_origin_the_dataset_needs(
    transport: FakeTransport, documents: PolicyDocuments
) -> None:
    origins = {url.split("/")[2] for url in transport.urls}

    assert origins == {
        "www.chipotle.com",
        "services.chipotle.com",
        "catering.chipotle.com",
        "locations.chipotle.com",
    }


def test_nothing_was_fetched_twice(
    transport: FakeTransport, documents: PolicyDocuments
) -> None:
    """The framework's fetch-once promise, from this source's side of it."""
    assert len(transport.urls) == len(set(transport.urls))


def test_the_robots_file_of_every_origin_was_read_first(
    transport: FakeTransport, documents: PolicyDocuments
) -> None:
    for origin in ("catering.chipotle.com", "locations.chipotle.com"):
        robots = f"https://{origin}/robots.txt"
        assert robots in transport.urls
        first = next(url for url in transport.urls if url.split("/")[2] == origin)
        assert first == robots


def test_the_fake_response_helper_is_not_serving_a_real_site(
    transport: FakeTransport,
) -> None:
    """A guard on the guard: this suite must never reach Chipotle."""
    assert isinstance(transport, FakeTransport)
    assert fake_response("https://example.test/", b"{}").status_code == 200
