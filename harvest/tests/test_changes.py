"""Diff detection at both levels, and the two ways it could be worthless."""

import json

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.changes import (
    changed_count,
    diff_documents,
    diff_tables,
    render_report,
    row_digest,
    snapshot_documents,
    snapshot_table,
    snapshot_tables,
)
from chip_chat.harvest.testing import EPOCH, fake_response

PREFIX = "parsed/chipotle/menu"
KEYS = {"menu_items": ("item_id",), "item_prices": ("restaurant_id", "item_id")}


def write_table(
    blobs: InMemoryBlobStore, table: str, rows: list[dict[str, object]]
) -> None:
    """Write one table the way the datasets write theirs: compact JSON Lines."""
    body = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    )
    blobs.write(f"{PREFIX}/{table}.jsonl", body.encode("utf-8"))


def item(
    item_id: str, name: str, harvested_at: str = "2026-01-01T12:00:00+00:00"
) -> dict[str, object]:
    """One menu_items row, in the shape the parser writes."""
    return {
        "item_id": item_id,
        "name": name,
        "source_url": "https://example.test/menu",
        "harvested_at": harvested_at,
    }


# --- Documents ---------------------------------------------------------------


def test_the_document_diff_names_what_appeared_changed_and_vanished() -> None:
    before = {"a": "1111", "b": "2222", "c": "3333"}
    after = {"a": "1111", "b": "9999", "d": "4444"}

    by_url = {change.url: change for change in diff_documents(before, after)}

    assert by_url["a"].status == "unchanged"
    assert by_url["b"].status == "changed"
    assert by_url["b"].before == "2222"
    assert by_url["b"].after == "9999"
    assert by_url["c"].status == "removed"
    assert by_url["d"].status == "added"
    assert changed_count(by_url.values()) == 3


def test_snapshotting_reads_the_pointers_and_skips_what_is_not_corpus(
    blobs: InMemoryBlobStore,
) -> None:
    cache = DocumentCache(blobs)
    cache.put("https://example.test/menu", fake_response("x", b"{}"), EPOCH)
    cache.put("https://example.test/robots.txt", fake_response("x", b""), EPOCH)

    assert list(snapshot_documents(cache)) == ["https://example.test/menu"]


# --- Rows --------------------------------------------------------------------


def test_a_re_harvest_that_changed_nothing_reports_nothing(
    blobs: InMemoryBlobStore,
) -> None:
    """The decision this test exists for: every parsed row carries
    ``harvested_at`` and it moves every week. Diff whole rows and the report
    says the entire corpus changed, every time, forever."""
    write_table(blobs, "menu_items", [item("CMG-2", "Steak Burrito")])
    before = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))
    write_table(
        blobs, "menu_items", [item("CMG-2", "Steak Burrito", "2026-02-14T09:00:00+00:00")]
    )
    after = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))

    (change,) = diff_tables({"menu_items": before}, {"menu_items": after})

    assert change.is_quiet
    assert change.unchanged == 1


def test_a_menu_item_disappearing_is_reported_by_name(
    blobs: InMemoryBlobStore,
) -> None:
    """Issue #38's own example."""
    write_table(
        blobs,
        "menu_items",
        [item("CMG-2", "Steak Burrito"), item("CMG-5", "Barbacoa Bowl")],
    )
    before = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))
    write_table(blobs, "menu_items", [item("CMG-2", "Steak Burrito")])
    after = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))

    (change,) = diff_tables({"menu_items": before}, {"menu_items": after})

    assert [row.key for row in change.removed] == ["CMG-5"]
    assert change.rows_before == 2
    assert change.rows_after == 1


def test_a_renamed_item_is_a_modification_rather_than_a_swap(
    blobs: InMemoryBlobStore,
) -> None:
    write_table(blobs, "menu_items", [item("CMG-2", "Steak Burrito")])
    before = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))
    write_table(blobs, "menu_items", [item("CMG-2", "Steak Burrito (New!)")])
    after = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))

    (change,) = diff_tables({"menu_items": before}, {"menu_items": after})

    assert [row.key for row in change.modified] == ["CMG-2"]
    assert not change.added
    assert not change.removed


