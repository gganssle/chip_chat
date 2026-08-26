"""Finding a PDF, reading it as a table, and checking it against the calculator.

The end-to-end assertions run against real bytes and a real recording: the
fixture nutrition sheet, and what Azure Document Intelligence actually returned
for it. What the reconciliation is checked against is real too — the fixture's
figures are Chipotle's published ones — so a test that says "the sheet and the
calculator agree about steak at 150 calories" is comparing two numbers Chipotle
published, not two numbers this repository invented.

The one number that is not Chipotle's is the sheet's sodium figure for Cheese,
which reads 260 mg where the calculator publishes 190. It is there because a
reconciliation that has never seen a disagreement has not been shown to notice
one, and noticing is the whole job: issue #22 says a mismatch is a finding, not
a merge conflict to resolve silently.

And the first fact any of this rests on is the smallest: **Chipotle publishes no
PDFs.** Not on the home page, the allergen page, the nutrition calculator, the
ingredients page, the rewards pages or the catering site, as of 26 August 2026.
So the tests below are about a capability that will fire when a sheet appears,
and the manifest a run produces today says, in as many words, that it looked and
found nothing.
"""

import hashlib
from decimal import Decimal
from typing import Any

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.analysis import (
    DEFAULT_API_VERSION,
    DEFAULT_MODEL_ID,
    AnalysisCache,
)
from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.errors import DocumentAnalysisError
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.layout import COLUMN_HEADER, CONTENT
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.sources.chipotle import (
    PDF_TABLES,
    Finding,
    PdfDataset,
    analyze_pdfs,
    cached_analyses,
    discover_pdf_urls,
    documents_of,
    harvest_menu,
    harvest_nutrition,
    harvest_pdfs,
    load_pdfs,
    parse_menu,
    parse_nutrition,
    parse_pdfs,
)
from chip_chat.harvest.testing import (
    FakeClock,
    FakeDocumentAnalyzer,
    FakeTransport,
    fake_response,
)

SHEET_DIGEST = hashlib.sha256(site.nutrition_sheet()).hexdigest()
CHEESE = "CMG-5252"
STEAK_BURRITO = "CMG-2"


def _harvester(transport: FakeTransport, blobs: InMemoryBlobStore) -> Harvester:
    """A harvester over the fake site with the politeness waits made instant."""
    return Harvester(
        blobs,
        transport,
        clock=FakeClock(),
        gate=PolitenessGate(RateLimiter(0.0, clock=FakeClock())),
    )


def _sheet_response(url: str = site.NUTRITION_SHEET_URL) -> Any:
    return fake_response(url, site.nutrition_sheet(), content_type="application/pdf")


def _build(
    extra: dict[str, object] | None = None,
) -> tuple[PdfDataset, FakeDocumentAnalyzer]:
    """Harvest the fake site, find its PDFs, read them, and reconcile."""
    blobs = InMemoryBlobStore()
    transport = site.site(extra=extra)
    analyzer = FakeDocumentAnalyzer({SHEET_DIGEST: site.nutrition_sheet_layout()})
    with _harvester(transport, blobs) as harvester:
        menu = parse_menu(harvest_menu(harvester, [site.REFERENCE]))
        nutrition_documents = harvest_nutrition(harvester, [site.REFERENCE])
        nutrition = parse_nutrition(nutrition_documents)
        found = harvest_pdfs(harvester, documents_of(nutrition_documents.documents()))
        analyzed = analyze_pdfs(
            found.pdfs, analyzer, AnalysisCache(blobs), clock=FakeClock()
        )
    return parse_pdfs(found, analyzed, menu, nutrition), analyzer


@pytest.fixture
def linked() -> dict[str, object]:
    """A fake site whose allergen page links to the nutrition sheet."""
    return {
        site.ALLERGENS_PAGE_URL: site.allergens_page_linking_to(site.NUTRITION_SHEET_URL),
        site.NUTRITION_SHEET_URL: _sheet_response(),
    }


# --- Discovery --------------------------------------------------------------


