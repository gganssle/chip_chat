"""The parsed shape of any nutrition data Chipotle publishes as a PDF.

Four flat tables, in the same style as the other three datasets, keyed on the
same ``item_id`` so that a figure read out of a PDF and a figure read out of
the nutrition calculator can be put beside each other rather than merged.

Two conventions carry over, and one is new.

**Structure survives.** ``pdf_table_cells`` holds every cell at the row and
column Document Intelligence reported, spans included. A nutrition row is
therefore recoverable as a row, with the headings that give its numbers
meaning, all the way from the PDF to the index. RFC-001 section 08 is explicit
that the alternative — a fixed window that cuts a table wherever the character
count runs out — is what produces confident wrong answers about allergens.

**Absence is a value.** As in the nutrition tables of issue #20, a figure
nobody published is ``None`` and a published zero is ``Decimal("0")``, and the
two never collapse into each other.

**And a disagreement is a finding, not a merge.** Where a PDF and the
calculator both publish a figure for the same item and nutrient and the figures
differ, ``pdf_nutrition_findings`` records both numbers and calls it
:attr:`Finding.DISAGREES`. Nothing here picks a winner. Silently preferring one
source would turn a fact worth investigating — the PDF is stale, or the
calculator changed, or the row was matched to the wrong item — into a number
that looks as authoritative as any other. See
``docs/decisions/pdf-tables.md``.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.sources.chipotle.tables import describe, write_tables

DEFAULT_PARSED_PREFIX = "parsed/chipotle/pdf"
"""Where the parsed tables land. Beside the other three datasets."""

TABLES = (
    "pdf_documents",
    "pdf_tables",
    "pdf_table_cells",
    "pdf_nutrition_findings",
)
"""Table names, in the order the manifest lists them."""


class Finding(StrEnum):
    """What comparing one PDF figure against the structured source produced.

    Attributes:
        AGREES: Both sources publish this figure and they are equal.
        DISAGREES: Both publish it and they differ. Recorded with both
            numbers; not resolved.
        UNIT_MISMATCH: Both publish a figure but in different units. Worse
            than a disagreement, because the numbers might well be equal — 22
            grams and 22 milligrams compare as equal and mean nothing alike.
        PORTION_MISMATCH: The PDF's serving size is not the portion the
            calculator's figure describes, so the two numbers are not
            comparable at all and are not compared.
        NOT_PUBLISHED: The PDF publishes a figure the calculator does not.
            Nothing is wrong; it is simply new information, and saying so is
            not the same as saying the two agree.
        UNMATCHED_ITEM: The PDF row's label matches no published menu item, so
            there is nothing to compare it against. The row is still stored in
            ``pdf_table_cells``; it is the *match* that failed, not the read.
        UNMATCHED_COLUMN: A column heading matches no published nutrient name.
            Recorded rather than dropped, because a column quietly ignored is
            how a nutrient stops being harvested without anybody noticing.
    """

    AGREES = "AGREES"
    DISAGREES = "DISAGREES"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    PORTION_MISMATCH = "PORTION_MISMATCH"
    NOT_PUBLISHED = "NOT_PUBLISHED"
    UNMATCHED_ITEM = "UNMATCHED_ITEM"
    UNMATCHED_COLUMN = "UNMATCHED_COLUMN"


@dataclass(frozen=True, slots=True)
class PdfDocument:
    """One harvested PDF, and the analysis that read it.

    Attributes:
        content_sha256: Digest of the PDF's bytes. The identity of the
            document everywhere in this dataset, because a URL can serve
            different bytes next week and the digest cannot.
        source_url: Where the bytes came from, after redirects. The citation
            field.
        harvested_at: When they were fetched.
        served_content_type: The ``Content-Type`` the server sent, verbatim.
            Kept because it is occasionally wrong, and a site that starts
            serving its nutrition sheet as ``text/html`` is worth seeing.
        page_count: Pages Document Intelligence reported.
        table_count: Tables it found.
        model_id: The model that read the document.
        api_version: The API version it answered on. The same bytes read by a
            later model may extract different tables; a dataset that cannot say
            which version read it cannot explain the difference.
        analyzed_at: When the analysis completed.
    """

    content_sha256: str
    source_url: str
    harvested_at: datetime
    served_content_type: str
    page_count: int
    table_count: int
    model_id: str
    api_version: str
    analyzed_at: datetime


@dataclass(frozen=True, slots=True)
class PdfTable:
    """One table extracted from a PDF.

    Attributes:
        table_id: ``<digest>:<index>``. Deterministic across runs over the
            same bytes, which is what lets two manifests be compared.
        content_sha256: The document the table came from.
        table_index: Its position in that document's table list.
        page_number: The page it starts on, where the service reported one.
        row_count: Rows, as reported.
        column_count: Columns, as reported.
        column_headers: One heading per column, or ``None`` where the service
            marked none. A heading merged across columns names each of them.
        nutrient_keys: The nutrient each column was matched to, or ``None``
            where it matched none. Aligned with ``column_headers``.
        item_column: Which column carries the item name, or ``None`` where no
            column could be identified as one — in which case no row of this
            table is reconciled against anything.
        portion_column: Which column carries the serving size, or ``None``.
        source_url: The document's URL.
        harvested_at: When it was fetched.
    """

    table_id: str
    content_sha256: str
    table_index: int
    page_number: int | None
    row_count: int
    column_count: int
    column_headers: tuple[str | None, ...]
    nutrient_keys: tuple[str | None, ...]
    item_column: int | None
    portion_column: int | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class PdfTableCell:
    """One cell, at the position Document Intelligence put it.

    This table is the structure-preserving one, and it is the reason this
    dataset exists in this shape rather than as a column of extracted text.
    Every cell keeps its row, its column and its spans, so a consumer can ask
    for row four of table two and receive row four of table two — headings
    included — instead of whatever text happened to fall inside a window.

    Attributes:
        table_id: The table this cell belongs to.
        row_index: Zero-based row.
        column_index: Zero-based column.
        row_span: How many rows the cell covers. One unless merged.
        column_span: How many columns it covers.
        kind: ``columnHeader``, ``rowHeader`` or ``content``.
        content: The cell's text, verbatim. Not normalised, not stripped of
            its units, not parsed. The interpretation lives in
            ``pdf_nutrition_findings``; this is the reading.
        page_number: The page it was found on, where one was reported.
    """

    table_id: str
    row_index: int
    column_index: int
    row_span: int
    column_span: int
    kind: str
    content: str
    page_number: int | None


@dataclass(frozen=True, slots=True)
class PdfNutritionFinding:
    """One comparison between a PDF figure and the published structured one.

    Attributes:
        table_id: The table the figure was read from.
        row_index: The row it was on. ``0`` for a finding about a heading.
        column_index: The column it was in.
        item_label: The row's item name, exactly as the PDF prints it.
        item_id: The menu item it was matched to, or ``None`` where the label
            matched nothing.
        column_header: The column's heading, exactly as the PDF prints it.
        nutrient_key: The published nutrient the heading was matched to, or
            ``None``.
        pdf_value: The figure the PDF publishes, or ``None`` where the cell
            held no number.
        pdf_unit: The unit the PDF's heading gives, e.g. ``g`` from
            ``Total Fat (g)``, or ``None`` where the heading names none.
        published_value: The figure the nutrition dataset publishes for this
            item and nutrient, or ``None`` where it publishes none.
        published_unit: The unit that figure is in.
        finding: What the comparison produced.
        source_url: The PDF's URL.
        harvested_at: When it was fetched.
    """

    table_id: str
    row_index: int
    column_index: int
    item_label: str | None
    item_id: str | None
    column_header: str | None
    nutrient_key: str | None
    pdf_value: Decimal | None
    pdf_unit: str | None
    published_value: Decimal | None
    published_unit: str | None
    finding: Finding
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class PdfDataset:
    """Everything issue #22 produces, ready to write.

    Attributes:
        pdf_documents: One row per harvested PDF.
        pdf_tables: One row per extracted table.
        pdf_table_cells: One row per cell.
        pdf_nutrition_findings: One row per comparison.
        discovered_urls: Every URL that looked like a PDF and was fetched,
            whether or not the bytes turned out to be one.
        rejected_urls: Those of them that were read and were not PDFs after
            all.
        unread_urls: Those that could not be read at all — a 404, a path
            ``robots.txt`` forbids, or one this landing zone predates.

    An empty dataset that discovered nothing and an empty dataset that
    discovered three links which all served HTML are different outcomes, and
    only the second is a reason to go and look.
    """

    pdf_documents: tuple[PdfDocument, ...]
    pdf_tables: tuple[PdfTable, ...]
    pdf_table_cells: tuple[PdfTableCell, ...]
    pdf_nutrition_findings: tuple[PdfNutritionFinding, ...]
    discovered_urls: tuple[str, ...] = ()
    rejected_urls: tuple[str, ...] = ()
    unread_urls: tuple[str, ...] = ()

    def table(self, name: str) -> Sequence[Any]:
        """Return one table by name.

        Args:
            name: One of :data:`TABLES`.

        Returns:
            The rows.

        Raises:
            KeyError: If ``name`` is not a table.
        """
        if name not in TABLES:
            raise KeyError(f"no such table {name!r}; expected one of {TABLES}")
        rows: Sequence[Any] = getattr(self, name)
        return rows

    def tables(self) -> Iterator[tuple[str, Sequence[Any]]]:
        """Yield every table as a ``(name, rows)`` pair, in :data:`TABLES` order."""
        for name in TABLES:
            yield name, self.table(name)

    def coverage(self) -> dict[str, int]:
        """Return what was found and what the comparison made of it.

        A PDF path that quietly stopped finding PDFs looks exactly like one
        that ran against a site publishing none, and the two want different
        responses. Counting both in the manifest is what makes the day a
        nutrition sheet appears — or disappears — show up in a diff.

        Returns:
            Counts of discovered URLs, PDFs, tables, cells, and findings by
            kind.
        """
        findings = [row.finding for row in self.pdf_nutrition_findings]
        counts = {
            "discovered_urls": len(self.discovered_urls),
            "rejected_urls": len(self.rejected_urls),
            "unread_urls": len(self.unread_urls),
            "pdfs": len(self.pdf_documents),
            "tables": len(self.pdf_tables),
            "cells": len(self.pdf_table_cells),
            "findings": len(findings),
        }
        counts.update(
            {finding.value.lower(): findings.count(finding) for finding in Finding}
        )
        return counts

    def manifest(self) -> dict[str, Any]:
        """Return a description of this dataset, digests and coverage included.

        Returns:
            A JSON-ready mapping.
        """
        return {
            "coverage": self.coverage(),
            "discovered_urls": list(self.discovered_urls),
            "rejected_urls": list(self.rejected_urls),
            "unread_urls": list(self.unread_urls),
            "tables": describe(self.tables()),
        }

    def write(
        self, blobs: BlobStore, prefix: str = DEFAULT_PARSED_PREFIX
    ) -> dict[str, str]:
        """Write every table, and the manifest, to the blob store.

        Args:
            blobs: Where to write. The same store the raw bytes landed in,
                under a different prefix.
            prefix: Key prefix for the parsed tables.

        Returns:
            Table name to the key it was written at, with the manifest under
            the key ``manifest``.
        """
        return write_tables(blobs, prefix, self.tables(), self.manifest())
