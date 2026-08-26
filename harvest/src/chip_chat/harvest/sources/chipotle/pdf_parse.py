"""Reading an analysed PDF as a table, and checking it against the calculator.

Two jobs, and the order matters.

**First, the table is stored as a table.** Every cell keeps the row, column and
span Document Intelligence gave it. Nothing here flattens a row to prose, and
nothing downstream needs to: the row is addressable, and so are the headings
that make its numbers mean something.

**Then it is compared, not merged.** Chipotle's nutrition calculator already
publishes figures for most of what a nutrition sheet would carry, and issue #22
asks for the two to be reconciled — with the explicit instruction that *a
mismatch is a finding, not a merge conflict to resolve silently*. So every
comparison lands as a row in ``pdf_nutrition_findings`` saying what each source
said, and no row anywhere is overwritten by the other source's number.

**Nothing about which column is which is spelled out here.** The item column is
found by asking which column's cells are published item names. The serving
column is found by asking which column's cells parse as a published portion
unit. A nutrient column is one whose heading *exactly* matches a published
nutrient name once the parenthesised unit is taken off it. All three
vocabularies come out of the datasets issues #19 and #20 already harvested, so
this parser learns a new nutrient the day Chipotle publishes one, and — much
more importantly — never guesses. A heading it cannot match is recorded as
:attr:`~chip_chat.harvest.sources.chipotle.pdf_records.Finding.UNMATCHED_COLUMN`
rather than fuzzily attached to the nearest nutrient, because a column
mis-attached to the wrong nutrient is precisely the confident wrong answer that
RFC-001 section 08 says allergen questions cannot tolerate.

One consequence is worth stating plainly: **a finding is a comparison, and a
cell holding no number is not one.** Where a PDF leaves a figure blank there is
nothing to compare, so no finding is emitted; the empty cell is still in
``pdf_table_cells``, where what the sheet does and does not print is visible.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from chip_chat.harvest.analysis import DocumentAnalysis
from chip_chat.harvest.cache import CachedDocument
from chip_chat.harvest.layout import CONTENT, LayoutTable, parse_layout
from chip_chat.harvest.sources.chipotle.nutrition_records import NutritionDataset
from chip_chat.harvest.sources.chipotle.pdf import PdfDocuments
from chip_chat.harvest.sources.chipotle.pdf_records import (
    Finding,
    PdfDataset,
    PdfDocument,
    PdfNutritionFinding,
    PdfTable,
    PdfTableCell,
)
from chip_chat.harvest.sources.chipotle.records import MenuDataset

_UNIT_SUFFIX = re.compile(r"\s*\((?P<unit>[^()]{1,12})\)\s*$")
"""``Total Fat (g)`` → name ``Total Fat``, unit ``g``. Bounded so that a
parenthetical sentence at the end of a heading is not mistaken for a unit."""

_NUMBER = re.compile(r"(?P<value>-?\d+(?:\.\d+)?)\s*[A-Za-z%]*")
"""A cell that is a figure, whole. Matched against the entire cell rather than
searched for inside it, so that ``<1``, ``trace`` and a published range like
``170-250`` yield no number instead of yielding their first digits."""

_PORTION = re.compile(r"^\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]{1,6})\s*$")


def _normalise(text: str) -> str:
    """Return ``text`` with its whitespace collapsed and its case folded."""
    return " ".join(text.split()).casefold()


def _split_unit(heading: str) -> tuple[str, str | None]:
    """Return a heading's name and the unit it names in brackets, if any."""
    match = _UNIT_SUFFIX.search(heading)
    if match is None:
        return heading.strip(), None
    return heading[: match.start()].strip(), match.group("unit").strip()


def _number(text: str) -> Decimal | None:
    """Return the figure a cell holds, or ``None`` if it does not hold one.

    Thousands separators are removed and a trailing unit is allowed; nothing
    else is. A cell reading ``<1``, ``trace`` or ``170-250`` yields ``None``,
    because the alternative — reading the first digits out of it — is how a
    published range becomes a published figure and a "less than one" becomes a
    one.
    """
    match = _NUMBER.fullmatch(text.replace(",", "").strip())
    if match is None:
        return None
    try:
        return Decimal(match.group("value"))
    except InvalidOperation:  # pragma: no cover - the pattern cannot produce one
        return None


