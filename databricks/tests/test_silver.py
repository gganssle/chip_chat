"""The silver declarations, checked against everything they have to agree with.

`chip_chat.databricks.silver` is a table of keys, casts, expectations and joins
that a pipeline consumes on a cluster nobody runs in CI, plus one HTML reader
that CI can run properly. So the assertions here come in two kinds.

The first kind is the same as `test_bronze.py`'s: the declaration says X, and
something else in this repository — bronze's own declarations, the catalogue
package's table list, the generator's ledger vocabulary, the Unity Catalog
layout, the Terraform, the notebooks — independently says X too.

The second kind is the one bronze had no need for. The boilerplate stripper and
the deduplication rule are *algorithms*, and an algorithm asserted against a
comment is an algorithm nobody has checked. So they are run here, over documents
written in this file, and the two claims #34 is graded on are made against a
known set:

`test_one_fact_on_three_pages_is_one_fact_with_three_citations` is criterion two.
`test_the_furniture_is_gone_and_the_prose_is_not` is criterion three.

The one thing these cannot check is the live corpus, and that is what
`databricks/notebooks/silver_verify.py` is for.
"""

from pathlib import Path

import pytest

from chip_chat.catalog.records import TABLES as CATALOGUE_TABLES
from chip_chat.data_gen import load_config
from chip_chat.data_gen.records import TABLES as SYNTHETIC_TABLES
from chip_chat.databricks import bronze, catalog, silver

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "databricks" / "notebooks" / "silver_conform.py"
VERIFY = REPO / "databricks" / "notebooks" / "silver_verify.py"
TERRAFORM = REPO / "infra" / "terraform" / "databricks_silver.tf"


# --- Agreement with the Unity Catalog layout --------------------------------


def test_the_streams_are_the_ones_unity_catalog_has() -> None:
    """`silver.STREAMS` is a copy, because silver.py may not import a sibling."""
    assert silver.STREAMS == catalog.STREAMS


def test_silver_is_a_layer_of_the_medallion() -> None:
    assert silver.LAYER in catalog.LAYERS


@pytest.mark.parametrize("stream", catalog.STREAMS)
def test_the_schema_name_is_the_one_terraform_created(stream: catalog.Stream) -> None:
    assert silver.schema_name(stream) == catalog.schema("silver", stream).name


def test_an_unknown_stream_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        silver.schema_name("real")


# --- Agreement with bronze --------------------------------------------------


def test_the_bronze_columns_it_reads_are_the_ones_bronze_writes() -> None:
    """Copies, for the reason the module docstring gives. A rename in bronze
    that was not made here would produce a silver update that fails on a column
    that does not exist — but only on the cluster, minutes in."""
    assert silver.INGESTED_AT == bronze.INGESTED_AT
    assert silver.SOURCE_PATH == bronze.SOURCE_PATH
    assert silver.QUARANTINED == bronze.QUARANTINED
    assert set(silver.DROPPED) == {
        bronze.SOURCE_PATH,
        bronze.SOURCE_MODIFIED_AT,
        bronze.SOURCE_SIZE_BYTES,
        bronze.RESCUED_DATA,
        bronze.QUARANTINED,
    }


def test_the_one_bronze_column_that_survives_is_the_one_still_true() -> None:
    """ "When did this row arrive" still has an honest answer in silver. "Which
    file did it come out of" does not — it changes every re-harvest without the
    fact changing at all."""
    assert bronze.INGESTED_AT not in silver.DROPPED
    assert silver.CONFORMED_AT not in silver.DROPPED


def test_every_conformed_table_reads_a_bronze_table_that_exists() -> None:
    for candidate in silver.TABLES:
        source = bronze.source(candidate.source)
        assert source.stream == candidate.stream or candidate.name in {
            "menu_items",
            "rewards",
        }


