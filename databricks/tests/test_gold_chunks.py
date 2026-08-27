"""The chunker, run over the real fixtures, and the fixed window run beside it.

`chip_chat.databricks.gold_chunks` is two things and this file checks them differently.

The first is a schema: the chunk metadata fields, the six kinds, the source
tables, the expectations. Those are checked the way `test_silver.py` checks
silver's — the declaration says X, and something else in this repository says X
independently.

The second is the chunker itself, and it is checked by running it. Issue #35
asks for two tests and adds a condition that makes them worth having:

    A test asserting that no nutrition table is split across a chunk boundary.
    A test asserting every chunk has a citable source.
    Both tests above pass, and would fail if fixed-window chunking were
    substituted.

So `_fixed_window` is in this file. It is a real, plausible fixed-window
chunker — the one somebody reaches for on the first afternoon — and it exists
here to be failed. `test_no_nutrition_table_is_split_across_a_chunk_boundary`
and `test_fixed_window_chunking_splits_a_published_nutrition_row` run the same
assertion over the same document and reach opposite verdicts, which is the
issue's condition executed rather than asserted in a comment.

The nutrition document is `harvest/tests/fixtures/chipotle/nutrition-sheet-layout.json`,
the recorded Document Intelligence reading the harvest tests already maintain,
read through `silver.analysis_table_rows` exactly as the pipeline reads it. The
menu items, allergen vocabulary and caveats are
`catalog/tests/fixtures/catalog/`, which is a trimmed recording of the real
endpoints. Two of them being real matters: a chunker that only ever sees
hand-written prose is a chunker nobody has run.
"""

import ast
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import fields as dataclass_fields
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from chip_chat.catalog.records import AllergenDisclosure
from chip_chat.databricks import catalog, gold_chunks, silver
from chip_chat.harvest.sources.chipotle.nutrition_records import AllergenStatus

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "databricks" / "notebooks" / "gold_chunk.py"
VERIFY = REPO / "databricks" / "notebooks" / "gold_chunk_verify.py"
TERRAFORM = REPO / "infra" / "terraform" / "databricks_gold_chunk.tf"
MODULE = REPO / "databricks" / "src" / "chip_chat" / "databricks" / "gold_chunks.py"
CATALOGUE_FIXTURES = REPO / "catalog" / "tests" / "fixtures" / "catalog"
NUTRITION_SHEET = (
    REPO / "harvest" / "tests" / "fixtures" / "chipotle" / "nutrition-sheet-layout.json"
)


def _jsonl(name: str) -> tuple[dict[str, Any], ...]:
    """Return one catalogue fixture file, a row per line."""
    path = CATALOGUE_FIXTURES / name
    assert path.exists(), f"{path} is missing"
    return tuple(
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    )


@pytest.fixture(scope="module")
def menu_items() -> tuple[dict[str, Any], ...]:
    return _jsonl("menu_items.jsonl")


@pytest.fixture(scope="module")
def allergen_names() -> dict[str, str | None]:
    return {row["allergen_code"]: row.get("name") for row in _jsonl("allergens.jsonl")}


@pytest.fixture(scope="module")
def caveats() -> tuple[dict[str, Any], ...]:
    return _jsonl("caveats.jsonl")


@pytest.fixture(scope="module")
def analysis() -> str:
    """The recorded Document Intelligence reading, as bronze lands it."""
    assert NUTRITION_SHEET.exists(), f"{NUTRITION_SHEET} is missing"
    return json.dumps(json.loads(NUTRITION_SHEET.read_text())["analyzeResult"])


@pytest.fixture(scope="module")
def nutrition_rows(analysis: str) -> tuple[dict[str, Any], ...]:
    """The sheet's table rows, read the way the silver pipeline reads them."""
    rows = silver.analysis_table_rows(analysis)
    assert rows, "the fixture sheet has no table rows; the rest of this file is vacuous"
    return tuple(
        {
            **row,
            "content_sha256": "0" * 64,
            "source_url": "https://www.chipotle.com/nutrition-sheet.pdf",
            "harvested_at": "2026-01-01T12:00:11+00:00",
        }
        for row in rows
    )


# --- Rows this repository does not already hold on disk ---------------------
#
# Policy sections, FAQ entries and deduplicated prose blocks arrive from a
# harvest that would have to be run to produce a fixture, so they are written
# here, in the silver shape, the way `test_silver.py` writes its documents.

_POLICY_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "document_id": "rewards-terms",
        "document_kind": "TERMS",
        "document_title": "Chipotle Rewards Terms and Conditions",
        "position": 0,
        "heading": "Earning points",
        "text": "Members earn 10 points for every $1 spent on qualifying purchases.",
        "source_url": "https://www.chipotle.com/rewards-terms",
        "harvested_at": "2026-01-01T12:00:12+00:00",
    },
    {
        "document_id": "rewards-terms",
        "document_kind": "TERMS",
        "document_title": "Chipotle Rewards Terms and Conditions",
        "position": 1,
        "heading": "Expiration",
        "text": "Points expire six months after the month in which they were earned.",
        "source_url": "https://www.chipotle.com/rewards-terms",
        "harvested_at": "2026-01-01T12:00:12+00:00",
    },
)

_FAQ_ENTRIES: tuple[dict[str, Any], ...] = (
    {
        "category": "Rewards Program",
        "subcategory": "Points",
        "rank": 0,
        "question": "Do my points expire?",
        "answer": "Yes. Points expire six months after the month you earned them.",
        "source_url": "https://www.chipotle.com/faq",
        "harvested_at": "2026-01-01T12:00:13+00:00",
    },
)

_DOCUMENT_BLOCKS: tuple[dict[str, Any], ...] = (
    {
        "block_sha256": silver.block_digest("Our Food", "We serve real ingredients."),
        "heading": "Our Food",
        "text": "We serve real ingredients.",
        "citations": (
            {
                "harvested_at": "2026-01-01T12:00:14+00:00",
                "source_url": "https://www.chipotle.com/values",
            },
            {
                "harvested_at": "2026-01-01T12:00:09+00:00",
                "source_url": "https://www.chipotle.com/ingredients",
            },
        ),
        "source_url": "https://www.chipotle.com/values",
        "harvested_at": "2026-01-01T12:00:14+00:00",
    },
)