def _portion(text: str) -> tuple[Decimal, str] | None:
    """Return the ``(value, unit)`` a serving cell holds, or ``None``."""
    match = _PORTION.match(text)
    if match is None:
        return None
    try:
        return Decimal(match.group("value")), match.group("unit")
    except InvalidOperation:  # pragma: no cover - the pattern cannot produce one
        return None


@dataclass(frozen=True, slots=True)
class _Vocabulary:
    """What the already-harvested datasets know, indexed for lookup.

    Attributes:
        items: Normalised item name to item id. A name two items share is left
            out entirely rather than resolved to whichever came first — an
            ambiguous label is an unmatched one, and saying so is honest where
            picking is not.
        nutrients: Normalised nutrient name to ``(nutrient_key, unit)``.
        figures: ``(item_id, nutrient_key)`` to ``(value, unit)``.
        portions: Item id to the ``(value, unit)`` its published figures are
            for.
    """

    items: Mapping[str, str]
    nutrients: Mapping[str, tuple[str, str | None]]
    figures: Mapping[tuple[str, str], tuple[Decimal | None, str | None]]
    portions: Mapping[str, tuple[Decimal, str]]


def _vocabulary(menu: MenuDataset, nutrition: NutritionDataset) -> _Vocabulary:
    """Index the published menu and nutrition data for matching against."""
    names: dict[str, set[str]] = {}
    for item in menu.menu_items:
        names.setdefault(_normalise(item.name), set()).add(item.item_id)
    items = {name: next(iter(ids)) for name, ids in names.items() if len(ids) == 1}

    nutrients = {
        _normalise(nutrient.name): (nutrient.nutrient_key, nutrient.unit)
        for nutrient in nutrition.nutrients
    }

    figures: dict[tuple[str, str], tuple[Decimal | None, str | None]] = {}
    portions: dict[str, tuple[Decimal, str]] = {}
    for row in nutrition.item_nutrition:
        figures[(row.item_id, row.nutrient_key)] = (row.value, row.unit)
        if row.portion_unit is not None and row.portion_value is not None:
            portions.setdefault(row.item_id, (row.portion_value, row.portion_unit))
    return _Vocabulary(
        items=items, nutrients=nutrients, figures=figures, portions=portions
    )


def _body_rows(table: LayoutTable) -> tuple[int, ...]:
    """Return the indices of the rows that are not entirely headings."""
    return tuple(
        row_index
        for row_index, cells in table.rows()
        if any(cell.kind == CONTENT for cell in cells)
    )


def _item_column(
    table: LayoutTable, grid: Sequence[Sequence[str | None]], vocabulary: _Vocabulary
) -> int | None:
    """Return the column whose cells are published item names, if one is.

    Asked of the data rather than of the heading, because "Item", "Menu Item",
    "Product" and an unlabelled first column are all the same column, and a
    parser that recognised only the spellings it was taught would silently stop
    reconciling the day a sheet used a fourth.

    A table in which *no* row names anything on the menu has no item column,
    and so is not reconciled at all. That is the difference between a catering
    price list, whose every row is unknown because it is not a nutrition
    table, and a nutrition table with one unknown row in it — which is a
    finding worth reading, and would be buried if every row of every other
    table were one too.
    """
    body = _body_rows(table)
    best: tuple[int, int] | None = None
    for column in range(table.column_count):
        matches = sum(
            1
            for row in body
            if (text := grid[row][column]) and _normalise(text) in vocabulary.items
        )
        if matches and (best is None or matches > best[0]):
            best = (matches, column)
    return None if best is None else best[1]


def _portion_column(
    table: LayoutTable,
    grid: Sequence[Sequence[str | None]],
    vocabulary: _Vocabulary,
    item_column: int | None,
) -> int | None:
    """Return the column whose cells are servings in a published portion unit."""
    units = {unit.casefold() for _value, unit in vocabulary.portions.values()}
    body = _body_rows(table)
    best: tuple[int, int] | None = None
    for column in range(table.column_count):
        if column == item_column:
            continue
        matches = 0
        for row in body:
            text = grid[row][column]
            parsed = _portion(text) if text else None
            if parsed is not None and parsed[1].casefold() in units:
                matches += 1
        if matches and (best is None or matches > best[0]):
            best = (matches, column)
    return None if best is None else best[1]