def test_silver_reads_bronze_and_never_the_landing_zone() -> None:
    """The whole layer, not most of it: that is what keeps "bronze is what
    arrived" a property rather than a slogan."""
    notebook = NOTEBOOK.read_text()
    assert "cloudFiles" not in notebook
    assert "readStream" not in notebook


def test_no_conformed_table_reads_a_source_bronze_reads_as_bytes() -> None:
    """Bytes are conformed by the corpus half, which decodes them first."""
    for candidate in silver.TABLES:
        assert bronze.source(candidate.source).is_parsed


def test_a_conformed_identity_is_a_key_bronze_also_recognises() -> None:
    """Bronze uses the identity to spot a file read twice and silver uses it to
    collapse one; the two disagreeing would mean one of them is wrong about what
    names a row."""
    for candidate in silver.TABLES:
        source = bronze.source(candidate.source)
        assert set(candidate.identity) == set(source.identity), candidate.name


# --- Agreement with the catalogue and the generator -------------------------


def test_the_catalogue_arrives_in_silver_whole() -> None:
    """Every table the catalogue publishes is conformed. One missing would be a
    reference the accounts cannot be resolved against."""
    conformed = {candidate.name for candidate in silver.tables_for("harvested")}
    assert set(CATALOGUE_TABLES) <= conformed


def test_the_generators_tables_all_arrive_in_silver() -> None:
    """A table added to data-gen/ and forgotten here would be landed and never
    conformed, and no expectation would ever run over it."""
    conformed = {
        candidate.name
        for candidate in silver.tables_for("synthetic")
        if candidate.name != "population_manifest"
    }
    assert conformed == set(SYNTHETIC_TABLES)


def test_the_ledger_vocabulary_is_the_generators_own() -> None:
    """The three-clause loyalty expectation is only decidable if these four
    strings are the four the generator writes."""
    loyalty = load_config().loyalty
    assert loyalty.seed_reason == silver.SEED_REASON
    assert loyalty.earn_reason == silver.EARN_REASON
    assert loyalty.redeem_reason == silver.REDEEM_REASON
    assert loyalty.expiry_reason == silver.EXPIRY_REASON
    assert set(silver.REASONS) == {
        loyalty.seed_reason,
        loyalty.earn_reason,
        loyalty.redeem_reason,
        loyalty.expiry_reason,
    }


def test_the_entree_derivations_are_the_catalogues_own() -> None:
    """The vocabulary expectation exempts exactly the two derivations whose
    terms the catalogue documents as carrying no `item_ids` — a vessel and a
    protein are each half of an entree. A third derivation added there and
    forgotten here would either fail every one of its terms or, if it were
    exempted by accident, stop checking the terms the expectation is about."""
    from chip_chat.catalog.records import Derivation

    assert set(silver.ENTREE_DERIVATIONS) == {
        Derivation.ITEM_TYPE.value,
        Derivation.PRIMARY_FILLING.value,
    }
    assert set(silver.ENTREE_DERIVATIONS) < {item.value for item in Derivation}


def test_the_vocabulary_expectation_exempts_only_the_entree_derivations() -> None:
    """`size(item_ids) > 0` alone fails four of the eight published terms."""
    vocabulary = next(
        candidate for candidate in silver.TABLES if candidate.name == "vocabulary"
    )
    constraint = next(
        expectation.constraint
        for expectation in vocabulary.expectations
        if "item_ids" in expectation.constraint
    )
    for derivation in silver.ENTREE_DERIVATIONS:
        assert f"'{derivation}'" in constraint
    assert "size(item_ids) > 0" in constraint


def test_every_published_reason_appears_in_the_expectation() -> None:
    """A reason the constraint does not name is a hole the check falls through:
    the row would satisfy none of the three clauses and fail, or — worse, if the
    clauses were written as a catch-all — satisfy it vacuously."""
    ledger = silver.table("loyalty_ledger")
    constraint = next(
        expectation.constraint
        for expectation in ledger.expectations
        if expectation.name == "references_a_real_order_or_a_real_reward"
    )
    for reason in silver.REASONS:
        assert f"'{reason}'" in constraint


