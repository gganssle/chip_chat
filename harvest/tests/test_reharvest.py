"""The weekly re-harvest, end to end against the fixture site.

The four acceptance criteria of issue #38 are all claims about a run, so they
are asserted against runs rather than against the pieces they are built from.
In particular the third — *a simulated partial-harvest failure leaves the live
index untouched* — is a test and not a paragraph: a run is made to die in the
middle, and the live corpus is then shown to be byte-for-byte what it was.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.blobs import BlobStore, LocalBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.release import ReleaseStore, read_current
from chip_chat.harvest.sources.chipotle.reharvest import (
    build_parser,
    run,
    staged_prefix,
    table_keys,
)
from chip_chat.harvest.testing import FakeClock, FakeTransport, fake_response
from chip_chat.harvest.transport import HttpResponse

STORES = 30
WEEK = timedelta(days=7)


def harvester(blobs: BlobStore, transport: FakeTransport, clock: FakeClock) -> Harvester:
    """A harvester with its own gate, so the tests do not share a rate limit."""
    return Harvester(
        blobs,
        transport,
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(0.0, clock), 1),
    )


def reharvest(
    blobs: BlobStore,
    transport: FakeTransport,
    clock: FakeClock,
    *,
    max_age: timedelta | None = None,
) -> tuple[dict[str, Any], str]:
    """One weekly run over the fixture site."""
    with harvester(blobs, transport, clock) as subject:
        return run(
            blobs,
            subject,
            None,
            clock=clock,
            restaurants=[site.REFERENCE],
            store_count=STORES,
            max_age=max_age,
        )


@pytest.fixture
def landing(tmp_path: Path) -> LocalBlobStore:
    """An empty landing zone on disk."""
    return LocalBlobStore(tmp_path)


@pytest.fixture
def clock() -> FakeClock:
    """A clock the test drives, so a week can pass in a microsecond."""
    return FakeClock()


# --- A first run -------------------------------------------------------------


def test_a_first_run_publishes_and_reports_everything_as_new(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    record, report = reharvest(landing, site.site(), clock)

    assert record["ok"] is True
    assert record["previous_release"] is None
    assert record["documents"] > 0
    assert record["changed"] == record["documents"]

    live = read_current(landing)
    assert live is not None
    assert live.run_id == record["run_id"]
    assert "Corpus change report" in report


def test_the_published_corpus_is_addressed_through_the_pointer(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    """Nothing is copied to make a release live, so the pointer is the address
    and one write of it is the whole swap."""
    reharvest(landing, site.site(), clock)

    live = read_current(landing)
    assert live is not None
    assert landing.exists(f"{staged_prefix(live.prefix, 'menu')}/menu_items.jsonl")


def test_the_report_lands_where_the_release_says_it_did(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    record, report = reharvest(landing, site.site(), clock)

    stored = landing.read(str(record["report_key"]))
    assert stored is not None
    assert stored.decode() == report


# --- A quiet week ------------------------------------------------------------


def test_a_second_run_over_an_unchanged_site_changes_nothing(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    reharvest(landing, site.site(), clock)
    clock.advance(WEEK.total_seconds())

    record, report = reharvest(landing, site.site(), clock)

    assert record["ok"] is True
    assert record["changed"] == 0
    assert record["table_changes"] == []
    assert "No document changed" in report
    assert "No parsed row changed." in report


def test_a_quiet_week_still_moves_the_corpus_freshness_forward(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    """The distinction the whole ticket turns on: nothing changed is not the
    same as nothing was checked."""
    reharvest(landing, site.site(), clock)
    clock.advance(WEEK.total_seconds())

    record, _ = reharvest(landing, site.site(), clock)

    freshness = record["freshness"]
    assert isinstance(freshness, dict)
    assert freshness["max_age_days"] == 0.0
    assert freshness["changed_last_release"] == 0


def test_an_unchanged_page_that_offers_a_validator_is_not_downloaded_again(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    """Issue #38's "does not re-fetch what has not changed", as two numbers."""
    served = site.site(
        extra={
            site.INGREDIENTS_URL: fake_response(
                site.INGREDIENTS_URL, site.read("ingredients.json"), etag='"ing-1"'
            )
        }
    )
    reharvest(landing, served, clock)
    clock.advance(WEEK.total_seconds())

    second = site.site(
        extra={
            site.INGREDIENTS_URL: fake_response(
                site.INGREDIENTS_URL, b"", status_code=304
            )
        }
    )
    record, _ = reharvest(landing, second, clock)

    assert record["revalidations"] == 1
    assert record["changed"] == 0
    # The body is still there, and still the one the parser reads.
    document = DocumentCache(landing).get(site.INGREDIENTS_URL)
    assert document is not None
    assert document.content == site.read("ingredients.json")


# --- A week in which something moved -----------------------------------------