def test_chipotle_publishes_no_pdfs_and_the_harvest_says_so() -> None:
    """The finding of 26 August 2026, kept where a change would be noticed.

    Every page this project harvests was swept for links ending in ``.pdf``
    and none had one. If that stops being true, this test fails and the
    dataset stops being empty — which is the outcome wanted either way.
    """
    dataset, analyzer = _build()
    assert dataset.discovered_urls == ()
    assert dataset.pdf_documents == ()
    assert analyzer.analyses == []
    assert dataset.coverage()["pdfs"] == 0


def test_a_link_is_found_wherever_on_the_page_it_is() -> None:
    document = _cached(
        site.page_linking_to("/content/dam/sheet.pdf", page_url="https://x.test/a")
    )
    assert discover_pdf_urls([document]) == ("https://x.test/content/dam/sheet.pdf",)


def test_a_link_with_a_cache_buster_is_still_a_pdf() -> None:
    document = _cached(
        site.page_linking_to("https://x.test/sheet.pdf?v=3", page_url="https://x.test/a")
    )
    assert discover_pdf_urls([document]) == ("https://x.test/sheet.pdf?v=3",)


def test_a_page_about_pdfs_is_not_a_pdf() -> None:
    document = _cached(
        site.page_linking_to("https://x.test/search?q=pdf", page_url="https://x.test/a")
    )
    assert discover_pdf_urls([document]) == ()


def test_the_same_sheet_linked_from_two_pages_is_one_url() -> None:
    documents = [
        _cached(
            site.page_linking_to("https://x.test/s.pdf", page_url="https://x.test/a")
        ),
        _cached(
            site.page_linking_to("https://x.test/s.pdf", page_url="https://x.test/b")
        ),
    ]
    assert discover_pdf_urls(documents) == ("https://x.test/s.pdf",)


def test_json_and_pdf_documents_are_not_searched_for_links() -> None:
    json_document = _cached(
        fake_response("https://x.test/menu.json", b'{"href": "/a.pdf"}')
    )
    pdf_document = _cached(_sheet_response("https://x.test/sheet.pdf"))
    assert discover_pdf_urls([json_document, pdf_document]) == ()


# --- What lands, and what does not ------------------------------------------


def test_a_discovered_sheet_is_landed_and_read(linked: dict[str, object]) -> None:
    dataset, analyzer = _build(linked)
    assert dataset.discovered_urls == (site.NUTRITION_SHEET_URL,)
    assert dataset.rejected_urls == ()
    assert analyzer.analyses == [SHEET_DIGEST]
    (document,) = dataset.pdf_documents
    assert document.source_url == site.NUTRITION_SHEET_URL
    assert document.content_sha256 == SHEET_DIGEST
    assert document.model_id == DEFAULT_MODEL_ID
    assert document.api_version == DEFAULT_API_VERSION
    assert (document.page_count, document.table_count) == (1, 1)


def test_a_link_that_serves_html_is_rejected_before_azure_is_paid() -> None:
    """A stale link answered with an error page must never reach the analyser.

    Sending it would buy a structured extraction of the words "page not
    found" and file it as nutrition data.
    """
    dataset, analyzer = _build(
        {
            site.ALLERGENS_PAGE_URL: site.allergens_page_linking_to(
                site.NUTRITION_SHEET_URL
            ),
            site.NUTRITION_SHEET_URL: fake_response(
                site.NUTRITION_SHEET_URL,
                b"<html><body>Page not found</body></html>",
                content_type="application/pdf",
            ),
        }
    )
    assert dataset.discovered_urls == (site.NUTRITION_SHEET_URL,)
    assert dataset.rejected_urls == (site.NUTRITION_SHEET_URL,)
    assert dataset.pdf_documents == ()
    assert analyzer.analyses == []


def test_a_link_that_404s_is_unread_rather_than_stopping_the_harvest() -> None:
    """One unreachable sheet is not a reason to abandon the whole dataset."""
    dataset, _ = _build(
        {
            site.ALLERGENS_PAGE_URL: site.allergens_page_linking_to(
                site.NUTRITION_SHEET_URL
            ),
            site.NUTRITION_SHEET_URL: fake_response(
                site.NUTRITION_SHEET_URL, b"gone", status_code=404
            ),
        }
    )
    assert dataset.unread_urls == (site.NUTRITION_SHEET_URL,)
    assert dataset.rejected_urls == ()
    assert dataset.pdf_documents == ()