def every_chunk(
    menu_items: Sequence[dict[str, Any]],
    allergen_names: dict[str, str | None],
    caveats: Sequence[dict[str, Any]],
    nutrition_rows: Sequence[dict[str, Any]],
) -> tuple[gold_chunks.Chunk, ...]:
    """Return one chunk of every kind, over every fixture row available."""
    chunks: list[gold_chunks.Chunk] = []
    chunks += [gold_chunks.menu_item_chunk(row, allergen_names) for row in menu_items]
    chunks += [gold_chunks.policy_section_chunk(row) for row in _POLICY_SECTIONS]
    chunks += [gold_chunks.faq_entry_chunk(row) for row in _FAQ_ENTRIES]
    chunks += [gold_chunks.allergen_caveat_chunk(row) for row in caveats]
    chunks += [gold_chunks.document_block_chunk(row) for row in _DOCUMENT_BLOCKS]
    chunks += [gold_chunks.nutrition_row_chunk(row) for row in nutrition_rows]
    return tuple(chunks)


@pytest.fixture(scope="module")
def corpus(
    menu_items: tuple[dict[str, Any], ...],
    allergen_names: dict[str, str | None],
    caveats: tuple[dict[str, Any], ...],
    nutrition_rows: tuple[dict[str, Any], ...],
) -> tuple[gold_chunks.Chunk, ...]:
    return every_chunk(menu_items, allergen_names, caveats, nutrition_rows)


# --- The fixed window, present only to be failed -----------------------------


def _fixed_window(text: str, size: int, overlap: int = 0) -> tuple[str, ...]:
    """Chunk ``text`` every ``size`` characters, the obvious wrong way.

    This is not a strawman. It is what a chunker looks like when nobody has
    read RFC-001 §08: a window, an optional overlap, no notion of what the
    text is made of. It runs without error, produces chunks of a comfortable
    size, and embeds perfectly well. Everything about it is fine except the
    boundaries.

    It lives in the test file rather than in `chip_chat.databricks.gold_chunks`
    deliberately. A module that shipped a fixed-window chunker "for hard
    documents" would eventually have one used on a hard document, and the hard
    documents are the nutrition sheets.

    Args:
        text: The document, flattened — which is itself the first mistake, and
            is exactly what Document Intelligence's own ``content`` field
            hands you if you take it.
        size: The window, in characters.
        overlap: How much of the previous window to repeat.

    Returns:
        The windows, in order.
    """
    if size <= overlap:
        raise ValueError("a window has to advance")
    stride = size - overlap
    return tuple(text[start : start + size] for start in range(0, len(text), stride))


_WINDOW_SIZES: tuple[int, ...] = tuple(range(100, 1001, 50))
"""Nineteen plausible window sizes.

The test below asserts against every one of them that actually divides the
document rather than against a chosen one, because a test that picked the size
that happened to break would be a test somebody could fix by picking a
different size.

Sizes large enough to swallow the whole document are skipped, and the test says
how many it skipped. That is a property of the *fixture* — a recorded sheet of
443 characters — and not a defence of fixed windows: a window that holds the
entire document has not chunked it, and the real nutrition sheets this stands in
for are dozens of pages.
"""

_MINIMUM_WINDOWS: int = 3
"""How many windows a size has to produce before it counts as chunking."""


def _row_label(row: dict[str, Any]) -> str:
    """Return the published item name a nutrition row is about."""
    return str(row["cells"][0])