def test_a_changed_price_is_reported_by_item_and_restaurant(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    reharvest(landing, site.site(), clock)
    clock.advance(WEEK.total_seconds())

    menu = json.loads(site.read(f"onlinemenu-{site.REFERENCE}.json"))
    entry = menu["entrees"][0]
    entry["unitPrice"] = entry["unitPrice"] + 1.0
    url = site.menu_url(site.REFERENCE)
    record, report = reharvest(
        landing,
        site.site(extra={url: fake_response(url, json.dumps(menu).encode())}),
        clock,
    )

    assert record["changed"] == 1
    # The parser normalises the restaurant id to Chipotle's own integer form,
    # so the key reads 679 rather than the zero-padded 0679 the URL carries.
    priced = f"{int(site.REFERENCE)}/{entry['itemId']}"
    assert _table(record, "item_prices")["modified"] == [priced]
    assert priced in report


def test_a_menu_item_disappearing_is_reported_by_its_identifier(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    """Issue #38's own example, on the real parse rather than on a fixture
    of the diff."""
    reharvest(landing, site.site(), clock)
    clock.advance(WEEK.total_seconds())

    menu = json.loads(site.read(f"onlinemenu-{site.REFERENCE}.json"))
    dropped = menu["entrees"].pop()["itemId"]
    url = site.menu_url(site.REFERENCE)
    record, report = reharvest(
        landing,
        site.site(extra={url: fake_response(url, json.dumps(menu).encode())}),
        clock,
    )

    assert dropped in _table(record, "menu_items")["removed"]
    assert "**removed**" in report


# --- The third acceptance criterion ------------------------------------------


def test_a_partial_harvest_leaves_the_live_corpus_untouched(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    """Simulated exactly as issue #38 asks: a run that dies in the middle. The
    nutrition endpoint answers 503 on every attempt, which is a transient
    failure the framework retries and then gives up on — so the menu documents
    have already been re-harvested and written before the run falls over."""
    reharvest(landing, site.site(), clock)
    published = read_current(landing)
    assert published is not None
    before = _released_tables(landing, published.prefix)

    clock.advance(WEEK.total_seconds())
    record, report = reharvest(
        landing,
        site.site(
            extra={
                site.NUTRITION_URL: fake_response(
                    site.NUTRITION_URL, b"upstream is unwell", status_code=503
                )
            }
        ),
        clock,
    )

    assert record["ok"] is False
    assert "503" in str(record["failure"])
    assert "FAILED — nothing published" in report

    still = read_current(landing)
    assert still is not None
    assert still.run_id == published.run_id
    assert _released_tables(landing, still.prefix) == before


def test_a_failed_run_still_leaves_a_report_to_read(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    """A weekly job whose only artefact on failure is a log line is a job whose
    failures nobody can compare."""
    record, _ = reharvest(
        landing,
        site.site(
            extra={
                site.NUTRITION_URL: fake_response(
                    site.NUTRITION_URL, b"", status_code=503
                )
            }
        ),
        clock,
    )

    store = ReleaseStore(landing)
    assert landing.exists(store.record_key(str(record["run_id"])))
    assert landing.exists(store.report_key(str(record["run_id"])))
    assert read_current(landing) is None


def test_a_failed_first_run_publishes_nothing_at_all(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    """There is no previous release to fall back to, and inventing one would be
    worse than having none."""
    reharvest(
        landing,
        FakeTransport({}, default=_unwell()),
        clock,
    )

    assert read_current(landing) is None


# --- Freshness, as the run reports it ----------------------------------------


def test_a_successful_run_reports_itself_as_the_last_successful_harvest(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    record, _ = reharvest(landing, site.site(), clock, max_age=timedelta(days=8))

    freshness = record["freshness"]
    assert isinstance(freshness, dict)
    assert freshness["last_release_id"] == record["run_id"]
    assert record["stale"] is False


def test_a_failed_run_reports_the_corpus_it_did_not_refresh_as_stale(
    landing: LocalBlobStore, clock: FakeClock
) -> None:
    reharvest(landing, site.site(), clock)
    clock.advance(timedelta(days=40).total_seconds())

    record, _ = reharvest(
        landing, FakeTransport({}, default=_unwell()), clock, max_age=timedelta(days=8)
    )

    assert record["ok"] is False
    assert record["stale"] is True


# --- The command's shape -----------------------------------------------------


def test_the_weekly_default_reads_fewer_stores_than_a_one_off_harvest() -> None:
    """Store profiles are the bulk of the requests and the least of what moves."""
    defaults = build_parser().parse_args(["--landing", "landing"])

    assert defaults.stores == STORES
    assert defaults.max_age_days == 8


def test_every_dataset_declares_an_identity_for_every_table_it_writes() -> None:
    """A table with no declared key would silently diff on contents forever."""
    from chip_chat.harvest.sources.chipotle.reharvest import MODULES

    for name, module in MODULES.items():
        assert tuple(table_keys()[name]) == module.TABLES


# --- Helpers -----------------------------------------------------------------


def _unwell() -> HttpResponse:
    """A 503 for every URL, including ``robots.txt``."""
    return fake_response("https://example.test/", b"upstream is unwell", status_code=503)


def _table(record: dict[str, Any], name: str) -> dict[str, Any]:
    """Return one table's entry from a run record's table changes."""
    changes = record["table_changes"]
    assert isinstance(changes, list)
    matches = [change for change in changes if change["table"] == name]
    assert matches, f"{name} is not among {[c['table'] for c in changes]}"
    return dict(matches[0])


def _released_tables(blobs: BlobStore, prefix: str) -> dict[str, bytes | None]:
    """Every parsed table of one release, as bytes, for a literal comparison."""
    return {
        f"{dataset}/{table}": blobs.read(
            f"{staged_prefix(prefix, dataset)}/{table}.jsonl"
        )
        for dataset, keys in table_keys().items()
        for table in keys
    }