# --- Deduplication ----------------------------------------------------------


def test_no_identity_is_a_display_name() -> None:
    """#34's warning, as an assertion.

    Two Chipotle items share a name across categories. A dedup partitioned on
    `name` would keep one of them and delete a menu item nobody removed, and the
    symptom would appear three layers away as an order item that stopped
    resolving.
    """
    for candidate in silver.TABLES:
        for column in candidate.identity:
            assert column not in {"name", "display_name", "label", "title"}, (
                candidate.name
            )


def test_the_menu_is_keyed_on_the_published_identifier() -> None:
    assert silver.table("menu_items").identity == ("item_id",)


def test_the_rewards_are_not_keyed_on_the_name_the_ledger_joins_on() -> None:
    """Keying on the name would let a duplicated name delete a reward instead of
    reporting the collision. The verify job reports it."""
    assert silver.table("rewards").identity == ("position",)
    ledger = silver.table("loyalty_ledger")
    reward = next(r for r in ledger.references if r.column == "reward_name")
    assert reward.key == "name"


def test_the_latest_arrival_wins_and_the_tie_is_broken() -> None:
    expression = silver.dedup_expression(silver.table("orders"))
    assert "PARTITION BY order_id" in expression
    assert f"{silver.INGESTED_AT} DESC" in expression
    assert f"{silver.SOURCE_PATH} DESC" in expression


def test_a_window_over_nothing_is_refused() -> None:
    """Partitioning by nothing would rank the whole table and keep exactly one
    row of it — a catastrophe that looks like a successful update."""
    with pytest.raises(ValueError, match="at least one identity column"):
        silver.latest_row()


def test_the_corpus_source_tables_are_deduplicated_the_same_way(
    notebook: str,
) -> None:
    """The pointers and the bodies are not declared in `silver.TABLES`, so they
    would otherwise be deduplicated a second way that happens to agree today —
    or not at all, which would double every citation after a re-ingest."""
    assert 'silver.latest_row("requested_url")' in notebook
    assert 'silver.latest_row("content_sha256")' in notebook
    assert 'silver.latest_row("content_sha256", "model_id", "api_version")' in notebook


def test_a_compound_key_partitions_on_every_column_of_it() -> None:
    expression = silver.dedup_expression(silver.table("order_items"))
    assert "PARTITION BY order_id, line_number" in expression


# --- The projection ---------------------------------------------------------


def test_the_projection_drops_what_describes_the_file_and_not_the_fact() -> None:
    clauses = silver.select_expressions(silver.table("orders"))
    star = clauses[0]
    for column in silver.DROPPED:
        assert column in star
    assert f"current_timestamp() AS {silver.CONFORMED_AT}" in clauses


def test_a_cast_column_is_excluded_from_the_star_before_it_is_re_added() -> None:
    """Otherwise the projection would name one column twice and Spark would
    refuse the plan."""
    candidate = silver.table("order_items")
    clauses = silver.select_expressions(candidate)
    for cast in candidate.casts:
        assert cast.column in clauses[0]
        assert f"CAST({cast.column} AS {silver.MONEY}) AS {cast.column}" in clauses


def test_money_is_exact_everywhere_it_is_cast() -> None:
    """A price is not a measurement. The harvest parses money out of the JSON
    token's own text to avoid binary-float noise, and a DOUBLE here would put it
    straight back."""
    for candidate in silver.TABLES:
        for cast in candidate.casts:
            assert cast.sql_type.startswith("DECIMAL"), (candidate.name, cast.column)