def test_a_compound_key_reads_as_one_string(blobs: InMemoryBlobStore) -> None:
    rows: list[dict[str, object]] = [
        {"restaurant_id": "0679", "item_id": "CMG-2", "unit_price": "11.15"},
    ]
    write_table(blobs, "item_prices", rows)
    before = snapshot_table(blobs, PREFIX, "item_prices", KEYS["item_prices"])
    rows[0]["unit_price"] = "11.65"
    write_table(blobs, "item_prices", rows)
    after = snapshot_table(blobs, PREFIX, "item_prices", KEYS["item_prices"])

    (change,) = diff_tables({"item_prices": before}, {"item_prices": after})

    assert [row.key for row in change.modified] == ["0679/CMG-2"]


def test_a_key_that_collides_degrades_the_diff_rather_than_lying(
    blobs: InMemoryBlobStore,
) -> None:
    """A hand-declared identity can be wrong. When it is, the table is diffed
    on contents instead: less informative, never untrue."""
    write_table(
        blobs, "menu_items", [item("CMG-2", "Steak Burrito"), item("CMG-2", "Duplicate")]
    )

    snapshot = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))

    assert not snapshot.keyed
    assert len(snapshot.rows) == 2
    (change,) = diff_tables(None, {"menu_items": snapshot})
    assert not change.keyed


def test_a_first_release_reports_every_row_as_added(
    blobs: InMemoryBlobStore,
) -> None:
    write_table(blobs, "menu_items", [item("CMG-2", "Steak Burrito")])

    (change,) = diff_tables(
        None, snapshot_tables(blobs, PREFIX, {"menu_items": ("item_id",)})
    )

    assert change.rows_before is None
    assert [row.key for row in change.added] == ["CMG-2"]


def test_a_table_that_was_never_written_is_not_a_table_of_no_rows(
    blobs: InMemoryBlobStore,
) -> None:
    """The PDF dataset legitimately produces empty tables; a missing prefix is
    a different thing and the report must not read them the same way."""
    missing = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))
    write_table(blobs, "menu_items", [])
    empty = snapshot_table(blobs, PREFIX, "menu_items", ("item_id",))

    assert not missing.present
    assert empty.present
    assert empty.rows == {}


def test_the_comparison_digest_ignores_only_when_we_looked() -> None:
    steady = {"item_id": "CMG-2", "name": "Steak Burrito"}
    assert row_digest({**steady, "harvested_at": "2026-01-01"}) == row_digest(
        {**steady, "harvested_at": "2026-08-26"}
    )
    assert row_digest({**steady, "source_url": "a"}) != row_digest(
        {**steady, "source_url": "b"}
    )


# --- The report --------------------------------------------------------------


def report(**overrides: object) -> str:
    """Render a report with everything defaulted but what a test cares about."""
    arguments: dict[str, object] = {
        "run_id": "20260826T120000Z",
        "started_at": "2026-08-26T12:00:00+00:00",
        "finished_at": "2026-08-26T12:04:00+00:00",
        "ok": True,
        "documents": (),
        "tables": (),
        "requests_made": 0,
        "revalidations": 0,
        "bytes_fetched": 0,
        "freshness": "Corpus freshness: 0 documents",
    }
    arguments.update(overrides)
    return render_report(**arguments)  # type: ignore[arg-type]


def test_a_quiet_week_says_so_rather_than_showing_empty_tables() -> None:
    documents = diff_documents({"a": "1"}, {"a": "1"})

    rendered = report(documents=documents)

    assert "No document changed" in rendered
    assert "No parsed row changed." in rendered


def test_a_failed_run_says_it_published_nothing() -> None:
    rendered = report(ok=False, failures={"run": "TransientFetchError: HTTP 503"})

    assert "FAILED — nothing published" in rendered
    assert "HTTP 503" in rendered


def test_the_report_states_what_the_re_harvest_did_not_download() -> None:
    rendered = report(requests_made=57, revalidations=54, bytes_fetched=12288)

    assert "57, of which 54 were answered 304" in rendered
    assert "12.0 KiB" in rendered


def test_a_truncated_row_list_says_how_many_it_did_not_show(
    blobs: InMemoryBlobStore,
) -> None:
    """Showing twenty silently reads as if only twenty changed."""
    write_table(blobs, "menu_items", [item(f"CMG-{n}", f"Item {n}") for n in range(30)])

    rendered = report(
        tables=diff_tables(
            None, snapshot_tables(blobs, PREFIX, {"menu_items": ("item_id",)})
        )
    )

    assert "and 10 more" in rendered