def _figures(row: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return the row's ``(heading, value)`` pairs, holes dropped."""
    return tuple(
        (str(header), str(cell))
        for header, cell in zip(row["column_headers"], row["cells"], strict=True)
        if header and cell
    )


def _chunks_holding(texts: Sequence[str], row: dict[str, Any]) -> list[int]:
    """Return the indices of the chunks that mention this row's item at all."""
    return [index for index, text in enumerate(texts) if _row_label(row) in text]


def _rows_kept_whole(texts: Sequence[str], rows: Sequence[dict[str, Any]]) -> list[str]:
    """Return the labels of the rows that survived chunking whole.

    This is the assertion both of the tests below make, and making it once is
    the point: the structured chunker and the fixed window are run through the
    same function and reach opposite verdicts.

    A row survived whole when exactly one chunk mentions its item, and that
    chunk carries **every published figure of the row together with the
    heading that figure is under**. The second half is what a boundary
    destroys and what `docs/decisions/pdf-tables.md` is about: a number that
    has been separated from its column is not a weaker fact, it is a different
    one, and nothing downstream can tell.

    Args:
        texts: One string per chunk, in order. Both chunkers produce these;
            neither is given an advantage the other does not have.
        rows: The published table rows, as `silver.analysis_table_rows` read
            them.

    Returns:
        The item labels of the rows that survived, in the order given.
    """
    kept: list[str] = []
    for row in rows:
        holders = _chunks_holding(texts, row)
        if len(holders) != 1:
            continue
        chunk = texts[holders[0]]
        if all(value in chunk and header in chunk for header, value in _figures(row)):
            kept.append(_row_label(row))
    return kept


# --- #35's first required test ----------------------------------------------


def test_no_nutrition_table_is_split_across_a_chunk_boundary(
    nutrition_rows: tuple[dict[str, Any], ...],
) -> None:
    """Every published row is one chunk, whole, every figure under its heading.

    The failure this rules out is the one `docs/decisions/pdf-tables.md`
    describes: a chunk holding Cheese's sodium immediately before the word
    Guacamole, under no heading at all. Every ingredient of a confident wrong
    answer, and nothing downstream able to detect it.
    """
    chunks = [gold_chunks.nutrition_row_chunk(row) for row in nutrition_rows]
    assert len(chunks) == len(nutrition_rows)

    texts = [chunk.text for chunk in chunks]
    assert _rows_kept_whole(texts, nutrition_rows) == [
        _row_label(row) for row in nutrition_rows
    ]

    for row, chunk in zip(nutrition_rows, chunks, strict=True):
        # Stricter than the shared assertion, because this chunker can be held
        # to it: every figure is rendered immediately against its own heading
        # rather than merely in the same chunk as it.
        for header, value in _figures(row):
            assert f"{header}: {value}" in chunk.text, (_row_label(row), header)

        # And the structure is on the chunk as well as in it, positionally
        # aligned — what an answer quotes when it needs the figure rather than
        # the sentence.
        assert chunk.column_headers is not None
        assert chunk.cells is not None
        assert len(chunk.column_headers) == len(chunk.cells)
        assert list(chunk.cells) == list(row["cells"])


def test_fixed_window_chunking_splits_a_published_nutrition_row(
    analysis: str, nutrition_rows: tuple[dict[str, Any], ...]
) -> None:
    """The same assertion, over the same sheet, chunked by length. It fails.

    This is #35's "would fail if fixed-window chunking were substituted",
    executed. `analyzeResult.content` is the flattened text Document
    Intelligence returns beside the structure, and taking it is the whole of
    the mistake: the table is in there as a run of tokens, and the headings
    that give its numbers meaning are printed once, at the top, where no
    character offset past the first window can reach them.

    Asserted over nineteen window sizes rather than one, so that it is a
    statement about fixed windows and not about a number.
    """
    content = json.loads(analysis)["content"]
    assert content, "the fixture has no flattened content to chunk badly"
    every_row = [_row_label(row) for row in nutrition_rows]
    tried = 0

    for size in _WINDOW_SIZES:
        for overlap in (0, size // 4):
            windows = _fixed_window(content, size, overlap)
            if len(windows) < _MINIMUM_WINDOWS:
                continue  # a window holding the whole sheet has not chunked it
            tried += 1
            kept = _rows_kept_whole(windows, nutrition_rows)
            assert kept != every_row, (
                f"a {size}-character window with {overlap} overlap kept every "
                "row of this sheet whole; the assertion above would not have "
                "caught the substitution"
            )

    assert tried >= 5, (
        f"only {tried} window sizes actually divided this {len(content)}-"
        "character fixture, which is too few to call this a statement about "
        "fixed windows"
    )


def test_fixed_window_chunking_strands_figures_under_no_heading(
    analysis: str, nutrition_rows: tuple[dict[str, Any], ...]
) -> None:
    """The concrete shape of that failure, named.

    A window past the first has left the header row behind at the top of the
    document. The figures in it are still numbers, still adjacent to an item
    name, and no longer attached to the column that says what they measure.
    That chunk retrieves well and answers wrongly, which is the combination
    RFC-001 §08 refuses to accept for allergen and nutrition questions.
    """
    content = json.loads(analysis)["content"]
    headings = {
        header
        for row in nutrition_rows
        for header in row["column_headers"]
        if header and header != "Item"
    }
    figures = {str(cell) for row in nutrition_rows for cell in row["cells"][2:] if cell}

    windows = _fixed_window(content, 300)
    stranded = [
        window
        for window in windows[1:]
        if any(figure in window for figure in figures)
        and not any(heading in window for heading in headings)
    ]
    assert stranded, "expected at least one window carrying figures and no heading"


# --- #35's second required test ---------------------------------------------


def test_every_chunk_has_a_citable_source(corpus: tuple[gold_chunks.Chunk, ...]) -> None:
    """Both halves of a citation, on every chunk of every kind.

    RFC-001 §08: citations are part of the payload, not reconstructed
    afterwards. `harvested_at` is as load-bearing as the URL — the citation
    decision renders it beside an allergen claim without interaction, because
    published allergen data goes stale and the corpus is re-harvested weekly.
    """
    assert {chunk.kind for chunk in corpus} == set(gold_chunks.KINDS), (
        "a kind with no chunk in this corpus is a kind this test does not check"
    )
    for chunk in corpus:
        assert chunk.source_url, (chunk.kind, chunk.chunk_id)
        assert str(chunk.source_url).startswith("https://"), chunk.source_url
        assert chunk.harvested_at, (chunk.kind, chunk.chunk_id)


def test_the_citation_is_an_expectation_and_not_only_a_habit() -> None:
    """A property every chunk happens to have is not a property every chunk has."""
    constraint = {entry.name: entry.constraint for entry in gold_chunks.expectations()}
    assert constraint["carries_its_citation"] == (
        f"{gold_chunks.SOURCE_URL} IS NOT NULL AND {gold_chunks.HARVESTED_AT} IS NOT NULL"
    )


def test_fixed_window_chunking_cannot_say_which_page_a_chunk_came_from() -> None:
    """The same assertion over a windowed corpus. It fails, and worse than absent.

    Three documents, three different published sources. Flattened and windowed,
    a chunk falls across two of them — so the question is not "which URL is
    missing" but "which of the two URLs is this sentence from", and any answer
    the pipeline gives is right about half of the chunk and wrong about the
    rest. A citation that is confidently wrong is worse than one that is
    absent: absent fails the check, wrong passes it.
    """
    documents = (
        ("https://www.chipotle.com/allergens", "Cross-contact occurs in preparation. "),
        ("https://www.chipotle.com/rewards-terms", "Points expire after six months. "),
        ("https://www.chipotle.com/faq", "Delivery prices differ from in-store. "),
    )
    flattened = "".join(text for _, text in documents)
    windows = _fixed_window(flattened, 50)

    spanning = [
        window
        for window in windows
        if sum(1 for _, text in documents if text.strip() and _shares(window, text)) > 1
    ]
    assert spanning, "expected a window spanning two documents"


def _shares(window: str, text: str) -> bool:
    """Return whether ``window`` holds at least eight characters of ``text``."""
    return any(text[start : start + 8] in window for start in range(len(text) - 8))


# --- The metadata schema, fixed ---------------------------------------------


def test_the_chunk_dataclass_and_the_declared_schema_are_the_same_schema() -> None:
    """#35's first acceptance criterion: the metadata schema is fixed.

    Fixed means one copy. The renderers return a `Chunk`, the pipeline writes
    `FIELDS`, and #48 builds its index from `FIELDS` — so a column added to one
    and not the other is a field the index has and nothing populates.
    """
    assert [entry.name for entry in gold_chunks.FIELDS] == [
        entry.name for entry in dataclass_fields(gold_chunks.Chunk)
    ]


def test_a_chunk_renders_to_exactly_the_declared_columns(
    corpus: tuple[gold_chunks.Chunk, ...],
) -> None:
    for chunk in corpus:
        assert list(chunk.as_row()) == [entry.name for entry in gold_chunks.FIELDS]


def test_the_issue_asks_for_these_fields_by_name() -> None:
    """The issue's own list: item id, category, allergens, calories, section
    heading, source url, harvest timestamp."""
    names = {entry.name for entry in gold_chunks.FIELDS}
    for required in (
        gold_chunks.ITEM_ID,
        gold_chunks.CATEGORY,
        gold_chunks.ALLERGENS,
        gold_chunks.CALORIES,
        gold_chunks.HEADING,
        gold_chunks.SOURCE_URL,
        gold_chunks.HARVESTED_AT,
    ):
        assert required in names, required


def test_the_comparative_questions_are_filters_and_not_paraphrases() -> None:
    """ "Fewer calories", "vegetarian", "without dairy" — #48's own three.

    Each is a constraint over a typed column here or it is a language model
    reading numbers out of prose, and the second one is how a calorie
    comparison comes back wrong."""
    filters = set(gold_chunks.filterable())
    assert gold_chunks.CALORIES in filters
    assert gold_chunks.ALLERGENS in filters
    assert gold_chunks.CATEGORY in filters
    assert "primary_filling" in filters


def test_every_facetable_field_is_also_filterable() -> None:
    """A facet you cannot filter on is a count nobody can act on."""
    assert set(gold_chunks.facetable()) <= set(gold_chunks.filterable())


def test_the_free_text_field_is_not_facetable() -> None:
    """Faceting a sentence buys a histogram of sentences."""
    assert not gold_chunks.field(gold_chunks.TEXT).facetable
    assert not gold_chunks.field(gold_chunks.TEXT).filterable


def test_the_citation_fields_come_back_with_a_hit() -> None:
    """#48: 'retrievable and not merely filterable'. A citation the application
    cannot read back is not a citation."""
    retrievable = set(gold_chunks.retrievable())
    assert gold_chunks.SOURCE_URL in retrievable
    assert gold_chunks.HARVESTED_AT in retrievable
    assert gold_chunks.CHUNK_ID in retrievable


def test_a_field_says_which_kinds_populate_it() -> None:
    """So that a null is a documented shape rather than something learned from
    a query."""
    assert gold_chunks.field(gold_chunks.ITEM_ID).kinds == (gold_chunks.MENU_ITEM,)
    assert gold_chunks.field(gold_chunks.TEXT).universal
    assert gold_chunks.field(gold_chunks.SOURCE_URL).universal


def test_columns_for_a_kind_are_its_own_plus_the_universal_ones() -> None:
    names = [entry.name for entry in gold_chunks.columns_for(gold_chunks.NUTRITION_ROW)]
    assert gold_chunks.COLUMN_HEADERS in names
    assert gold_chunks.CHUNK_ID in names
    assert gold_chunks.ITEM_ID not in names


def test_columns_for_refuses_a_kind_nothing_produces() -> None:
    with pytest.raises(ValueError, match="unknown chunk kind"):
        list(gold_chunks.columns_for("PARAGRAPH"))


def test_field_lookup_refuses_a_column_the_table_does_not_have() -> None:
    with pytest.raises(KeyError, match="no chunk field is called"):
        gold_chunks.field("sodium")


# --- One menu item is one chunk ---------------------------------------------


def test_one_menu_item_is_exactly_one_chunk(
    menu_items: tuple[dict[str, Any], ...], allergen_names: dict[str, str | None]
) -> None:
    """The issue's first rule, counted."""
    chunks = [gold_chunks.menu_item_chunk(row, allergen_names) for row in menu_items]
    assert len(chunks) == len(menu_items)
    assert len({chunk.chunk_id for chunk in chunks}) == len(menu_items)
    assert {chunk.item_id for chunk in chunks} == {row["item_id"] for row in menu_items}


def test_a_menu_chunk_carries_its_nutrition_as_a_column_and_not_only_as_prose(
    menu_items: tuple[dict[str, Any], ...], allergen_names: dict[str, str | None]
) -> None:
    """ "as metadata, not as prose to be re-parsed by a model" — the issue.

    Re-parsing "230 calories" out of a sentence is a step that can go wrong.
    Reading a decimal column is not.
    """
    guacamole = next(row for row in menu_items if row["name"] == "Guacamole")
    chunk = gold_chunks.menu_item_chunk(guacamole, allergen_names)
    assert chunk.calories == guacamole["calories"]
    assert chunk.category == guacamole["category"]
    assert chunk.item_type == guacamole["item_type"]
    assert chunk.allergens == tuple(sorted(guacamole["allergens"]))
    # And in the prose too, because prose is what gets embedded and a chunk
    # that never says "calories" is not retrieved for a question about them.
    assert "calories" in chunk.text


def test_a_published_figure_reads_the_way_it_was_published() -> None:
    """Silver casts calories to `DECIMAL(8,2)` because a published figure is
    exact. That is the right type and the wrong sentence — "230.00 calories" is
    a number no page printed. The trim is display only; the column keeps what
    silver cast."""
    chunk = gold_chunks.menu_item_chunk(
        {
            "item_id": "CMG-1001",
            "name": "Guacamole",
            "category": None,
            "item_type": "Toppings",
            "calories": Decimal("230.00"),
            "allergens": [],
            "allergen_disclosure": "PUBLISHED",
            "source_url": "https://www.chipotle.com/menu",
            "harvested_at": "2026-01-01T12:00:00+00:00",
        }
    )
    assert "230 calories" in chunk.text
    assert "230.00" not in chunk.text
    assert chunk.calories == "230.00"


def test_a_figure_with_real_precision_keeps_it() -> None:
    """The trim stops at the last significant digit, not at a fixed number of
    places. Half a gram of saturated fat is half a gram."""
    chunk = gold_chunks.menu_item_chunk(
        {
            "item_id": "CMG-1",
            "name": "Fixture Item",
            "category": "Side",
            "item_type": "Side",
            "calories": Decimal("1.50"),
            "allergens": [],
            "allergen_disclosure": "PUBLISHED",
            "source_url": "https://www.chipotle.com/menu",
            "harvested_at": "2026-01-01T12:00:00+00:00",
        }
    )
    assert "1.5 calories" in chunk.text


def test_a_composed_item_says_its_calories_are_one_component(
    menu_items: tuple[dict[str, Any], ...], allergen_names: dict[str, str | None]
) -> None:
    """A Chicken Bowl's published 180 calories are the chicken. A chunk that
    presented them as the meal would understate a burrito by a factor."""
    composed = next(row for row in menu_items if row["is_composed"])
    chunk = gold_chunks.menu_item_chunk(composed, allergen_names)
    assert chunk.is_composed is True
    assert "component alone" in chunk.text


def test_an_item_with_no_published_calorie_figure_says_so(
    allergen_names: dict[str, str | None],
) -> None:
    """Null is not zero, and a chunk that stayed silent would be summarised as
    a zero by the next thing that reads it."""
    chunk = gold_chunks.menu_item_chunk(
        {
            "item_id": "CMG-9",
            "name": "Fixture Item",
            "category": "Side",
            "item_type": "Side",
            "calories": None,
            "allergens": [],
            "allergen_disclosure": "PUBLISHED",
            "source_url": "https://www.chipotle.com/menu",
            "harvested_at": "2026-01-01T12:00:00+00:00",
        },
        allergen_names,
    )
    assert chunk.calories is None
    assert "publishes no calorie figure" in chunk.text


def test_the_two_allergen_silences_do_not_read_the_same(
    allergen_names: dict[str, str | None],
) -> None:
    """The one that matters most. `docs/decisions/allergen-absence.md`: an
    empty allergens array means either "marks are published and none is this
    one" or "nothing is published at all", and the second read as the first is
    a wrong allergen answer given to a stranger on the open internet."""
    base = {
        "item_id": "CMG-9",
        "name": "Fixture Item",
        "category": "Side",
        "item_type": "Side",
        "calories": "100",
        "allergens": [],
        "source_url": "https://www.chipotle.com/menu",
        "harvested_at": "2026-01-01T12:00:00+00:00",
    }
    published = gold_chunks.menu_item_chunk({**base, "allergen_disclosure": "PUBLISHED"})
    silent = gold_chunks.menu_item_chunk({**base, "allergen_disclosure": "NOT_PUBLISHED"})

    assert published.text != silent.text
    assert published.allergen_disclosure != silent.allergen_disclosure
    assert "marks none of them" in published.text
    assert "not a statement that the item is free" in published.text
    assert "publishes no allergen information" in silent.text


def test_an_allergen_renders_under_its_published_label(
    menu_items: tuple[dict[str, Any], ...], allergen_names: dict[str, str | None]
) -> None:
    marked = next(row for row in menu_items if row["allergens"])
    chunk = gold_chunks.menu_item_chunk(marked, allergen_names)
    for code in marked["allergens"]:
        assert (allergen_names.get(code) or code) in chunk.text


def test_a_code_with_no_published_label_renders_as_the_code() -> None:
    """Two of Chipotle's codes have no published label. Inventing one would put
    a word in the source's mouth in the place this project can least afford."""
    chunk = gold_chunks.menu_item_chunk(
        {
            "item_id": "CMG-9",
            "name": "Fixture Item",
            "category": "Side",
            "item_type": "Side",
            "calories": "100",
            "allergens": ["sulf"],
            "allergen_disclosure": "PUBLISHED",
            "source_url": "https://www.chipotle.com/menu",
            "harvested_at": "2026-01-01T12:00:00+00:00",
        },
        {"sulf": None},
    )
    assert "sulf" in chunk.text


def test_a_menu_row_with_no_item_id_is_refused(
    allergen_names: dict[str, str | None],
) -> None:
    with pytest.raises(ValueError, match="published item_id"):
        gold_chunks.menu_item_chunk({"name": "Nameless"}, allergen_names)


def test_a_disclosure_outside_the_published_two_is_refused() -> None:
    with pytest.raises(ValueError, match="two silences must not merge"):
        gold_chunks.menu_item_chunk(
            {
                "item_id": "CMG-9",
                "name": "Fixture Item",
                "allergen_disclosure": "UNKNOWN",
            }
        )


# --- Policy and FAQ documents chunk by section -------------------------------


def test_a_policy_document_chunks_at_its_own_sections() -> None:
    """The issue's second rule. Two published sections, two chunks, and the
    boundary is the one the page drew."""
    chunks = [gold_chunks.policy_section_chunk(row) for row in _POLICY_SECTIONS]
    assert len(chunks) == len(_POLICY_SECTIONS)
    assert [chunk.heading for chunk in chunks] == ["Earning points", "Expiration"]
    assert [chunk.position for chunk in chunks] == [0, 1]
    assert chunks[0].document_kind == "TERMS"


def test_a_policy_chunk_names_the_document_it_is_a_section_of() -> None:
    """A section quoted without its document is a rule with no contract behind
    it, and retrieval cannot prefer the contract over the page explaining it."""
    chunk = gold_chunks.policy_section_chunk(_POLICY_SECTIONS[1])
    assert "Chipotle Rewards Terms and Conditions" in chunk.text
    assert chunk.document_id == "rewards-terms"


def test_a_section_that_names_no_document_is_refused() -> None:
    with pytest.raises(ValueError, match="the document it is a section of"):
        gold_chunks.policy_section_chunk({"position": 0, "text": "Points expire."})


def test_a_faq_question_and_its_answer_are_one_chunk() -> None:
    """Separated, the answer is an answer to something the reader has to guess
    and retrieval has nothing to match the question against."""
    chunk = gold_chunks.faq_entry_chunk(_FAQ_ENTRIES[0])
    assert "Do my points expire?" in chunk.text
    assert "six months" in chunk.text
    assert chunk.heading == "Do my points expire?"


def test_a_faq_chunk_carries_its_published_trail() -> None:
    """ "Rewards Program > Points" in front of an answer about expiry is the
    difference between matching "do my Chipotle points expire" and matching the
    word "expire"."""
    chunk = gold_chunks.faq_entry_chunk(_FAQ_ENTRIES[0])
    assert "Rewards Program > Points" in chunk.text


def test_a_faq_row_with_no_question_is_refused() -> None:
    with pytest.raises(ValueError, match="published question"):
        gold_chunks.faq_entry_chunk({"category": "Rewards Program", "answer": "Yes."})


# --- The caveats -------------------------------------------------------------


def test_every_published_caveat_is_its_own_chunk(
    caveats: tuple[dict[str, Any], ...],
) -> None:
    """Appended to every item chunk instead, the cross-contact caveat would
    dominate hundreds of embeddings and be retrieved for questions that are not
    about allergens at all."""
    chunks = [gold_chunks.allergen_caveat_chunk(row) for row in caveats]
    assert len(chunks) == len(caveats)
    assert len({chunk.chunk_id for chunk in chunks}) == len(caveats)


def test_the_cross_contact_caveat_survives_into_a_chunk(
    caveats: tuple[dict[str, Any], ...],
) -> None:
    """PRD K3's safety sentence. If it is not retrievable it is not said."""
    texts = " ".join(gold_chunks.allergen_caveat_chunk(row).text for row in caveats)
    assert "contact" in texts.lower()


def test_a_heading_the_text_already_opens_with_is_not_said_twice(
    caveats: tuple[dict[str, Any], ...],
) -> None:
    """A hand-review finding, from `docs/corpus-chunking.md` §6.

    Chipotle's "GLUTEN INTOLERANCE & CELIAC DISEASE" caveat carries its own
    heading as the first line of its text, and the first draft of this module
    prepended it anyway. The result stated the heading twice, doubled the
    weight of those five words in the embedding, and read to a visitor as a
    stutter in an answer about celiac disease.
    """
    repeated = next(
        row
        for row in caveats
        if row.get("heading")
        and silver.normalise(row["text"]).startswith(silver.normalise(row["heading"]))
    )
    text = gold_chunks.allergen_caveat_chunk(repeated).text
    heading = silver.normalise(repeated["heading"])
    assert text.startswith(heading)
    assert text.count(heading) == 1


def test_a_heading_the_text_does_not_open_with_is_kept() -> None:
    """The other half. A chunk retrieved without its heading is a paragraph
    with no subject."""
    chunk = gold_chunks.allergen_caveat_chunk(
        {
            "position": 9,
            "heading": "Cross-contact",
            "text": "Foods contact one another during preparation.",
            "source_url": "https://www.chipotle.com/allergens",
            "harvested_at": "2026-01-01T12:00:10+00:00",
        }
    )
    assert chunk.text.startswith("Cross-contact.")


def test_an_empty_caveat_is_refused() -> None:
    with pytest.raises(ValueError, match="is not a caveat"):
        gold_chunks.allergen_caveat_chunk({"position": 0, "text": "  "})


# --- Prose blocks silver already deduplicated --------------------------------


def test_a_document_block_keeps_every_source_that_published_it() -> None:
    """Silver collapsed the block and conserved the citations. Dropping the
    array here would undo that at the last step: the payload would name one
    page for a fact that two published."""
    chunk = gold_chunks.document_block_chunk(_DOCUMENT_BLOCKS[0])
    assert chunk.citations is not None
    assert len(chunk.citations) == 2
    assert chunk.source_url == "https://www.chipotle.com/values"


def test_a_document_block_is_keyed_on_the_digest_silver_gave_it() -> None:
    """The one kind whose key is its content, because anonymous prose has no
    published identifier to name it by."""
    block = _DOCUMENT_BLOCKS[0]
    assert gold_chunks.document_block_chunk(block).chunk_id == gold_chunks.chunk_id(
        gold_chunks.DOCUMENT_BLOCK, block["block_sha256"]
    )


def test_a_block_with_no_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="block_sha256"):
        gold_chunks.document_block_chunk(
            {"heading": "Our Food", "text": "Real ingredients."}
        )


# --- Nutrition rows ----------------------------------------------------------


def test_a_hole_in_a_row_is_named_rather_than_closed_up() -> None:
    """Skipping the hole would shift every heading after it onto the wrong
    number, which is the same failure as a split boundary arriving by a
    different route."""
    chunk = gold_chunks.nutrition_row_chunk(
        {
            "content_sha256": "0" * 64,
            "table_index": 0,
            "row_index": 1,
            "column_headers": ["Item", "Sodium (mg)"],
            "cells": ["Guacamole", None],
            "source_url": "https://www.chipotle.com/nutrition-sheet.pdf",
            "harvested_at": "2026-01-01T12:00:11+00:00",
        }
    )
    assert "Sodium (mg): not published" in chunk.text
    assert chunk.cells == ("Guacamole", None)


def test_a_row_with_more_cells_than_headings_is_refused() -> None:
    """A figure whose column is a guess is the thing this kind exists to
    prevent, so it stops here rather than being rendered without one."""
    with pytest.raises(ValueError, match="one heading per cell"):
        gold_chunks.nutrition_row_chunk(
            {"column_headers": ["Item"], "cells": ["Guacamole", "230"]}
        )


def test_a_row_with_no_cells_is_refused() -> None:
    with pytest.raises(ValueError, match="is not a row"):
        gold_chunks.nutrition_row_chunk({"column_headers": [], "cells": []})


def test_a_nutrition_chunk_cites_the_page_it_was_read_from(
    nutrition_rows: tuple[dict[str, Any], ...],
) -> None:
    chunk = gold_chunks.nutrition_row_chunk(nutrition_rows[0])
    assert chunk.page_number == nutrition_rows[0]["page_number"]
    assert str(chunk.page_number) in chunk.text


# --- Identity ----------------------------------------------------------------


def test_a_chunk_id_names_what_the_chunk_is_about_and_not_what_it_says() -> None:
    """So a weekly rebuild does not retire the id a two-turn-old conversation
    is still citing."""
    before = {
        "item_id": "CMG-101",
        "name": "Chicken Bowl",
        "category": "Entree",
        "item_type": "Bowl",
        "calories": "180",
        "allergens": [],
        "allergen_disclosure": "PUBLISHED",
        "source_url": "https://www.chipotle.com/menu",
        "harvested_at": "2026-01-01T12:00:00+00:00",
    }
    after = {**before, "calories": "190", "harvested_at": "2026-01-08T12:00:00+00:00"}
    first = gold_chunks.menu_item_chunk(before)
    second = gold_chunks.menu_item_chunk(after)
    assert first.chunk_id == second.chunk_id
    assert first.text != second.text


def test_two_kinds_with_the_same_key_are_two_chunks() -> None:
    """Position zero of the caveats and position zero of a policy document are
    not the same thing, and an id that collided them would make one of them
    uncitable."""
    assert gold_chunks.chunk_id(gold_chunks.ALLERGEN_CAVEAT, 0) != gold_chunks.chunk_id(
        gold_chunks.POLICY_SECTION, 0
    )


def test_a_missing_key_part_is_not_the_empty_string() -> None:
    """ "This document published no heading" and "this document published an
    empty heading" are different documents."""
    assert gold_chunks.chunk_id(gold_chunks.POLICY_SECTION, None) != gold_chunks.chunk_id(
        gold_chunks.POLICY_SECTION, ""
    )


def test_an_id_over_the_kind_alone_is_refused() -> None:
    """It would make every chunk of that kind the same chunk."""
    with pytest.raises(ValueError, match="the kind alone is not one"):
        gold_chunks.chunk_id(gold_chunks.MENU_ITEM)


def test_an_unknown_kind_has_no_id() -> None:
    with pytest.raises(ValueError, match="unknown chunk kind"):
        gold_chunks.chunk_id("PARAGRAPH", "CMG-1")


def test_no_two_chunks_in_the_corpus_share_an_id(
    corpus: tuple[gold_chunks.Chunk, ...],
) -> None:
    """Citation ids are compared, so a collision is two facts one of which can
    never be quoted."""
    ids = [chunk.chunk_id for chunk in corpus]
    assert len(ids) == len(set(ids))


def test_identity_normalises_the_way_silver_does() -> None:
    """One rule for what makes two pieces of text the same text, in a layer
    that cannot import the module that states it."""
    for text in ("Steak\xa0 Burrito\n", "  Chips  and   Guacamole ", "Café"):
        assert gold_chunks.normalise(text) == silver.normalise(text)


# --- There is nowhere for a window to go ------------------------------------


@pytest.fixture(scope="module")
def module_source() -> str:
    assert MODULE.exists(), f"{MODULE} is missing"
    return MODULE.read_text()


def test_the_chunker_holds_no_window_no_overlap_and_no_truncation(
    module_source: str,
) -> None:
    """The structural version of #35's third bullet.

    A chunker with a length in it will one day use it, and the documents it
    will be used on are the hard ones — the nutrition sheets. So there is
    nothing in this module that slices text, no overlap, no target length and
    nothing that truncates.

    Read off the parsed module with the docstrings stripped, because the
    docstrings argue about windows at length and a test that read the argument
    as a violation would push the argument out of the module.
    """
    body = _code_without_prose(module_source)
    for forbidden in ("textwrap", "chunk_size", "overlap", "max_chars", "window"):
        assert forbidden not in body, forbidden

    # The budget is allowed to be declared. It is not allowed to be read,
    # because the only thing this module could do with it is split.
    assert body.count("EMBEDDING_CHARACTER_BUDGET") == 1, (
        "EMBEDDING_CHARACTER_BUDGET should appear once in the code — its own "
        "declaration — and nowhere that reads it"
    )


def test_the_module_imports_nothing_but_the_standard_library(
    module_source: str,
) -> None:
    """The load-bearing constraint, for the third time.

    A Lakeflow pipeline runs a notebook in the workspace, not an installed
    wheel, so Terraform uploads this file beside the notebook and the notebook
    puts its directory on `sys.path`. One import of a sibling — even
    `chip_chat.databricks.silver`, whose constants this module copies — and the
    upload stops being enough, and the failure surfaces on the driver rather
    than here.
    """
    imported: set[str] = set()
    for node in ast.walk(ast.parse(module_source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(
        sys.stdlib_module_names
    )
    assert "chip_chat" not in imported


def _code_without_prose(source: str) -> str:
    """Return ``source`` as the names it uses, with every string emptied.

    Comments go when `ast.unparse` rebuilds the tree. Docstrings and the
    string literals this module writes its Unity Catalog comments and its
    error messages in are blanked here, so that what is left is what the
    module *does* rather than what it says about what it does. The distinction
    matters for exactly this test: `gold_chunks.CHUNK_COMMENT` contains the sentence
    "there is no window size anywhere in chip_chat.databricks.gold_chunks", and a
    substring search over the raw file would read that promise as a breach of
    itself.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    return ast.unparse(tree)


def test_the_budget_reports_and_does_not_split(
    corpus: tuple[gold_chunks.Chunk, ...],
) -> None:
    """Every fixture chunk is comfortably under it, and nothing checked."""
    assert all(
        chunk.character_count <= gold_chunks.EMBEDDING_CHARACTER_BUDGET
        for chunk in corpus
    )
    assert gold_chunks.EMBEDDING_CHARACTER_BUDGET > 0


def test_a_long_published_section_stays_one_chunk() -> None:
    """The case the budget is about. A publisher who stopped using headings
    produces a long section, and the answer is to say so, not to invent a
    boundary they did not draw."""
    long_text = "Points expire six months after the month earned. " * 400
    chunk = gold_chunks.policy_section_chunk(
        {
            "document_id": "rewards-terms",
            "document_kind": "TERMS",
            "document_title": "Terms",
            "position": 0,
            "heading": "Expiration",
            "text": long_text,
            "source_url": "https://www.chipotle.com/rewards-terms",
            "harvested_at": "2026-01-01T12:00:12+00:00",
        }
    )
    assert chunk.character_count > gold_chunks.EMBEDDING_CHARACTER_BUDGET
    assert chunk.text.endswith("earned.")


# --- Agreement with everything else -----------------------------------------


def test_gold_is_a_layer_of_the_medallion() -> None:
    assert gold_chunks.LAYER in catalog.LAYERS


def test_chunks_are_harvested_and_there_is_no_synthetic_corpus() -> None:
    """RFC-001 §04's boundary, at the one place it would do the most damage to
    blur: an invented order reaching retrieval is a fabricated fact with a
    real-looking citation on it."""
    assert gold_chunks.STREAM in catalog.STREAMS
    assert gold_chunks.STREAM == "harvested"
    assert all(gold_chunks.source(kind).table for kind in gold_chunks.KINDS)


def test_the_schema_name_is_the_one_terraform_created() -> None:
    assert gold_chunks.schema_name() == catalog.schema("gold", "harvested").name


def test_the_allergen_states_are_the_harvest_s_own() -> None:
    assert gold_chunks.CONTAINS == AllergenStatus.CONTAINS
    assert gold_chunks.NOT_PUBLISHED == AllergenStatus.NOT_PUBLISHED
    assert set(gold_chunks.DISCLOSURES) == {member.value for member in AllergenDisclosure}


def test_the_expectation_record_is_the_shape_silver_uses() -> None:
    """A copy, for the reason the module docstring gives. A drift here would be
    a pipeline that reads `constraint` off something that calls it something
    else."""
    assert [entry.name for entry in dataclass_fields(gold_chunks.Expectation)] == [
        entry.name for entry in dataclass_fields(silver.Expectation)
    ]


def test_every_kind_is_produced_by_exactly_one_source() -> None:
    assert [entry.kind for entry in gold_chunks.SOURCES] == list(gold_chunks.KINDS)
    assert len({entry.kind for entry in gold_chunks.SOURCES}) == len(gold_chunks.SOURCES)


def test_every_source_reads_a_silver_table_that_exists() -> None:
    """A chunk source naming a table nothing conforms is a pipeline that fails
    on the cluster, minutes in, with a message about a missing table."""
    for entry in gold_chunks.SOURCES:
        _silver_table(entry.table)


def _silver_table(name: str) -> None:
    """Assert that silver publishes ``name`` in `silver_harvested`."""
    try:
        assert silver.table(name).stream == "harvested"
    except KeyError:
        assert silver.corpus(name).stream == "harvested"


def test_every_renderer_named_by_a_source_exists_and_is_used() -> None:
    """Both directions: no source names a function that is not there, and no
    renderer sits in the module unreachable from the pipeline."""
    named = {entry.renderer for entry in gold_chunks.SOURCES}
    for name in named:
        assert callable(getattr(gold_chunks, name)), name
    exported = {
        name
        for name in gold_chunks.__all__
        if name.endswith("_chunk") and callable(getattr(gold_chunks, name))
    }
    assert exported == named


def test_source_lookup_refuses_a_kind_nothing_produces() -> None:
    with pytest.raises(KeyError, match="nothing produces"):
        gold_chunks.source("PARAGRAPH")


def test_every_expectation_names_what_is_true_rather_than_what_went_wrong() -> None:
    """The event log reads `carries_its_citation` failed. Lower snake case, and
    a statement — the same convention silver's carry."""
    for entry in gold_chunks.expectations():
        assert entry.name == entry.name.lower()
        assert " " not in entry.name
        assert entry.why


def test_a_per_kind_expectation_passes_for_every_other_kind() -> None:
    """The obvious spelling — `kind = 'X' AND ...` — fails every row that is
    not an X, which would stop the pipeline on the first chunk of any other
    kind."""
    for entry in gold_chunks.expectations():
        if f"{gold_chunks.KIND} <> " in entry.constraint:
            assert " OR (" in entry.constraint, entry.name


def test_the_nutrition_expectation_is_the_first_required_test_as_a_constraint() -> None:
    constraint = {entry.name: entry.constraint for entry in gold_chunks.expectations()}
    assert (
        f"size({gold_chunks.COLUMN_HEADERS}) = size(cells)"
        in constraint["keeps_the_row_whole"]
    )


# --- The notebooks and the Terraform ----------------------------------------


@pytest.fixture(scope="module")
def notebook() -> str:
    assert NOTEBOOK.exists(), f"{NOTEBOOK} is missing"
    return NOTEBOOK.read_text()


def test_the_pipeline_is_written_against_lakeflow_and_not_dlt(notebook: str) -> None:
    """Delta Live Tables became Lakeflow Spark Declarative Pipelines in 2026."""
    code = "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )
    assert "from pyspark import pipelines as dp" in code
    assert "import dlt" not in code


def test_every_expectation_in_the_pipeline_is_fatal(notebook: str) -> None:
    """A dropped chunk is a fact that silently stops being retrievable, which is
    the corpus quietly getting smaller."""
    assert "dp.expect_all_or_fail" in notebook
    assert "dp.expect_or_drop" not in notebook
    assert "dp.expect_all(" not in notebook


def test_the_pipeline_reads_silver_and_never_bronze(notebook: str) -> None:
    """Gold chunks what silver decided was true.

    Reading bronze here would chunk a row silver had quarantined, deduplicated
    away or failed an expectation on — a retrievable sentence this lakehouse
    has already decided is not true."""
    code = "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )
    assert "silver.LAYER" in code
    assert "import bronze" not in code
    assert "bronze." not in code


def test_the_pipeline_never_reads_the_synthetic_stream(notebook: str) -> None:
    """An invented order reaching the retrieval index is the worst thing this
    lakehouse could produce.

    Asserted over the code and not the prose: the markdown above it argues at
    length about why the stream is a constant rather than a loop variable, and
    a test that read the argument as a violation would push the argument out of
    the notebook."""
    code = "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )
    assert "synthetic" not in code
    assert "catalog.STREAMS" not in code
    assert "gold_chunks.STREAM" in code


def test_the_chunk_struct_is_built_from_the_declared_schema(notebook: str) -> None:
    """Rather than beside it, so a column added to `gold_chunks.FIELDS` arrives in the
    pipeline without anyone remembering to add it there too."""
    code = "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )
    assert "for entry in gold_chunks.FIELDS" in code
    assert "raise ValueError" in code, (
        "an unknown SQL type must raise; a silently stringified DECIMAL puts "
        "the calorie comparison back into prose"
    )


def test_the_union_across_the_kinds_is_by_name(notebook: str) -> None:
    """Six frames with the same columns in the same order today is not a
    promise about tomorrow, and a positional union that drifted would put
    `item_id` into `heading` and fail no expectation at all."""
    code = "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )
    assert "unionByName" in code
    assert "unionAll" not in code
    assert ".union(" not in code


def test_the_citation_array_is_narrowed_by_name(notebook: str) -> None:
    """Silver's citation struct carries four fields and `gold_chunks.FIELDS` declares
    two. A struct handed back whole would be matched into the narrower type by
    position — right today by luck, and silently wrong the first time silver
    reorders it."""
    code = "\n".join(
        line for line in notebook.splitlines() if not line.startswith("# MAGIC")
    )
    assert '"harvested_at": one["harvested_at"]' in code
    assert '"source_url": one["source_url"]' in code


@pytest.mark.parametrize("path", [NOTEBOOK, VERIFY])
def test_a_markdown_cell_holds_no_code(path: Path) -> None:
    """Databricks reads a cell beginning `# MAGIC %md` as one markdown block:
    Python written below it in the same cell is rendered, not run."""
    for index, cell in enumerate(path.read_text().split("# COMMAND ----------")):
        lines = [line.rstrip() for line in cell.splitlines()]
        if not any(line.startswith("# MAGIC %md") for line in lines):
            continue
        code = [line for line in lines if line and not line.startswith(("#", "# MAGIC"))]
        assert not code, (
            f"{path.name} cell {index} is markdown and holds code: {code[:3]}"
        )


def test_the_verify_job_asserts_the_criteria_rather_than_reporting_them() -> None:
    """A notebook that prints its findings and exits zero proves nothing."""
    source = VERIFY.read_text()
    assert "raise AssertionError" in source
    assert "dbutils.notebook.exit" in source


def test_the_verify_job_prints_the_twenty_chunks_the_criterion_asks_for() -> None:
    """#35's third criterion is a person reading twenty chunks and deciding
    whether each is independently answerable. The job's part is to put the same
    twenty in front of them every time."""
    source = VERIFY.read_text()
    assert "SAMPLE" in source
    assert "20" in source


def test_the_notebook_reads_the_configuration_terraform_supplies(
    notebook: str,
) -> None:
    for key in ("chip_chat.catalog", "chip_chat.lib_path"):
        assert f'spark.conf.get("{key}")' in notebook


@pytest.fixture(scope="module")
def terraform() -> str:
    assert TERRAFORM.exists(), f"{TERRAFORM} is missing"
    return TERRAFORM.read_text()


def test_terraform_supplies_every_key_the_notebook_reads(
    terraform: str, notebook: str
) -> None:
    for key in ("chip_chat.catalog", "chip_chat.lib_path"):
        assert f'"{key}"' in terraform, f"{key} is read by the notebook and unset"
        assert f'spark.conf.get("{key}")' in notebook


def test_terraform_uploads_the_module_the_notebook_imports(terraform: str) -> None:
    """It is stdlib-only so that this upload is all the packaging needed."""
    assert "databricks/src/chip_chat/databricks/gold_chunks.py" in terraform


def test_the_pipeline_is_triggered_rather_than_continuous(terraform: str) -> None:
    """A continuous pipeline holds a cluster open, which is the cost trap #31
    exists to close."""
    assert "continuous  = false" in terraform or "continuous = false" in terraform


def test_the_pipeline_runs_single_node(terraform: str) -> None:
    """The budget is $150 a month and the corpus is a few hundred documents."""
    assert "num_workers  = 0" in terraform or "num_workers = 0" in terraform
    assert "SingleNode" in terraform


def test_gold_takes_no_checkpoint(terraform: str, notebook: str) -> None:
    """One materialized view, recomputed in full. Auto Loader's file ledger
    belongs to the layer that reads files."""
    assert "chip_chat.checkpoint_uri" not in notebook
    assert 'configuration = {\n    "chip_chat.checkpoint_uri"' not in terraform


def _iter_documentation() -> Iterator[Path]:
    yield REPO / "docs" / "corpus-chunking.md"


@pytest.mark.parametrize("path", list(_iter_documentation()))
def test_the_metadata_schema_is_documented(path: Path) -> None:
    """#35's first acceptance criterion says "fixed AND documented", and a
    schema documented only in a docstring is documented for people who already
    know where to look."""
    assert path.exists(), f"{path} is missing"
    text = path.read_text()
    for entry in gold_chunks.FIELDS:
        assert f"`{entry.name}`" in text, entry.name