def test_the_money_columns_bronze_landed_as_strings_are_all_cast() -> None:
    """Bronze deliberately does not cast them, so silver has to — a price left
    as a string sorts lexicographically and compares wrong."""
    expected = {
        ("orders", "total"),
        ("order_items", "unit_price"),
        ("order_items", "line_total"),
        ("item_prices", "unit_price"),
        ("item_prices", "unit_delivery_price"),
        ("persona_fixtures", "lifetime_spend"),
    }
    cast = {
        (candidate.name, item.column)
        for candidate in silver.TABLES
        for item in candidate.casts
    }
    assert expected <= cast


# --- Expectations -----------------------------------------------------------


def test_every_expectation_has_a_name_that_says_what_is_true() -> None:
    """The event log reads `<name> failed`, so a name phrased as the fault reads
    as a double negative."""
    for candidate in silver.TABLES:
        for expectation in silver.expectations(candidate):
            assert expectation.name == expectation.name.lower()
            assert " " not in expectation.name
            assert expectation.why


def test_expectation_names_are_unique_within_a_table() -> None:
    """A duplicate would leave one of the two unreported in the event log."""
    for candidate in silver.TABLES:
        names = [expectation.name for expectation in silver.expectations(candidate)]
        assert len(names) == len(set(names)), candidate.name


def test_the_four_expectations_the_issue_names_are_all_here() -> None:
    """Every order item references a menu_items row; every loyalty entry
    references a real order or a real reward; no null demo_id on any
    visitor-scoped row; no corpus chunk without source_url and harvested_at."""
    order_items = {e.name for e in silver.expectations(silver.table("order_items"))}
    assert "item_id_resolves" in order_items

    ledger = {e.name for e in silver.expectations(silver.table("loyalty_ledger"))}
    assert "references_a_real_order_or_a_real_reward" in ledger

    for name in ("orders", "order_items", "loyalty_ledger", "demo_visitors"):
        expectations = {e.name for e in silver.expectations(silver.table(name))}
        assert "demo_id_is_present" in expectations, name

    for entry in silver.CORPUS:
        assert "carries_its_citation" in {e.name for e in entry.expectations}


def test_a_reference_is_proved_by_a_column_worth_having() -> None:
    """The carried column is one the serving layer wants anyway, so the check
    leaves a column behind rather than a receipt."""
    order_items = silver.table("order_items")
    items = next(r for r in order_items.references if r.table == "menu_items")
    assert ("name", "item_name") in items.carries
    assert items.expectation.constraint == "(item_name IS NOT NULL)"


def test_a_nullable_foreign_key_is_null_or_it_resolves() -> None:
    """`loyalty_ledger.order_id` is null for an opening balance, and a null
    foreign key is not a dangling one."""
    ledger = silver.table("loyalty_ledger")
    order = next(r for r in ledger.references if r.column == "order_id")
    assert order.optional
    assert "order_id IS NULL OR" in order.expectation.constraint


def test_every_reference_points_at_a_table_silver_actually_declares() -> None:
    declared = {(c.stream, c.name) for c in silver.TABLES}
    for candidate in silver.TABLES:
        for reference in candidate.references:
            assert (reference.stream, reference.table) in declared, candidate.name


def test_a_carried_column_never_lands_on_top_of_one_the_table_has() -> None:
    """A join that aliased a column onto a name the row already carries would
    produce two columns with one name, and Spark resolves that ambiguously."""
    for candidate in silver.TABLES:
        source = bronze.source(candidate.source)
        existing = {hint.split()[0] for hint in source.schema_hints}
        aliases = [
            alias for reference in candidate.references for _, alias in reference.carries
        ]
        assert len(aliases) == len(set(aliases)), candidate.name
        assert not (set(aliases) & existing), candidate.name


def test_a_carried_column_exists_on_the_table_it_is_carried_from() -> None:
    for candidate in silver.TABLES:
        for reference in candidate.references:
            target = bronze.source(silver.table(reference.table).source)
            available = {hint.split()[0] for hint in target.schema_hints}
            assert reference.key in available, reference.table
            for column, _ in reference.carries:
                assert column in available, (reference.table, column)