def _nutrient_columns(
    table: LayoutTable, vocabulary: _Vocabulary
) -> tuple[tuple[str | None, ...], tuple[str | None, ...], tuple[str | None, ...]]:
    """Return each column's heading, matched nutrient key, and stated unit."""
    headings = table.column_headers()
    keys: list[str | None] = []
    units: list[str | None] = []
    for heading in headings:
        if heading is None:
            keys.append(None)
            units.append(None)
            continue
        name, unit = _split_unit(heading)
        matched = vocabulary.nutrients.get(_normalise(name))
        keys.append(None if matched is None else matched[0])
        units.append(unit)
    return headings, tuple(keys), tuple(units)


def _reconcile_table(
    table: LayoutTable,
    table_id: str,
    document: CachedDocument,
    vocabulary: _Vocabulary,
) -> tuple[PdfTable, tuple[PdfNutritionFinding, ...]]:
    """Describe one extracted table and compare its figures with the published ones."""
    grid = table.grid()
    headings, keys, units = _nutrient_columns(table, vocabulary)
    item_column = _item_column(table, grid, vocabulary)
    portion_column = _portion_column(table, grid, vocabulary, item_column)

    described = PdfTable(
        table_id=table_id,
        content_sha256=document.content_sha256,
        table_index=table.table_index,
        page_number=table.page_number,
        row_count=table.row_count,
        column_count=table.column_count,
        column_headers=headings,
        nutrient_keys=keys,
        item_column=item_column,
        portion_column=portion_column,
        source_url=document.source_url,
        harvested_at=document.harvested_at,
    )
    if item_column is None or not any(key is not None for key in keys):
        # Not a nutrition table. It is still stored, cell for cell; there is
        # simply nothing in it to check the calculator against.
        return described, ()

    def finding(
        *,
        row_index: int,
        column_index: int,
        item_label: str | None,
        item_id: str | None,
        column_header: str | None,
        nutrient_key: str | None,
        pdf_value: Decimal | None,
        pdf_unit: str | None,
        published_value: Decimal | None,
        published_unit: str | None,
        verdict: Finding,
    ) -> PdfNutritionFinding:
        """Build one finding, filling in the fields every finding shares."""
        return PdfNutritionFinding(
            table_id=table_id,
            row_index=row_index,
            column_index=column_index,
            item_label=item_label,
            item_id=item_id,
            column_header=column_header,
            nutrient_key=nutrient_key,
            pdf_value=pdf_value,
            pdf_unit=pdf_unit,
            published_value=published_value,
            published_unit=published_unit,
            finding=verdict,
            source_url=document.source_url,
            harvested_at=document.harvested_at,
        )

    findings: list[PdfNutritionFinding] = [
        finding(
            row_index=0,
            column_index=column,
            item_label=None,
            item_id=None,
            column_header=headings[column],
            nutrient_key=None,
            pdf_value=None,
            pdf_unit=units[column],
            published_value=None,
            published_unit=None,
            verdict=Finding.UNMATCHED_COLUMN,
        )
        for column in range(table.column_count)
        if keys[column] is None and column not in (item_column, portion_column)
    ]

    for row in _body_rows(table):
        label = (grid[row][item_column] or "").strip()
        item_id = vocabulary.items.get(_normalise(label))
        if item_id is None:
            findings.append(
                finding(
                    row_index=row,
                    column_index=item_column,
                    item_label=label or None,
                    item_id=None,
                    column_header=headings[item_column],
                    nutrient_key=None,
                    pdf_value=None,
                    pdf_unit=None,
                    published_value=None,
                    published_unit=None,
                    verdict=Finding.UNMATCHED_ITEM,
                )
            )
            continue

        comparable = True
        if portion_column is not None:
            stated = _portion(grid[row][portion_column] or "")
            published_portion = vocabulary.portions.get(item_id)
            comparable = (
                stated is None
                or published_portion is None
                or (
                    stated[0] == published_portion[0]
                    and stated[1].casefold() == published_portion[1].casefold()
                )
            )

        for column in range(table.column_count):
            key = keys[column]
            if key is None:
                continue
            pdf_value = _number(grid[row][column] or "")
            if pdf_value is None:
                continue
            published_value, published_unit = vocabulary.figures.get(
                (item_id, key), (None, None)
            )
            pdf_unit = units[column]
            if not comparable:
                verdict = Finding.PORTION_MISMATCH
            elif published_value is None:
                verdict = Finding.NOT_PUBLISHED
            elif (
                pdf_unit is not None
                and published_unit is not None
                and pdf_unit.casefold() != published_unit.casefold()
            ):
                verdict = Finding.UNIT_MISMATCH
            elif pdf_value == published_value:
                verdict = Finding.AGREES
            else:
                verdict = Finding.DISAGREES
            findings.append(
                finding(
                    row_index=row,
                    column_index=column,
                    item_label=label,
                    item_id=item_id,
                    column_header=headings[column],
                    nutrient_key=key,
                    pdf_value=pdf_value,
                    pdf_unit=pdf_unit,
                    published_value=published_value,
                    published_unit=published_unit,
                    verdict=verdict,
                )
            )
    return described, tuple(findings)