def test_a_link_that_serves_html_and_one_that_is_missing_are_told_apart() -> None:
    """ "Chipotle changed that link" and "we never fetched it" want different work."""
    dataset, _ = _build(
        {
            site.ALLERGENS_PAGE_URL: site.allergens_page_linking_to(
                site.NUTRITION_SHEET_URL
            ),
            site.NUTRITION_SHEET_URL: fake_response(
                site.NUTRITION_SHEET_URL, b"<html>nope</html>", content_type="text/html"
            ),
        }
    )
    assert dataset.rejected_urls == (site.NUTRITION_SHEET_URL,)
    assert dataset.unread_urls == ()


# --- The table, kept as a table ---------------------------------------------


def test_the_sheet_lands_cell_for_cell(linked: dict[str, object]) -> None:
    dataset, _ = _build(linked)
    (table,) = dataset.pdf_tables
    assert (table.row_count, table.column_count) == (8, 6)
    assert len(dataset.pdf_table_cells) == 48
    assert table.table_id == f"{SHEET_DIGEST}:0"
    assert table.column_headers == (
        "Item",
        "Serving",
        "Total Calories",
        "Total Fat (g)",
        "Saturated Fat (g)",
        "Sodium (mg)",
    )


def test_no_nutrition_row_is_split_across_a_boundary(
    linked: dict[str, object],
) -> None:
    """The property RFC-001 section 08 turns on, asserted on the stored rows.

    Every body row of the stored table is complete: seven items, six cells
    each, all carrying the same ``table_id`` and ``row_index``. A chunker that
    takes whole rows from here cannot separate a sodium figure from the item
    it belongs to, however long its window is.
    """
    dataset, _ = _build(linked)
    (table,) = dataset.pdf_tables
    by_row: dict[int, list[int]] = {}
    for cell in dataset.pdf_table_cells:
        assert cell.table_id == table.table_id
        by_row.setdefault(cell.row_index, []).append(cell.column_index)
    assert sorted(by_row) == list(range(8))
    for columns in by_row.values():
        assert sorted(columns) == list(range(6))


def test_the_heading_row_stays_marked_as_headings(linked: dict[str, object]) -> None:
    dataset, _ = _build(linked)
    kinds = {cell.row_index: cell.kind for cell in dataset.pdf_table_cells}
    assert kinds[0] == COLUMN_HEADER
    assert all(kinds[row] == CONTENT for row in range(1, 8))


def test_the_columns_are_matched_to_the_published_vocabulary(
    linked: dict[str, object],
) -> None:
    dataset, _ = _build(linked)
    (table,) = dataset.pdf_tables
    assert table.item_column == 0
    assert table.portion_column == 1
    assert table.nutrient_keys == (None, None, "tcal", "tfat", "satu", "sodi")


# --- The reconciliation -----------------------------------------------------


def _findings(dataset: PdfDataset, item_id: str) -> dict[str | None, Any]:
    """Return one item's findings keyed by nutrient."""
    return {
        row.nutrient_key: row
        for row in dataset.pdf_nutrition_findings
        if row.item_id == item_id
    }


def test_the_two_sources_are_compared_item_by_item(linked: dict[str, object]) -> None:
    dataset, _ = _build(linked)
    steak = _findings(dataset, STEAK_BURRITO)
    assert steak["tcal"].finding is Finding.AGREES
    assert steak["tcal"].pdf_value == Decimal("150")
    assert steak["tcal"].published_value == Decimal("150")
    assert steak["satu"].pdf_value == Decimal("2.5")
    assert steak["satu"].finding is Finding.AGREES