def test_a_required_column_is_one_bronze_pinned_a_type_for() -> None:
    """A column inferred rather than hinted could arrive as the wrong type on
    one run, and a null check on a column that is not there is an error rather
    than a finding."""
    for candidate in silver.TABLES:
        source = bronze.source(candidate.source)
        hinted = {hint.split()[0] for hint in source.schema_hints}
        carried = {
            alias for reference in candidate.references for _, alias in reference.carries
        }
        assert set(candidate.required) <= hinted | carried, candidate.name


# --- Boilerplate removal ----------------------------------------------------

_PAGE = """
<!doctype html>
<html><head><title>Chipotle</title><style>body {{ margin: 0 }}</style></head>
<body>
  <a class="skip-link" href="#main">Skip to main content</a>
  <div id="onetrust-consent-sdk">We use cookies to improve your experience.
    <button>Accept all</button></div>
  <header><nav>Order Now  Catering  Rewards  Locations</nav></header>
  <main>
    {body}
  </main>
  <footer role="contentinfo">
    <span class="sr-only">Opens in a new window</span>
    &copy; 2026 Chipotle Mexican Grill. All rights reserved.
  </footer>
</body></html>
"""

_STEAK = """
    <h1>Steak Burrito</h1>
    <p>A flour tortilla with steak, rice and beans.</p>
    <h2>Nutrition</h2>
    <p>Steak: 150 calories per 4oz serving.</p>
"""

_CHICKEN = """
    <h1>Chicken Burrito</h1>
    <p>A flour tortilla with chicken, rice and beans.</p>
    <h2>Nutrition</h2>
    <p>Steak: 150 calories per 4oz serving.</p>
"""


def test_the_furniture_is_gone_and_the_prose_is_not() -> None:
    """Criterion three, on a known page.

    Navigation, the cookie banner, the skip link, the footer and the
    screen-reader-only text are all gone; every published sentence is still
    here. The screen-reader text is the one worth watching: it is real English,
    invisible to a reader, repeated on every page, and the single best way to
    poison a chunk embedding with words no visitor ever saw.
    """
    blocks = silver.extract_blocks(_PAGE.format(body=_STEAK))
    text = "\n".join(f"{block.heading}\n{block.text}" for block in blocks)

    for furniture in (
        "Skip to main content",
        "We use cookies",
        "Accept all",
        "Order Now",
        "All rights reserved",
        "Opens in a new window",
        "margin: 0",
    ):
        assert furniture not in text, furniture

    for prose in (
        "Steak Burrito",
        "A flour tortilla with steak, rice and beans.",
        "Nutrition",
        "150 calories",
    ):
        assert prose in text, prose


def test_the_page_is_split_at_its_own_headings() -> None:
    """RFC-001 §08: chunking follows structure, not length."""
    blocks = silver.extract_blocks(_PAGE.format(body=_STEAK))
    assert [block.heading for block in blocks] == ["Steak Burrito", "Nutrition"]
    assert blocks[0].position == 0
    assert blocks[1].position == 1


def test_a_heading_is_not_also_the_first_line_of_its_own_block() -> None:
    """It would deduplicate as two facts saying the same thing."""
    blocks = silver.extract_blocks(_PAGE.format(body=_STEAK))
    assert "Steak Burrito" not in blocks[0].text


def test_a_list_keeps_one_item_per_line() -> None:
    body = "<h2>Allergens</h2><ul><li>Wheat</li><li>Milk</li><li>Soy</li></ul>"
    blocks = silver.extract_blocks(_PAGE.format(body=body))
    assert blocks[0].text.splitlines() == ["Wheat", "Milk", "Soy"]


def test_words_separated_only_by_markup_do_not_run_together() -> None:
    body = "<p><b>Steak</b> <i>Burrito</i> bowl</p>"
    blocks = silver.extract_blocks(_PAGE.format(body=body))
    assert blocks[0].text == "Steak Burrito bowl"