def table_id_for(content_sha256: str, table_index: int) -> str:
    """Return the identifier one table keeps across every run over the same bytes.

    Args:
        content_sha256: Digest of the PDF the table came from.
        table_index: Its position in that document's table list.

    Returns:
        ``<digest>:<index>``.
    """
    return f"{content_sha256}:{table_index}"


def parse_pdfs(
    documents: PdfDocuments,
    analyzed: Sequence[tuple[CachedDocument, DocumentAnalysis]],
    menu: MenuDataset,
    nutrition: NutritionDataset,
) -> PdfDataset:
    """Turn analysed PDFs into tables, and check them against the calculator.

    Args:
        documents: What the discovery step found, so that the manifest can
            report links that were not documents as well as those that were.
        analyzed: Each PDF with its Document Intelligence analysis.
        menu: The parsed menu, for the published item names.
        nutrition: The parsed nutrition data, for the published nutrient
            vocabulary and the figures to compare against.

    Returns:
        The dataset. An empty one — no PDFs discovered — is a valid result and
        not an error: it is what a site that publishes none produces, and the
        manifest says so in as many words.
    """
    vocabulary = _vocabulary(menu, nutrition)
    pdf_documents: list[PdfDocument] = []
    pdf_tables: list[PdfTable] = []
    pdf_cells: list[PdfTableCell] = []
    findings: list[PdfNutritionFinding] = []

    for document, analysis in analyzed:
        layout = parse_layout(analysis.result)
        pdf_documents.append(
            PdfDocument(
                content_sha256=document.content_sha256,
                source_url=document.source_url,
                harvested_at=document.harvested_at,
                served_content_type=document.content_type,
                page_count=layout.page_count,
                table_count=len(layout.tables),
                model_id=analysis.model_id,
                api_version=analysis.api_version,
                analyzed_at=analysis.analyzed_at,
            )
        )
        for table in layout.tables:
            table_id = table_id_for(document.content_sha256, table.table_index)
            described, table_findings = _reconcile_table(
                table, table_id, document, vocabulary
            )
            pdf_tables.append(described)
            findings.extend(table_findings)
            pdf_cells.extend(
                PdfTableCell(
                    table_id=table_id,
                    row_index=cell.row_index,
                    column_index=cell.column_index,
                    row_span=cell.row_span,
                    column_span=cell.column_span,
                    kind=cell.kind,
                    content=cell.content,
                    page_number=cell.page_number,
                )
                for cell in table.cells
            )

    return PdfDataset(
        pdf_documents=tuple(pdf_documents),
        pdf_tables=tuple(pdf_tables),
        pdf_table_cells=tuple(pdf_cells),
        pdf_nutrition_findings=tuple(findings),
        discovered_urls=documents.discovered_urls,
        rejected_urls=documents.rejected_urls,
        unread_urls=documents.unread_urls,
    )