def test_a_disagreement_is_recorded_with_both_numbers_and_not_resolved(
    linked: dict[str, object],
) -> None:
    """The sheet says 260 mg of sodium in Cheese; the calculator says 190.

    Both numbers survive. Nothing here decides which is right, because
    deciding would turn a fact worth investigating into a number that looks
    as authoritative as any other.
    """
    dataset, _ = _build(linked)
    sodium = _findings(dataset, CHEESE)["sodi"]
    assert sodium.finding is Finding.DISAGREES
    assert sodium.pdf_value == Decimal("260")
    assert sodium.published_value == Decimal("190")
    assert sodium.pdf_unit == "mg"
    assert sodium.published_unit == "mg"
    assert sodium.item_label == "Cheese"


def test_one_disagreement_and_the_rest_agree(linked: dict[str, object]) -> None:
    """Twenty-eight comparisons over seven items, and exactly one mismatch."""
    dataset, _ = _build(linked)
    counts = dataset.coverage()
    assert counts["findings"] == 28
    assert counts["agrees"] == 27
    assert counts["disagrees"] == 1
    assert counts["unmatched_item"] == 0
    assert counts["unmatched_column"] == 0


def test_a_figure_read_at_the_wrong_serving_is_not_compared() -> None:
    """A number for a different portion is not a disagreement, it is unusable.

    Comparing eight grams of fat per ounce against eight grams per four
    ounces would produce either a false agreement or a false alarm; both are
    worse than saying the two are not comparable.
    """
    dataset = _parse_synthetic(_table(rows=[["Cheese", "4 oz", "110"]]))
    (row,) = dataset.pdf_nutrition_findings
    assert row.finding is Finding.PORTION_MISMATCH


def test_a_column_in_the_wrong_unit_is_worse_than_a_disagreement() -> None:
    """22 g and 22 mg compare as equal and mean nothing alike."""
    dataset = _parse_synthetic(
        _table(
            headers=["Item", "Serving", "Total Fat (mg)"],
            rows=[["Guacamole", "4 oz", "22"]],
        )
    )
    (row,) = dataset.pdf_nutrition_findings
    assert row.finding is Finding.UNIT_MISMATCH
    assert (row.pdf_unit, row.published_unit) == ("mg", "g")


def test_a_heading_that_matches_no_published_nutrient_is_reported() -> None:
    """Not dropped, and not attached to whichever nutrient looks closest."""
    dataset = _parse_synthetic(
        _table(
            headers=["Item", "Serving", "Total Calories", "Net Carbs"],
            rows=[["Guacamole", "4 oz", "230", "2"]],
        )
    )
    unmatched = [
        row
        for row in dataset.pdf_nutrition_findings
        if row.finding is Finding.UNMATCHED_COLUMN
    ]
    assert [row.column_header for row in unmatched] == ["Net Carbs"]


def test_a_row_naming_something_that_is_not_on_the_menu_is_reported() -> None:
    dataset = _parse_synthetic(
        _table(
            rows=[["Guacamole", "4 oz", "230"], ["Pineapple Salsa", "4 oz", "40"]],
        )
    )
    unmatched = [
        row
        for row in dataset.pdf_nutrition_findings
        if row.finding is Finding.UNMATCHED_ITEM
    ]
    (row,) = unmatched
    assert row.item_label == "Pineapple Salsa"
    assert row.item_id is None
    assert row.row_index == 2


def test_a_table_whose_rows_name_nothing_on_the_menu_is_not_a_nutrition_table() -> None:
    """One unknown label is a finding; a whole table of them is a different table.

    A catering price list has an item column too, and reporting every one of
    its rows as an unmatched nutrition row would bury the single unmatched row
    that actually means something.
    """
    dataset = _parse_synthetic(_table(rows=[["Pineapple Salsa", "4 oz", "40"]]))
    (table,) = dataset.pdf_tables
    assert table.item_column is None
    assert dataset.pdf_nutrition_findings == ()


def test_a_figure_the_calculator_does_not_publish_is_new_rather_than_wrong() -> None:
    """Jarritos Guava publishes no Vitamin C. A sheet that did would say so."""
    dataset = _parse_synthetic(
        _table(
            headers=["Item", "Serving", "Vitamin C"],
            rows=[["Jarritos Guava", "370 ml", "7"]],
        )
    )
    (row,) = dataset.pdf_nutrition_findings
    assert row.finding is Finding.NOT_PUBLISHED
    assert row.pdf_value == Decimal("7")
    assert row.published_value is None