def test_a_page_with_no_main_falls_back_to_the_whole_body() -> None:
    """Not every published page marks its content, and a corpus that skipped
    those would be missing documents rather than furniture."""
    blocks = silver.extract_blocks(
        "<html><body><nav>Order Now</nav><h1>Catering</h1>"
        "<p>Feeds twenty.</p></body></html>"
    )
    assert [block.heading for block in blocks] == ["Catering"]
    assert blocks[0].text == "Feeds twenty."


def test_a_page_that_is_entirely_furniture_extracts_to_nothing() -> None:
    """A redirect stub, a consent interstitial, or a client-rendered shell
    fetched for the address of its script bundle. A corpus that keeps it keeps
    noise with a citation attached, so it is not a document: `_documents`
    excludes it, and `silver_verify` bounds those exclusions against
    `MAXIMUM_PROSELESS_SHARE` and prints every one of them by URL."""
    assert silver.extract_blocks(_PAGE.format(body="")) == ()


def test_a_void_element_does_not_swallow_the_rest_of_the_page() -> None:
    """HTMLParser reports `<img>` as a start tag and never as an end tag.

    Counting it against the nesting depth would leave the count one too high for
    everything after it, and the next `</div>` would close a skipped subtree
    that was still open — so a page with one image would extract its navigation
    and lose its prose. Real pages are full of images.
    """
    body = '<h1>Steak Burrito</h1><p><img src="steak.jpg" alt="">A burrito.</p>'
    blocks = silver.extract_blocks(_PAGE.format(body=body))
    assert [block.heading for block in blocks] == ["Steak Burrito"]
    assert blocks[0].text == "A burrito."


def test_a_pages_own_title_is_not_a_second_copy_of_its_heading() -> None:
    """It is the same sentence as the `<h1>` on most pages and a truncated
    version of it on the rest, so keeping it deduplicates as a second fact
    saying nearly the same thing."""
    blocks = silver.extract_blocks(_PAGE.format(body=_STEAK))
    assert all(block.heading != "Chipotle" for block in blocks)


def test_the_class_hints_are_specific_enough_to_be_safe() -> None:
    """A hint here promises that no content element in this corpus carries the
    substring. `banner` alone would break that promise — hero images hold real
    copy — which is why the list has `privacy-banner` instead."""
    assert "banner" not in silver.BOILERPLATE_CLASS_HINTS
    assert "privacy-banner" in silver.BOILERPLATE_CLASS_HINTS
    for hint in silver.BOILERPLATE_CLASS_HINTS:
        assert hint == hint.lower()


# --- Deduplication of facts -------------------------------------------------


def test_one_fact_on_three_pages_is_one_fact_with_three_citations() -> None:
    """Criterion two, on a known set.

    Two different item pages publish the same nutrition sentence. The sentence
    is one fact; the two pages are two citations for it. Everything else about
    the two pages stays distinct, which is the half of the criterion that says
    "without losing any distinct fact".
    """
    steak = silver.extract_blocks(_PAGE.format(body=_STEAK))
    chicken = silver.extract_blocks(_PAGE.format(body=_CHICKEN))

    facts: dict[str, list[str]] = {}
    for page, blocks in (("steak", steak), ("chicken", chicken)):
        for block in blocks:
            facts.setdefault(block_key(block), []).append(page)

    shared = [pages for pages in facts.values() if len(pages) > 1]
    assert len(shared) == 1
    assert sorted(shared[0]) == ["chicken", "steak"]

    # Four block occurrences across the two pages, three distinct facts, and
    # every occurrence still accounted for as a citation.
    assert len(steak) + len(chicken) == 4
    assert len(facts) == 3
    assert sum(len(pages) for pages in facts.values()) == 4


def block_key(block: silver.Block) -> str:
    return silver.block_digest(block.heading, block.text)


