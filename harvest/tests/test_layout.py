"""The extracted table keeps its shape.

Every assertion here runs against ``nutrition-sheet-layout.json``, which is
what the live Azure Document Intelligence account returned for the fixture
PDF's exact bytes. The point of testing against a recording rather than a
hand-written mapping is that the field names, the omitted ``kind``, the omitted
``rowSpan`` and the shape of ``boundingRegions`` are the service's, not this
repository's guess at the service's.
"""

import chipotle_fixtures as fixtures
import pytest

from chip_chat.harvest.layout import (
    COLUMN_HEADER,
    CONTENT,
    LayoutCell,
    LayoutDocument,
    LayoutTable,
    is_pdf,
    parse_layout,
)

SHEET = (
    (
        "Item",
        "Serving",
        "Total Calories",
        "Total Fat (g)",
        "Saturated Fat (g)",
        "Sodium (mg)",
    ),
    ("Guacamole", "4 oz", "230", "22", "3.5", "370"),
    ("Chips", "4 oz", "540", "25", "3.5", "390"),
    ("Chicken Bowl", "4 oz", "180", "7", "3", "310"),
    ("Steak Burrito", "4 oz", "150", "6", "2.5", "330"),
    ("White Rice", "4 oz", "210", "4", "0.5", "350"),
    ("Black Beans", "4 oz", "130", "1.5", "0", "210"),
    ("Cheese", "1 oz", "110", "8", "5", "260"),
)
"""The sheet as it is printed. The extraction is compared against this."""


@pytest.fixture
def layout() -> LayoutDocument:
    """The recorded analysis, parsed."""
    return parse_layout(fixtures.nutrition_sheet_layout())


def test_a_pdf_is_recognised_by_its_own_bytes() -> None:
    assert is_pdf(fixtures.nutrition_sheet())
    assert not is_pdf(b"<html><body>Page not found</body></html>")
    assert not is_pdf(b"")


def test_the_recorded_analysis_reports_the_model_that_produced_it(
    layout: LayoutDocument,
) -> None:
    assert layout.model_id == "prebuilt-layout"
    assert layout.api_version == "2024-11-30"
    assert layout.page_count == 1


def test_one_table_comes_out_of_the_one_page_sheet(layout: LayoutDocument) -> None:
    (table,) = layout.tables
    assert (table.row_count, table.column_count) == (8, 6)
    assert len(table.cells) == 48
    assert table.page_number == 1


def test_every_cell_lands_where_the_service_put_it(layout: LayoutDocument) -> None:
    (table,) = layout.tables
    assert table.grid() == SHEET


def test_the_heading_row_is_marked_as_headings(layout: LayoutDocument) -> None:
    (table,) = layout.tables
    assert table.column_headers() == SHEET[0]
    heading_rows = {cell.row_index for cell in table.cells if cell.kind == COLUMN_HEADER}
    assert heading_rows == {0}


def test_no_nutrition_row_is_split_or_short(layout: LayoutDocument) -> None:
    """The property issue #22 exists for, asserted rather than described.

    Every row of the extracted table is complete — one cell per column, none
    missing, none borrowed from the row above. A chunker handed whole rows
    from here cannot cut "Cheese | 1 oz | 110" apart from "| 8 | 5 | 260"
    however long its window is, because a row is never offered in halves.
    """
    (table,) = layout.tables
    for row_index, cells in table.rows():
        assert [cell.column_index for cell in cells] == list(range(table.column_count))
        assert [cell.content for cell in cells] == list(SHEET[row_index])
        assert all(cell.row_index == row_index for cell in cells)


def test_a_body_row_is_distinguishable_from_the_heading_row(
    layout: LayoutDocument,
) -> None:
    (table,) = layout.tables
    _, cells = next(iter(table.rows()))
    assert all(cell.kind == COLUMN_HEADER for cell in cells)
    body = [
        row_index
        for row_index, row in table.rows()
        if all(cell.kind == CONTENT for cell in row)
    ]
    assert body == [1, 2, 3, 4, 5, 6, 7]


def _merged_table() -> LayoutTable:
    """A table whose heading spans two columns and whose first column merges."""
    return LayoutTable(
        table_index=0,
        page_number=1,
        row_count=3,
        column_count=3,
        cells=(
            LayoutCell(0, 0, 1, 1, COLUMN_HEADER, "Item", 1),
            LayoutCell(0, 1, 1, 2, COLUMN_HEADER, "Total Fat", 1),
            LayoutCell(1, 0, 2, 1, CONTENT, "Guacamole", 1),
            LayoutCell(1, 1, 1, 1, CONTENT, "22", 1),
            LayoutCell(1, 2, 1, 1, CONTENT, "g", 1),
            LayoutCell(2, 1, 1, 1, CONTENT, "3.5", 1),
            LayoutCell(2, 2, 1, 1, CONTENT, "g", 1),
        ),
    )


def test_a_heading_spanning_two_columns_names_both_of_them() -> None:
    assert _merged_table().column_headers() == ("Item", "Total Fat", "Total Fat")


def test_a_cell_merged_down_repeats_in_every_row_it_covers() -> None:
    assert _merged_table().grid() == (
        ("Item", "Total Fat", "Total Fat"),
        ("Guacamole", "22", "g"),
        ("Guacamole", "3.5", "g"),
    )


def test_a_merged_cell_belongs_to_the_row_it_starts_in() -> None:
    table = _merged_table()
    assert [cell.content for cell in table.row(1)] == ["Guacamole", "22", "g"]
    assert [cell.content for cell in table.row(2)] == ["3.5", "g"]


def test_a_position_no_cell_covers_stays_none_rather_than_becoming_empty() -> None:
    table = LayoutTable(
        table_index=0,
        page_number=None,
        row_count=1,
        column_count=2,
        cells=(LayoutCell(0, 0, 1, 1, CONTENT, "Guacamole", None),),
    )
    assert table.grid() == (("Guacamole", None),)


def test_the_prose_around_the_table_survives_too(layout: LayoutDocument) -> None:
    assert layout.paragraphs[0] == "Nutrition Facts"
    assert any("Cross-contact" in paragraph for paragraph in layout.paragraphs)


def test_an_analysis_with_no_tables_parses_to_no_tables() -> None:
    document = parse_layout({"modelId": "prebuilt-layout", "apiVersion": "2024-11-30"})
    assert document.tables == ()
    assert document.paragraphs == ()
    assert document.page_count == 0