@pytest.mark.parametrize("printed", ["<1", "trace", "170-250", ""])
def test_a_cell_that_is_not_a_figure_yields_no_figure(printed: str) -> None:
    """Reading the first digits out of ``<1`` would publish a one nobody wrote.

    Same for a published range: "170-250 cal" is what Chipotle prints for its
    lemonades, and narrowing it to 170 would be inventing a number.
    """
    dataset = _parse_synthetic(_table(rows=[["Guacamole", "4 oz", printed]]))
    assert dataset.pdf_nutrition_findings == ()


def test_a_blank_cell_produces_no_comparison_but_keeps_its_cell() -> None:
    dataset = _parse_synthetic(_table(rows=[["Guacamole", "4 oz", ""]]))
    assert dataset.pdf_nutrition_findings == ()
    assert len(dataset.pdf_table_cells) == 6


def test_a_table_that_is_not_a_nutrition_table_is_stored_and_not_reconciled() -> None:
    dataset = _parse_synthetic(
        _table(
            headers=["Store", "City"],
            rows=[["0679", "Lakewood"]],
        )
    )
    (table,) = dataset.pdf_tables
    assert table.item_column is None
    assert dataset.pdf_nutrition_findings == ()
    assert len(dataset.pdf_table_cells) == 4


# --- Caching and the offline path -------------------------------------------


def test_a_sheet_already_read_is_never_sent_to_azure_again(
    linked: dict[str, object],
) -> None:
    blobs = InMemoryBlobStore()
    transport = site.site(extra=linked)
    analyzer = FakeDocumentAnalyzer({SHEET_DIGEST: site.nutrition_sheet_layout()})
    analyses = AnalysisCache(blobs)

    for _ in range(2):
        with _harvester(transport, blobs) as harvester:
            documents = harvest_nutrition(harvester, [site.REFERENCE])
            found = harvest_pdfs(harvester, documents_of(documents.documents()))
            analyze_pdfs(found.pdfs, analyzer, analyses, clock=FakeClock())

    assert analyzer.analyses == [SHEET_DIGEST]


def test_the_offline_path_reads_the_cache_and_fetches_nothing(
    linked: dict[str, object],
) -> None:
    blobs = InMemoryBlobStore()
    transport = site.site(extra=linked)
    analyzer = FakeDocumentAnalyzer({SHEET_DIGEST: site.nutrition_sheet_layout()})
    analyses = AnalysisCache(blobs)

    with _harvester(transport, blobs) as harvester:
        warm = harvest_nutrition(harvester, [site.REFERENCE])
        found = harvest_pdfs(harvester, documents_of(warm.documents()))
        analyze_pdfs(found.pdfs, analyzer, analyses, clock=FakeClock())
    requests_made = len(transport.requests)

    cache = DocumentCache(blobs)
    offline = load_pdfs(cache, documents_of(warm.documents()))
    replayed = cached_analyses(
        offline.pdfs, analyses, DEFAULT_MODEL_ID, DEFAULT_API_VERSION
    )

    assert len(transport.requests) == requests_made
    assert [document.content_sha256 for document, _ in replayed] == [SHEET_DIGEST]
    assert analyzer.analyses == [SHEET_DIGEST]


def test_an_offline_run_over_a_sheet_that_was_never_read_says_so(
    linked: dict[str, object],
) -> None:
    blobs = InMemoryBlobStore()
    transport = site.site(extra=linked)
    with _harvester(transport, blobs) as harvester:
        warm = harvest_nutrition(harvester, [site.REFERENCE])
        found = harvest_pdfs(harvester, documents_of(warm.documents()))

    with pytest.raises(DocumentAnalysisError, match="no prebuilt-layout"):
        cached_analyses(
            found.pdfs, AnalysisCache(blobs), DEFAULT_MODEL_ID, DEFAULT_API_VERSION
        )


# --- What comes out ---------------------------------------------------------