def test_two_pages_differing_only_in_furniture_are_one_document() -> None:
    """Bronze content-addresses the bytes, which is the right identity for a
    landing zone and the wrong one for a corpus."""
    first = silver.extract_blocks(_PAGE.format(body=_STEAK))
    second = silver.extract_blocks(
        _PAGE.format(body=_STEAK).replace(
            "<nav>Order Now  Catering  Rewards  Locations</nav>",
            "<nav>Order  Catering  Rewards  Find a Chipotle  Gift Cards</nav>",
        )
    )
    assert silver.text_digest(first) == silver.text_digest(second)


def test_two_pages_differing_in_a_published_word_are_two_documents() -> None:
    steak = silver.extract_blocks(_PAGE.format(body=_STEAK))
    chicken = silver.extract_blocks(_PAGE.format(body=_CHICKEN))
    assert silver.text_digest(steak) != silver.text_digest(chicken)


def test_whitespace_is_normalised_and_case_is_not() -> None:
    """Whitespace carries no meaning here; case sometimes does, and folding it
    is the kind of normalisation that eventually merges two proper nouns."""
    assert silver.normalise("Steak\xa0 Burrito\n") == "Steak Burrito"
    assert silver.block_digest(None, "Steak  Burrito") == silver.block_digest(
        None, "Steak\nBurrito"
    )
    assert silver.block_digest(None, "Steak") != silver.block_digest(None, "steak")


def test_the_same_sentence_under_two_headings_is_two_facts() -> None:
    """It is being said about two different things, and merging them would
    produce a fact whose citations were not making the same claim."""
    assert silver.block_digest("Allergens", "Contains wheat.") != silver.block_digest(
        "Nutrition", "Contains wheat."
    )


# --- The PDFs ---------------------------------------------------------------

_ANALYSIS = """
{
  "modelId": "prebuilt-layout",
  "apiVersion": "2024-11-30",
  "paragraphs": [
    {"content": "Nutrition information is calculated from supplier data."},
    {"content": ""}
  ],
  "tables": [
    {
      "rowCount": 3,
      "columnCount": 3,
      "caption": {"content": "Calories by item"},
      "boundingRegions": [{"pageNumber": 2}],
      "cells": [
        {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader", "content": "Item"},
        {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader",
         "content": "Total Fat", "columnSpan": 2},
        {"rowIndex": 1, "columnIndex": 0, "content": "Steak"},
        {"rowIndex": 1, "columnIndex": 1, "content": "6g"},
        {"rowIndex": 1, "columnIndex": 2, "content": "9%"},
        {"rowIndex": 2, "columnIndex": 0, "content": "Chicken"},
        {"rowIndex": 2, "columnIndex": 1, "content": "7g"},
        {"rowIndex": 2, "columnIndex": 2, "content": "11%"}
      ]
    }
  ]
}
"""


def test_a_table_row_arrives_whole_or_not_at_all() -> None:
    rows = silver.analysis_table_rows(_ANALYSIS)
    assert [row["cells"] for row in rows] == [
        ["Steak", "6g", "9%"],
        ["Chicken", "7g", "11%"],
    ]


def test_every_figure_knows_which_column_it_is_under() -> None:
    """A merged heading names every column it covers, because a figure under the
    right-hand half of a merged "Total Fat" heading is still a total fat
    figure."""
    rows = silver.analysis_table_rows(_ANALYSIS)
    assert rows[0]["column_headers"] == ["Item", "Total Fat", "Total Fat"]
    assert len(rows[0]["column_headers"]) == len(rows[0]["cells"])


def test_a_header_row_is_not_a_row() -> None:
    """It is carried on every row of its table, which is where it is useful."""
    rows = silver.analysis_table_rows(_ANALYSIS)
    assert all(row["row_index"] > 0 for row in rows)


def test_a_row_keeps_its_caption_and_its_page() -> None:
    rows = silver.analysis_table_rows(_ANALYSIS)
    assert rows[0]["caption"] == "Calories by item"
    assert rows[0]["page_number"] == 2


