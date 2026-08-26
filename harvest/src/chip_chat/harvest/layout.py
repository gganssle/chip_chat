"""What Document Intelligence returns, kept as a table rather than as prose.

Issue #22 exists because of one sentence in RFC-001 section 08: *fixed-window
chunking splits nutrition tables across boundaries and produces exactly the
confident wrong answers that allergen questions cannot tolerate.* Flattening a
nutrition table to text is the same mistake made earlier — by the time a row
has become a line of prose, the column a number sat under is gone, and nothing
downstream can put it back.

So the analyser's answer is read into :class:`LayoutTable`, which keeps every
cell's row, column and span exactly as the service reported them, and offers
:meth:`LayoutTable.rows` as the smallest unit anything downstream is allowed to
take. A row is either present whole, with the headers that give its numbers
meaning, or it is not there.

Nothing in this module is Chipotle-specific, and nothing in it decides what a
column *means*. That judgement needs a published vocabulary to check against,
so it lives beside the source that has one.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PDF_MAGIC = b"%PDF-"
"""The five bytes every PDF starts with."""

PDF_CONTENT_TYPE = "application/pdf"

COLUMN_HEADER = "columnHeader"
"""The ``kind`` Document Intelligence gives a cell it read as a column heading."""

ROW_HEADER = "rowHeader"

CONTENT = "content"
"""The ``kind`` of an ordinary cell. The service omits ``kind`` for these."""


def is_pdf(content: bytes) -> bool:
    """Return whether ``content`` is a PDF, judged by its own first bytes.

    The ``Content-Type`` header is deliberately not consulted. A server that
    serves an HTML error page under ``application/pdf`` would otherwise send an
    error page to the analyser and get a structured extraction of the words
    "page not found"; a server that serves a PDF as ``application/octet-stream``
    would otherwise be skipped. The bytes are the only honest witness.

    Args:
        content: The document body.

    Returns:
        ``True`` if the body begins with ``%PDF-``.
    """
    return content.startswith(PDF_MAGIC)


@dataclass(frozen=True, slots=True)
class LayoutCell:
    """One cell of an extracted table, at the position the service put it.

    Attributes:
        row_index: Zero-based row, as reported.
        column_index: Zero-based column, as reported.
        row_span: How many rows the cell covers. One unless it is merged.
        column_span: How many columns it covers.
        kind: ``columnHeader``, ``rowHeader`` or ``content``. The service omits
            the field for ordinary cells; it is filled in here so that the
            column headings a row needs are addressable rather than assumed to
            be row zero.
        content: The cell's text, verbatim.
        page_number: The page it was found on, or ``None`` if the service
            reported no bounding region for it.
    """

    row_index: int
    column_index: int
    row_span: int
    column_span: int
    kind: str
    content: str
    page_number: int | None

    @property
    def is_header(self) -> bool:
        """Return whether this cell is a column or row heading."""
        return self.kind in (COLUMN_HEADER, ROW_HEADER)


@dataclass(frozen=True, slots=True)
class LayoutTable:
    """One extracted table, with its structure intact.

    Attributes:
        table_index: Position in the document's table list, which is what
            makes a table identifiable across two runs over the same bytes.
        page_number: The page the table starts on, or ``None``.
        row_count: Rows, as reported.
        column_count: Columns, as reported.
        cells: Every cell, ordered by row then column.
        caption: The service's caption for the table, if it found one.
    """

    table_index: int
    page_number: int | None
    row_count: int
    column_count: int
    cells: tuple[LayoutCell, ...]
    caption: str | None = None

    def row(self, row_index: int) -> tuple[LayoutCell, ...]:
        """Return every cell whose own row is ``row_index``, left to right.

        A cell merged across rows is returned only for the row it starts in.
        Use :meth:`grid` where a merged cell should repeat.

        Args:
            row_index: The row wanted.

        Returns:
            The cells, ordered by column.
        """
        return tuple(
            cell
            for cell in sorted(self.cells, key=lambda c: c.column_index)
            if cell.row_index == row_index
        )

    def rows(self) -> Iterator[tuple[int, tuple[LayoutCell, ...]]]:
        """Yield ``(row_index, cells)`` for every row, in order.

        This is the unit of the table. Anything that consumes a table consumes
        whole rows through here, so that a row cannot be halved by a window
        that happened to end in the middle of it.
        """
        for row_index in range(self.row_count):
            yield row_index, self.row(row_index)

    def column_headers(self) -> tuple[str | None, ...]:
        """Return one heading per column, or ``None`` where there is none.

        A heading merged across columns names every column it covers, because
        a figure under the right-hand half of a merged "Total Fat" heading is
        still a total fat figure.

        Returns:
            ``column_count`` entries, in column order.
        """
        headings: list[str | None] = [None] * self.column_count
        for cell in self.cells:
            if cell.kind != COLUMN_HEADER:
                continue
            for offset in range(cell.column_span):
                column = cell.column_index + offset
                if 0 <= column < self.column_count and headings[column] is None:
                    headings[column] = cell.content
        return tuple(headings)

    def grid(self) -> tuple[tuple[str | None, ...], ...]:
        """Return the table as rows of text, with merged cells repeated.

        Args:
            None.

        Returns:
            ``row_count`` tuples of ``column_count`` entries. A position no
            cell covers is ``None`` — which is a hole the service reported,
            not an empty string this module invented.
        """
        filled: list[list[str | None]] = [
            [None] * self.column_count for _ in range(self.row_count)
        ]
        for cell in self.cells:
            for row_offset in range(cell.row_span):
                for column_offset in range(cell.column_span):
                    row = cell.row_index + row_offset
                    column = cell.column_index + column_offset
                    if 0 <= row < self.row_count and 0 <= column < self.column_count:
                        filled[row][column] = cell.content
        return tuple(tuple(row) for row in filled)


@dataclass(frozen=True, slots=True)
class LayoutDocument:
    """One analysed document, reduced to what this package stores.

    Attributes:
        model_id: The Document Intelligence model that produced it.
        api_version: The API version it answered on. Recorded because the
            same bytes analysed a year later may not extract the same tables,
            and a dataset that cannot say which version read it cannot explain
            the difference.
        page_count: Pages the service reported.
        tables: Every table, in the order the service listed them.
        paragraphs: Every paragraph's text, in reading order. The prose around
            a table — the footnote that says the chart does not reflect
            cross-contact — matters as much as the table.
    """

    model_id: str
    api_version: str
    page_count: int
    tables: tuple[LayoutTable, ...]
    paragraphs: tuple[str, ...]


def _page_of(regions: Sequence[Any] | None) -> int | None:
    """Return the page number of the first bounding region, if there is one."""
    if not regions:
        return None
    first = regions[0]
    if not isinstance(first, Mapping):
        return None
    page = first.get("pageNumber")
    return int(page) if isinstance(page, int) else None


def _cell(raw: Mapping[str, Any]) -> LayoutCell:
    """Read one cell, defaulting the fields the service omits when they are one."""
    return LayoutCell(
        row_index=int(raw["rowIndex"]),
        column_index=int(raw["columnIndex"]),
        row_span=int(raw.get("rowSpan") or 1),
        column_span=int(raw.get("columnSpan") or 1),
        kind=str(raw.get("kind") or CONTENT),
        content=str(raw.get("content") or ""),
        page_number=_page_of(raw.get("boundingRegions")),
    )


def _table(index: int, raw: Mapping[str, Any]) -> LayoutTable:
    """Read one table, with its cells ordered by row and then column."""
    cells = tuple(
        sorted(
            (_cell(cell) for cell in raw.get("cells", ())),
            key=lambda cell: (cell.row_index, cell.column_index),
        )
    )
    caption = raw.get("caption")
    caption_text = (
        str(caption.get("content")) if isinstance(caption, Mapping) and caption else None
    )
    return LayoutTable(
        table_index=index,
        page_number=_page_of(raw.get("boundingRegions")),
        row_count=int(raw.get("rowCount") or 0),
        column_count=int(raw.get("columnCount") or 0),
        cells=cells,
        caption=caption_text,
    )


def parse_layout(result: Mapping[str, Any]) -> LayoutDocument:
    """Read one ``analyzeResult`` into the shape this package stores.

    Args:
        result: The ``analyzeResult`` object from a Document Intelligence
            layout analysis, verbatim.

    Returns:
        The document.

    Raises:
        KeyError: If a table cell omits its row or column index, which would
            leave a number with no column to belong to. That is not a case to
            paper over with a default.
    """
    tables = tuple(
        _table(index, raw) for index, raw in enumerate(result.get("tables", ()))
    )
    paragraphs = tuple(
        str(paragraph.get("content") or "") for paragraph in result.get("paragraphs", ())
    )
    return LayoutDocument(
        model_id=str(result.get("modelId") or ""),
        api_version=str(result.get("apiVersion") or ""),
        page_count=len(result.get("pages", ())),
        tables=tables,
        paragraphs=paragraphs,
    )