def test_the_dataset_writes_four_tables_and_a_manifest(
    linked: dict[str, object],
) -> None:
    dataset, _ = _build(linked)
    blobs = InMemoryBlobStore()
    written = dataset.write(blobs)
    assert set(written) == {*PDF_TABLES, "manifest"}
    assert written["pdf_table_cells"] == "parsed/chipotle/pdf/pdf_table_cells.jsonl"


def test_two_runs_over_the_same_bytes_produce_the_same_digests(
    linked: dict[str, object],
) -> None:
    first, _ = _build(linked)
    second, _ = _build(linked)
    assert first.manifest()["tables"] == second.manifest()["tables"]


def test_the_manifest_says_what_it_looked_for_and_what_it_found(
    linked: dict[str, object],
) -> None:
    dataset, _ = _build(linked)
    manifest = dataset.manifest()
    assert manifest["discovered_urls"] == [site.NUTRITION_SHEET_URL]
    assert manifest["rejected_urls"] == []
    assert manifest["unread_urls"] == []
    assert manifest["coverage"]["cells"] == 48


def test_an_empty_dataset_is_a_result_rather_than_a_failure() -> None:
    dataset, _ = _build()
    blobs = InMemoryBlobStore()
    dataset.write(blobs)
    assert blobs.read("parsed/chipotle/pdf/pdf_documents.jsonl") == b""
    assert dataset.manifest()["coverage"]["discovered_urls"] == 0


def test_asking_for_a_table_that_does_not_exist_raises() -> None:
    dataset, _ = _build()
    with pytest.raises(KeyError, match="no such table"):
        dataset.table("nutrients")


# --- Helpers ----------------------------------------------------------------


def _cached(response: Any) -> Any:
    """Store one response in a throwaway cache and return the document."""
    return DocumentCache(InMemoryBlobStore()).put(
        response.url, response, FakeClock().now()
    )


def _table(
    *,
    headers: list[str] | None = None,
    rows: list[list[str]],
) -> dict[str, Any]:
    """Build one ``analyzeResult`` holding a single table.

    Used only where a property needs a sheet the fixture does not contain —
    a wrong unit, a serving that does not match, an unknown nutrient. The
    recorded analysis is what the shape of this mapping is copied from.
    """
    columns = headers or ["Item", "Serving", "Total Calories"]
    cells = [
        {
            "kind": "columnHeader",
            "rowIndex": 0,
            "columnIndex": column,
            "content": heading,
            "boundingRegions": [{"pageNumber": 1}],
        }
        for column, heading in enumerate(columns)
    ]
    for row_index, row in enumerate(rows, start=1):
        cells.extend(
            {
                "rowIndex": row_index,
                "columnIndex": column,
                "content": value,
                "boundingRegions": [{"pageNumber": 1}],
            }
            for column, value in enumerate(row)
        )
    return {
        "modelId": DEFAULT_MODEL_ID,
        "apiVersion": DEFAULT_API_VERSION,
        "pages": [{"pageNumber": 1}],
        "tables": [
            {
                "rowCount": len(rows) + 1,
                "columnCount": len(columns),
                "cells": cells,
                "boundingRegions": [{"pageNumber": 1}],
            }
        ],
    }


def _parse_synthetic(result: dict[str, Any]) -> PdfDataset:
    """Run one synthetic analysis through the reconciliation."""
    blobs = InMemoryBlobStore()
    transport = site.site(
        extra={
            site.ALLERGENS_PAGE_URL: site.allergens_page_linking_to(
                site.NUTRITION_SHEET_URL
            ),
            site.NUTRITION_SHEET_URL: _sheet_response(),
        }
    )
    analyzer = FakeDocumentAnalyzer({SHEET_DIGEST: result})
    with _harvester(transport, blobs) as harvester:
        menu = parse_menu(harvest_menu(harvester, [site.REFERENCE]))
        documents = harvest_nutrition(harvester, [site.REFERENCE])
        nutrition = parse_nutrition(documents)
        found = harvest_pdfs(harvester, documents_of(documents.documents()))
        analyzed = analyze_pdfs(
            found.pdfs, analyzer, AnalysisCache(blobs), clock=FakeClock()
        )
    return parse_pdfs(found, analyzed, menu, nutrition)