def test_the_prose_around_a_table_is_kept_and_the_empty_paragraphs_are_not() -> None:
    """The footnote saying the chart does not reflect cross-contact matters as
    much as the chart."""
    assert silver.analysis_paragraphs(_ANALYSIS) == (
        "Nutrition information is calculated from supplier data.",
    )


def test_a_cell_with_no_position_stops_the_extraction() -> None:
    """A number with no column to belong to is not a case to paper over."""
    broken = (
        '{"tables": [{"rowCount": 1, "columnCount": 1, "cells": [{"content": "6g"}]}]}'
    )
    with pytest.raises(ValueError, match="no position"):
        silver.analysis_table_rows(broken)


def test_something_that_is_not_an_analyze_result_is_refused() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        silver.analysis_paragraphs("[]")


# --- Lookups ----------------------------------------------------------------


def test_table_lookup_refuses_a_name_nothing_conforms() -> None:
    with pytest.raises(KeyError, match="no silver table is called"):
        silver.table("catering_packages")


def test_corpus_lookup_refuses_a_name_nothing_builds() -> None:
    with pytest.raises(KeyError, match="no silver corpus table is called"):
        silver.corpus("chunks")


def test_tables_for_refuses_an_unknown_stream() -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        list(silver.tables_for("harvestd"))


def test_no_two_tables_share_a_name_within_a_schema() -> None:
    for stream in silver.STREAMS:
        names = [candidate.name for candidate in silver.tables_for(stream)]
        names += [entry.name for entry in silver.CORPUS if entry.stream == stream]
        assert len(names) == len(set(names))


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
    """The issue asks for violations to fail the pipeline. `expect` records a
    warning nobody reads and `expect_or_drop` loses the row, and a check that
    can be satisfied by deleting the evidence is not a check."""
    assert "dp.expect_all_or_fail" in notebook
    assert "dp.expect_or_drop" not in notebook
    assert "dp.expect_all(" not in notebook


def test_a_quarantined_bronze_row_never_enters_silver(notebook: str) -> None:
    assert "NOT {silver.QUARANTINED}" in notebook


def test_the_referential_join_is_a_left_join(notebook: str) -> None:
    """An inner join would DROP the violating row and quietly satisfy the
    expectation it exists to test — the easiest way to write a check that can
    never fail."""
    assert '"left",' in notebook
    assert '"inner"' in notebook  # the corpus joins, which are not checks


@pytest.mark.parametrize("path", [NOTEBOOK, VERIFY])
def test_a_markdown_cell_holds_no_code(path: Path) -> None:
    """Databricks reads a cell beginning `# MAGIC %md` as one markdown block:
    Python written below it in the same cell is rendered, not run. Nothing
    errors — the pipeline simply defines no tables and the update fails with
    `NO_TABLES_IN_PIPELINE`, which reads like the decorators are wrong."""
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


def test_the_verify_job_checks_the_dedup_did_not_shrink_the_catalogue() -> None:
    """#34's bluntest sentence, checked against the live tables rather than
    only against the fixtures above."""
    source = VERIFY.read_text()
    assert "distinct item_ids in bronze" in source


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
    assert "databricks/src/chip_chat/databricks/silver.py" in terraform


def test_the_pipeline_is_triggered_rather_than_continuous(terraform: str) -> None:
    """A continuous pipeline holds a cluster open, which is the cost trap."""
    assert "continuous  = false" in terraform or "continuous = false" in terraform


def test_silver_takes_no_checkpoint(terraform: str, notebook: str) -> None:
    """Materialized views recompute in full; Auto Loader's file ledger belongs
    to the layer that reads files. A checkpoint here would be a value nothing
    reads, which is worse than no value at all."""
    assert "chip_chat.checkpoint_uri" not in notebook
    assert 'configuration = {\n    "chip_chat.checkpoint_uri"' not in terraform
