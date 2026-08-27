# Corpus chunking

Where a fact ends. What a chunk carries, what it cites, and why there is no
window size anywhere in this layer. Issue
[#35](https://github.com/gganssle/chip_chat/issues/35).

Everything below is `infra/terraform/databricks_gold_chunk.tf`,
`databricks/notebooks/gold_chunk.py`,
`databricks/notebooks/gold_chunk_verify.py` and
`databricks/src/chip_chat/databricks/gold_chunks.py`. Nothing was made by hand
in the workspace UI.

**Gold is two things and they are two modules.**
`chip_chat.databricks.gold` is [#36](https://github.com/gganssle/chip_chat/issues/36)'s
four personalization marts, computed over the *synthetic* stream into
`gold_synthetic`; `chip_chat.databricks.gold_chunks` is this issue's corpus
chunks, rendered from the *harvested* stream into `gold_harvested`. They share a
medallion layer, a lib directory on the driver, and nothing else — different
schema, different pipeline, different verify job, and no update in which one can
fail the other. The split is not tidiness. A single module and a single pipeline
named for the layer would have one `schema` default and one `STREAM` spanning
both, and the thing it would span is the boundary RFC-001 §04 is least willing
to see blurred: an invented order that reached the retrieval index would be a
fabricated fact with a real-looking citation on it. See §8.

> **Status.** Run. `chip-chat-gold-chunk` completed against `dbw-chip-chat` on
> 2026-08-27 and `chip-chat-gold-chunk-verify` returned SUCCESS over it, which
> is what §7 asks for and the whole of the live half. `make ci` is green over
> the rest — including the chunker itself, which is run over the recorded
> nutrition sheet and the recorded catalogue rather than described, and
> including the fixed-window chunker in `test_gold_chunks.py` that the same
> assertions are run over and required to **fail**. §6 is the hand review the
> third acceptance criterion asks for; §6.1 reconciles it against the corpus
> the live run actually produced.
>
> The first update failed, and the failure is worth keeping. `gold_chunk.py`
> closed a UDF over an imported module, which is the trap `silver_conform.py`
> documents at length one layer down and dated the day before this ran:
> cloudpickle serializes a module global by name, the Python worker cannot
> satisfy the import, and the flow dies at the first row with
> `ModuleNotFoundError: No module named 'gold_chunks'` inside a
> `SerializationError` — after the graph has validated. The note did not travel
> from silver to this file when the chunker was written, because there was
> nothing to run it against. `lib()` in `gold_chunk.py` is silver's, moved up a
> layer.
>
> **This landed after #36 and after #48.** An earlier attempt at this issue put
> the chunk renderers in a module called `gold.py`, which #36's marts had
> already taken; it never merged. What is here is that work rebased onto the
> landed marts under the rule the merge queue settled the collision by — main
> wins on shared ground, the slice keeps what it uniquely introduces — so
> nothing in `gold.py`, `gold_marts.py`, `gold_verify.py` or
> `databricks_gold.tf` is touched, and the chunk half arrives under its own
> names. #48's `chip_chat.search.chunks` was written against this schema while
> it was still unlanded and mirrors it field for field; the contract test that
> holds the two together is live as of this commit rather than skipped. See §9.

## 1. The one decision

RFC-001 §08 does not leave this open:

> Chunking follows structure, not length. One menu item is one chunk, carrying
> its nutrition and allergen fields as metadata. Policy and FAQ documents chunk
> by section. Fixed-window chunking splits nutrition tables across boundaries
> and produces exactly the confident wrong answers that allergen questions
> cannot tolerate.

So `chip_chat.databricks.gold_chunks` has no window size, no overlap, no target length
and no truncation, and there is deliberately nowhere for one to go: every
renderer takes **one whole structural unit** and returns one chunk. Six silver
tables, six kinds of chunk, and every boundary is one somebody else drew.

```
chip_chat.silver_harvested                       chip_chat.gold_harvested
├── menu_items ──────── one item, one chunk ───►
├── policy_sections ─── the page's own heading ─►
├── faq_entries ─────── the FAQ's own question ─┼─► corpus_chunks
├── caveats ─────────── the paragraph as published
├── document_blocks ─── the document's own heading
└── document_tables ─── the table's own row ────►
```

| Kind | Silver table | The boundary | Why it is its own kind |
| --- | --- | --- | --- |
| `MENU_ITEM` | `menu_items` | the menu's item | the commonest question the product answers is about one item |
| `POLICY_SECTION` | `policy_sections` | the page's heading | "what are the rules" should retrieve the contract, not the page explaining it |
| `FAQ_ENTRY` | `faq_entries` | the published question | an answer without its question is an answer to something the reader has to guess |
| `ALLERGEN_CAVEAT` | `caveats` | the paragraph | see §4 — it is not a footer on every item chunk, on purpose |
| `DOCUMENT_BLOCK` | `document_blocks` | the document's heading | silver already split it and already conserved its citations |
| `NUTRITION_ROW` | `document_tables` | the table's row | see §3 — this is the one the issue is really about |

**Gold reads silver and never bronze.** A chunk built from bronze would be a
chunk of a row silver had quarantined, deduplicated away or failed an
expectation on — a retrievable sentence this lakehouse has already decided is
not true.

**Gold reads the harvested stream and never the synthetic one.** RFC-001 §04
holds the real catalogue and the invented account data apart, and the retrieval
index is where blurring them would cost the most: a generated order that reached
the index would be a fabricated fact with a real-looking citation on it.
`gold_chunks.STREAM` is a constant rather than a loop variable, so there is no code
path that could take one there. `test_the_pipeline_never_reads_the_synthetic_stream`
asserts the notebook has no `catalog.STREAMS` in it.

## 2. The metadata schema, fixed

`gold_chunks.FIELDS` is the schema, and it is a tuple of records rather than a
`CREATE TABLE` in a notebook. The pipeline builds its struct from it, the
`Chunk` dataclass is asserted field-for-field against it, and
[#48](https://github.com/gganssle/chip_chat/issues/48) builds the AI Search
index schema from it. One copy.

**R** is retrievable — the index returns it on a hit. **F** is filterable — a
query may constrain on it. **Fa** is facetable — the index may count it.

| Field | Type | Kinds | R | F | Fa | What it is for |
| --- | --- | --- | :-: | :-: | :-: | --- |
| `chunk_id` | `STRING` | every chunk | ● | ● | · | the identifier a response envelope cites; stable across a rebuild because it names what the chunk is about rather than what it says |
| `kind` | `STRING` | every chunk | ● | ● | ● | which published structure this is one of; the retriever weights on it |
| `text` | `STRING` | every chunk | ● | · | · | what integrated vectorization embeds and what a person reads back |
| `heading` | `STRING` | every chunk | ● | · | · | the published heading this chunk sits under, which is also what keyword recall matches on for item names |
| `item_id` | `STRING` | `MENU_ITEM` | ● | ● | · | the item this chunk is about, for joining a retrieved chunk to the menu |
| `category` | `STRING` | `MENU_ITEM` | ● | ● | ● | Entree, Side, Drink or null for a component; a facet the UI can offer |
| `item_type` | `STRING` | `MENU_ITEM` | ● | ● | ● | Burrito, Bowl, Chips; the finer published type the vessel vocabulary uses |
| `primary_filling` | `STRING` | `MENU_ITEM` | ● | ● | ● | the protein an entree is built around; half of how a described meal resolves |
| `allergens` | `ARRAY<STRING>` | `MENU_ITEM` | ● | ● | ● | the codes published as CONTAINS; "without dairy" is this filter |
| `allergen_disclosure` | `STRING` | `MENU_ITEM` | ● | ● | · | whether anything is published for this item at all; without it an empty `allergens` array means two different things and reads as one |
| `calories` | `DECIMAL(8,2)` | `MENU_ITEM` | ● | ● | · | the published figure, exact; "fewer calories" is a filter on this |
| `is_composed` | `BOOLEAN` | `MENU_ITEM` | ● | ● | · | whether `calories` is the whole meal or one component of it; a comparison that ignored it would rank a burrito's tortilla against a bowl |
| `document_id` | `STRING` | `POLICY_SECTION` | ● | ● | · | which policy document this section belongs to |
| `document_kind` | `STRING` | `POLICY_SECTION` | ● | ● | ● | `TERMS` or `OVERVIEW`, so retrieval can prefer the contract over the page explaining it |
| `position` | `INT` | `POLICY_SECTION`, `ALLERGEN_CAVEAT`, `FAQ_ENTRY` | ● | · | · | where this unit falls in its document, in the order the source published |
| `column_headers` | `ARRAY<STRING>` | `NUTRITION_ROW` | ● | · | · | one heading per cell of the row this chunk is; the structure that makes the row's numbers mean anything, kept beside the prose rather than only inside it |
| `cells` | `ARRAY<STRING>` | `NUTRITION_ROW` | ● | · | · | the row's own values, positionally aligned with `column_headers` |
| `page_number` | `INT` | `NUTRITION_ROW` | ● | · | · | which page of the PDF the row was read from, for a citation somebody can check |
| `source_url` | `STRING` | every chunk | ● | ● | · | RFC-001 §08: citations are part of the payload, not reconstructed after |
| `harvested_at` | `TIMESTAMP` | every chunk | ● | ● | · | how old the answer is, which for an allergen claim renders without interaction because published allergen data goes stale |
| `citations` | `ARRAY<STRUCT<harvested_at, source_url>>` | `DOCUMENT_BLOCK` | ● | · | · | every source that published this text, where silver deduplicated several into one; the row's own `source_url` is the most recent |
| `character_count` | `INT` | every chunk | ● | · | · | reported so an over-long published section is visible; never split on |
| `chunked_at` | `TIMESTAMP` | every chunk | ● | · | · | when the gold update ran; the fourth clock, and not one of the other three |

**Null is a documented shape, not something you learn from a query.** The
`Kinds` column is on the `Field` record itself, so "`item_id` is null on a
`NUTRITION_ROW`" is a statement in the schema.

**The filterable set is chosen, not exhaustive.** #48's scope names the three
questions the product must answer — *fewer calories*, *vegetarian*, *without
dairy* — and each of them is a constraint over a typed column here or it is a
language model reading numbers out of prose. The second is how a calorie
comparison comes back wrong. Every filterable field costs index size and build
time, which is why `text`, `citations` and the two clocks that are not
`harvested_at` are not among them.

**`chunk_id` names what the chunk is about, not what it says.**
`MENU_ITEM:CMG-101` is the Chicken Bowl's chunk this week and next week, through
a re-harvest that changes its calorie figure. Content-addressing is the habit
everywhere else in this repository — bronze addresses the bytes, silver
addresses the prose — but both of those answer a landing-zone question, *have I
seen this before*. A chunk id answers RFC-001 D9's question instead: the response
envelope cites ids the `retriever.search` span returned on that turn, and #48
rebuilds the index rather than patching it. Under content-addressing every
weekly rebuild would retire every id whose page changed a word, and a
conversation two turns old would be citing chunks that no longer exist. The
wording moving under a stable id is the correct behaviour: the citation points
at the guacamole chunk, and the guacamole chunk now says 230 calories rather
than 240.

## 3. The nutrition table, and what a window does to it

This is the part the issue exists for. `docs/decisions/pdf-tables.md` already
walked through the failure and this is where it is prevented.

A window ends after `Cheese | 1 oz | 110 | 8`. The next begins
`| 5 | 260 | Guacamole | 4 oz | 230`. Retrieval scores the second highly for a
question about guacamole and returns it. It contains **260** — Cheese's sodium —
sitting immediately before the word Guacamole, under no heading at all. Every
ingredient of a confident wrong answer is present, and nothing downstream can
detect it, because by then there is no column left to check against.

`gold_chunks.nutrition_row_chunk` cannot produce that chunk. Not because it is careful
about boundaries, but because it never sees one: it is handed a row and its
headings, which is what `silver.analysis_table_rows` guarantees it, and it
renders every cell against the heading the table published it under.

```
Item: Guacamole, Serving: 4 oz, Total Calories: 230, Total Fat (g): 22,
Saturated Fat (g): 3.5, Sodium (mg): 370. Published figures, read from page 1
of the source document.
```

The headings also ride on the chunk as `column_headers` and `cells`, positionally
aligned. That redundancy is deliberate: the prose is what gets embedded and
matched, and the arrays are what an answer quotes when it needs the figure
rather than the sentence — and what the test checks.

A hole — a position the service reported no cell for — renders as `not
published` rather than being skipped. Skipping it would shift every heading
after it onto the wrong number, which is the same failure arriving by a
different route.

### The test, and the test of the test

The issue asks for two tests and adds the condition that makes them worth
having: *both tests above pass, and would fail if fixed-window chunking were
substituted*. So `databricks/tests/test_gold_chunks.py` holds a real fixed-window
chunker — a window, an optional overlap, no notion of what the text is made of —
and runs the same assertion over both.

`_rows_kept_whole` is that assertion, written once. A row survived when exactly
one chunk mentions its item **and that chunk carries every published figure of
the row together with the heading that figure is under**. The second half is
what a boundary destroys: a number separated from its column is not a weaker
fact, it is a different one.

| Test | Chunker | Verdict |
| --- | --- | --- |
| `test_no_nutrition_table_is_split_across_a_chunk_boundary` | `gold_chunks.nutrition_row_chunk` | every row whole |
| `test_fixed_window_chunking_splits_a_published_nutrition_row` | `_fixed_window`, nineteen sizes × two overlaps | at least one row lost, every time |
| `test_fixed_window_chunking_strands_figures_under_no_heading` | `_fixed_window` | a window carrying figures and no heading |

The fixed-window chunker lives in the test file rather than in the module on
purpose. A module that shipped one "for hard documents" would eventually have
it used on a hard document, and the hard documents are the nutrition sheets.

The document is `harvest/tests/fixtures/chipotle/nutrition-sheet-layout.json` —
the recorded Document Intelligence reading the harvest tests already maintain —
read through `silver.analysis_table_rows` exactly as the pipeline reads it, and
windowed over the `content` string the same service returns beside the
structure. Taking that string is the whole of the mistake.

Window sizes large enough to swallow the recorded 443-character sheet whole are
skipped, and the test says how many it skipped and fails if too few remain. A
window that holds the entire document has not chunked it; the real sheets this
one stands in for are dozens of pages.

## 4. Allergens: three states, and why the caveats are chunks

The allergen fields carry Chipotle's own three states and never a boolean. This
is `docs/decisions/allergen-absence.md`'s argument arriving at the retrieval
layer, and it survives here in two places:

- `allergens` holds the codes published as `CONTAINS`, and `allergen_disclosure`
  says whether anything is published for the item at all. A chunk carrying only
  the array would have merged the two silences — *marks are published and none
  is dairy* and *nothing is published about this item* — and the second read as
  the first is a wrong allergen answer given to a stranger on the open internet.
- The prose says which. `test_the_two_allergen_silences_do_not_read_the_same`
  asserts the two chunks differ in the text as well as in the column, because
  the text is what a model reads.

**The caveats are chunks of their own and not a footer appended to every menu
chunk.** That is the arguable call and it is deliberate. Appended, Chipotle's
cross-contact caveat would sit inside hundreds of item chunks, dominate their
embeddings, and be retrieved for questions that are not about allergens at all.
As chunks, the agent has to retrieve them — which is what the allergen path is
for and what the eval set measures. The cost is that an agent which fails to
retrieve them answers without them, and that failure is visible to an
evaluation in a way a diluted embedding would not be.

## 5. Length is reported and never acted on

`gold_chunks.EMBEDDING_CHARACTER_BUDGET` is the one number in this layer and it splits
nothing. `gold_chunk_verify.py` reports the chunks over it and the pipeline does not
look at it at all.

The distinction is the whole issue in one constant. **A length that splits is a
fixed window with better manners.** A length that reports leaves the boundary
where the publisher put it and tells a person that the publisher put it
somewhere awkward — a policy section of twenty thousand characters is a page
whose author stopped using headings, which is an argument for asking the harvest
for a finer published boundary and never for inventing one here.

`test_the_chunker_holds_no_window_no_overlap_and_no_truncation` parses the module,
blanks every string literal and docstring, and asserts that the remaining code
contains no `window`, no `overlap`, no `chunk_size`, no `textwrap`, and exactly
one mention of the budget: its own declaration.

## 6. Twenty chunks, read by hand

The third acceptance criterion: *a sample of 20 chunks reviewed by hand for
whether each is independently answerable*. The question is whether somebody
handed only this text, and told where it came from, could answer the question it
is about without needing the chunk either side of it.

> **What this is a review of.** These twenty are rendered by the shipped
> renderers over `catalog/tests/fixtures/catalog/` and
> `harvest/tests/fixtures/chipotle/nutrition-sheet-layout.json` — recordings of
> the real endpoints, trimmed. They are real published text and real published
> figures. They were **not** the live corpus when this was written, because
> there was not one. There is now, and §6.1 puts the two side by side: the
> three kinds sampled here are the same size live, chunk for chunk, because the
> fixtures are recordings of these endpoints rather than a reduction of them.

### `MENU_ITEM` — ten of ten

| # | Chunk | Independently answerable? |
| --- | --- | --- |
| 1 | `Guacamole. Type: Toppings. Only served as part of another item. 230 calories. Chipotle publishes allergen marks for this item and marks none of them. That is not a statement that the item is free of them; see the published allergen caveats.` | **Yes.** Name, calories, allergen state and the honest qualifier, all in 240 characters. |
| 2 | `Chips. Type: Chips. Category: Side. 540 calories. …` | **Yes.** |
| 3 | `Chicken Bowl. Type: Bowl. Built around Chicken. Category: Entree. 180 calories for this component alone; a full order adds the calories of whatever is chosen in it. …` | **Yes**, and this is the sentence that stops a wrong answer: 180 is the chicken, not the meal. |
| 4 | `Extra Chicken. Type: ExtraPortion. Only served as part of another item. 360 calories. …` | **Yes**, though `ExtraPortion` is the menu's own camel-cased label. Carried verbatim rather than prettified: see the finding below. |
| 5 | `Steak Burrito. Type: Burrito. Built around Steak. Category: Entree. 150 calories for this component alone; …` | **Yes.** |
| 6 | `Jarritos Guava. Type: Jarritos. Category: Drink. 110 calories. …` | **Yes.** |
| 7 | `White Rice. Type: Rice. Only served as part of another item. Every morning, we make our cilantro-lime white rice from scratch … 210 calories. …` | **Yes.** 616 characters, the longest menu chunk, all of it Chipotle's own published copy. |
| 8 | `Black Beans. Type: Beans. Only served as part of another item. Condor, Blackhawk, Domino, Valentine, and Black Magic. … 130 calories. …` | **Yes.** |
| 9 | `Cheese. Type: Toppings. Only served as part of another item. 110 calories. Marked as containing: Dairy.` | **Yes**, and it is the shortest at 103 characters. The one that answers "does the cheese have dairy". |
| 10 | `Napkins & Utensils. Type: Meal Hardware. Category: Non Food Items. Chipotle publishes no calorie figure for this item. Chipotle publishes no allergen information for this item.` | **Yes**, and it is the one worth having in the sample: both nulls say what they are instead of being absent. |

### `ALLERGEN_CAVEAT` — three of five

| # | Chunk | Independently answerable? |
| --- | --- | --- |
| 11 | `No matter what your unique dietary needs are, Chipotle has options for you. Unless you have an allergy to delicious food, in which case, we might have an issue.` | **No**, and correctly so. It is published marketing prose with no fact in it. It carries a citation and will rank below the real caveats for any allergen question, because it contains none of their words. Left in rather than filtered: a rule that dropped "unhelpful" published text is a rule that would eventually drop a caveat. |
| 12 | `* Wheat & Gluten categories are combined since all Chipotle gluten-containing items contain wheat. ** All sulphites present in Chipotle food items come exclusively from vinegar … 1. FOR THOSE WHO AVOID PORK: Our carnitas are made with pork.` | **Yes**, though it is three footnotes in one published paragraph. The boundary is Chipotle's; splitting it here would be inventing one. |
| 13 | `Individual foods may come into contact with one another during preparation, which is not reflected on this chart. Although we do not use eggs, mustard, peanuts, tree nuts, sesame, shellfish, or fish as ingredients in our food, Chipotle cannot guarantee the complete absence of these allergens in its restaurants. …` | **Yes.** The safety sentence PRD K3 turns on. If it is not retrievable it is not said. |

### `NUTRITION_ROW` — seven of seven

| # | Chunk | Independently answerable? |
| --- | --- | --- |
| 14 | `Item: Guacamole, Serving: 4 oz, Total Calories: 230, Total Fat (g): 22, Saturated Fat (g): 3.5, Sodium (mg): 370. Published figures, read from page 1 of the source document.` | **Yes.** Every figure against its heading, the serving stated, the page cited. |
| 15 | `Item: Chips, Serving: 4 oz, Total Calories: 540, …` | **Yes.** |
| 16 | `Item: Chicken Bowl, Serving: 4 oz, Total Calories: 180, …` | **Yes.** |
| 17 | `Item: Steak Burrito, Serving: 4 oz, Total Calories: 150, …` | **Yes.** |
| 18 | `Item: White Rice, Serving: 4 oz, Total Calories: 210, Total Fat (g): 4, Saturated Fat (g): 0.5, Sodium (mg): 350. …` | **Yes**, and it is the one to check against a windowed chunker: `210` is also Black Beans' sodium, and in a window carrying neither heading the two are the same number. |
| 19 | `Item: Black Beans, Serving: 4 oz, Total Calories: 130, Total Fat (g): 1.5, Saturated Fat (g): 0, Sodium (mg): 210. …` | **Yes.** `Saturated Fat (g): 0` is a published zero and reads as one. |
| 20 | `Item: Cheese, Serving: 1 oz, Total Calories: 110, Total Fat (g): 8, Saturated Fat (g): 5, Sodium (mg): 260. …` | **Yes**, and the serving is `1 oz` where every other row is `4 oz` — which is exactly the fact a flattened chunk loses first. |

### What the review changed

Two findings, both fixed in this issue, both now tests:

1. **`A Toppings.` → `Type: Toppings.`** The first draft put an article in front
   of `item_type`, and several of the menu's own labels are plural or
   camel-cased — `Toppings`, `ExtraPortion`, `Meal Hardware`. "A Toppings" is a
   sentence no publisher wrote and no reader would. The label is still carried
   verbatim; only the frame around it changed.
2. **A heading stated twice.** Chipotle's `GLUTEN INTOLERANCE & CELIAC DISEASE`
   caveat carries its own heading as the first line of its text, and the first
   draft prepended it anyway. That doubled the weight of five words in the
   embedding and read as a stutter in an answer about celiac disease.
   `gold_chunks._headed` now skips the prefix when the text already opens with it, and
   `test_a_heading_the_text_already_opens_with_is_not_said_twice` holds it there.

### What the review did not settle

- **A `NUTRITION_ROW` carries no `item_id`.** It says `Item: Guacamole` and
  nothing joins that to `CMG-1001`. That is deliberate for now:
  `docs/decisions/pdf-tables.md` records that the sheet's figures are *evidence
  about* the calculator's data rather than a second copy of it, and resolving
  the label to an item is the reconciliation that decision put in
  `pdf_nutrition_findings`. What is unsettled is what the **agent** should do
  when both a `MENU_ITEM` chunk and a `NUTRITION_ROW` chunk come back for one
  question, and that belongs to the retrieval and agent issues rather than here.
- **`heading` is null on every `NUTRITION_ROW` in this sample**, because the
  fixture sheet publishes no table caption. A real sheet that captions its
  tables will fill it.
- **The live corpus.** Twenty fixture chunks are twenty real sentences and are
  not the two hundred documents the weekly harvest will produce.

### 6.1 The same twenty, against the corpus that was built

The caveat above says this section gets redone when the pipeline runs. It ran,
and the honest answer is that there was almost nothing to redo, for a reason
worth writing down rather than celebrating.

`chip-chat-gold-chunk-verify` counted the published corpus at **51 chunks**:

| Kind | Live | In §6's review |
| --- | --- | --- |
| `MENU_ITEM` | 10 | 10 of 10 |
| `NUTRITION_ROW` | 7 | 7 of 7 |
| `ALLERGEN_CAVEAT` | 5 | 3 of 5 |
| `POLICY_SECTION` | 10 | — |
| `FAQ_ENTRY` | 11 | — |
| `DOCUMENT_BLOCK` | 8 | — |

The three kinds §6 sampled are the same size live as they were in the fixtures,
because the fixtures **are** recordings of these endpoints rather than a
reduction of them: ten menu items is what the trimmed catalogue holds, and seven
nutrition rows is what the recorded sheet publishes. So the twenty chunks read
by hand are the live twenty, item for item, and the two findings §6 records were
already fixed in the renderers that produced these.

What is new is the three kinds §6 did not sample — `POLICY_SECTION`,
`FAQ_ENTRY` and `DOCUMENT_BLOCK`, 29 of the 51 — and they are the half a hand
review cannot reach from here: the chunk table is readable by the jobs and
readonly service principals and by nobody else, which is `databricks_catalog.tf`
working as designed and is also why the twenty below could not simply be
selected out of it. The verify job prints them; a person reading the run output
is the review, and extending §6 over those three kinds is filed rather than
claimed.

The count that matters most is the one that is zero: **no chunk exceeds
`EMBEDDING_CHARACTER_BUDGET`**. §5 argues that the budget is reported and never
acted on, and the argument would have been harder to hold if the live corpus had
produced a chunk that overran it. It did not.

Every other assertion in §7 passed, which is what SUCCESS means for that job: the
six kinds present, the columns exactly `gold_chunks.FIELDS` in order, every
`chunk_id` distinct, `carries_its_citation` and `keeps_the_row_whole` re-run as
filters and matching nothing, and — the check no per-row constraint can make —
every extracted table row, every deduplicated prose block and every menu item
becoming exactly one chunk: 7, 8 and 10 in, 7, 8 and 10 out.

## 7. What the live run has to show

`gold_chunk_verify.py`, run as the `chip-chat-gold-chunk-verify` job. It reads and never
writes.

```bash
databricks pipelines start-update $(terraform output -raw databricks_gold_chunk_pipeline_id)
databricks jobs run-now       $(terraform output -raw databricks_gold_chunk_verify_job_id)
```

It asserts, and fails the job on any of them:

1. `chip_chat.gold_harvested.corpus_chunks` exists, holds rows of all six kinds,
   and its columns are exactly `gold_chunks.FIELDS` in order.
2. Every `chunk_id` is distinct. A collision is two facts one of which can never
   be quoted.
3. Every expectation re-run as a filter matches nothing — including
   `carries_its_citation` and `keeps_the_row_whole`, which are #35's two required
   properties written as constraints. This catches the one failure mode the
   pipeline's own expectations cannot: a constraint quietly downgraded from
   `expect_all_or_fail` leaves violating rows in the published table.
4. Every extracted table row became exactly one chunk, every deduplicated prose
   block became exactly one chunk, and every menu item became exactly one chunk.
   A table of eight rows that produced seven lost one; one that produced nine
   split one. Both are "split across a chunk boundary" seen from the table's
   side, and neither is visible in a per-row constraint.
5. The deterministic twenty of §6, printed whole, ordered by kind and then by
   chunk id so a re-run after a change puts the same twenty beside the previous
   twenty.

It reports, and does not fail on, the count of chunks over
`EMBEDDING_CHARACTER_BUDGET`. See §5.

## 8. Cost

One more Lakeflow pipeline on a single-node job cluster, `continuous = false`,
and one more read-only verify job on the same shape. The trap
[#31](https://github.com/gganssle/chip_chat/issues/31) exists to close is an
always-on all-purpose cluster, and this adds none.

The argument for a pipeline of its own rather than a share of silver's is the
one `databricks_silver.tf` makes about bronze, one layer up, and its second half
applies harder: chunking is the logic that changes — a renderer's wording, a new
chunk kind, a metadata field #48 turns out to need — and iterating on it should
not involve recomputing twenty-four silver tables to find out whether a sentence
reads better.

The argument for a *fourth* pipeline rather than a share of #36's third is the
one at the top of this document, and it is the only decision here that the other
three layers did not have to make. #36's pipeline reads the generated accounts
and publishes into `gold_synthetic`; this one reads the real catalogue and
publishes into `gold_harvested`. One gold pipeline would have a single `schema`
default, a single event log and a single set of table properties across both,
and the cost of getting that wrong is not a mislabelled table — it is a
synthetic row in the retrieval index. Two pipelines cost one more single-node
cluster start on a manual trigger, and the thing they buy is that no code path
exists from the synthetic stream to a chunk.

## 9. What this hands to #48, which already took it

#48 landed first, against this schema as it stood on an unmerged branch. So this
section is half hand-off and half reconciliation, and the reconciliation is the
part with a test attached.

`chip_chat.search.chunks` restates `gold_chunks.FIELDS` — name, SQL type, and
the three index flags — so that the index can be built without importing a Spark
driver module, which is the same convention `gold_chunks.py` itself uses for the
constants it copies from `silver.py`. `search/tests/test_chunk_contract.py` is
what makes that a convention rather than a duplicate: it asserts the two tuples
are the same tuple, field for field, in order. While this issue was unmerged
that test skipped itself with a reason, and `cc-6rb` tracked the rejoin — the
search index was being built against a chunk schema nothing verified it matched.
It imports `chip_chat.databricks.gold_chunks` directly now and any drift between
the two fails `make ci`.

- One table, `chip_chat.gold_harvested.corpus_chunks`, and one schema,
  `gold_chunks.FIELDS`, with retrievability, filterability and facetability already
  decided per field and argued for in §2.
- `source_url` and `harvested_at` on every row, non-null by a fatal expectation,
  so "every document carries a resolvable `source_url` and a `harvested_at`" is
  inherited rather than re-established.
- `chunk_id`, stable across a rebuild, which is what makes an alias swap safe
  for a conversation in flight: the ids the previous index returned are still the
  ids the new one holds.
- Chunk text that is already the unit to embed. #48 configures integrated
  vectorization against `text` and does not chunk again — the skillset's own
  split skill is the fixed window this issue exists to refuse, and running it
  over these chunks would undo the whole of §3.

## 10. What this does not do

- **The hand review does not cover three of the six kinds.** §6.1 says which and
  why. Twenty chunks is what the criterion asks for and twenty is what was read;
  `POLICY_SECTION`, `FAQ_ENTRY` and `DOCUMENT_BLOCK` are 29 of the 51 chunks
  the live run produced and none of them was read one by one. They are printed
  by `gold_chunk_verify`, so the material is there.
- **No nutrient detail beyond calories.** `silver-conformance.md` §7 leaves this
  open and it stays open. The catalogue lands `calories` and the allergen marks;
  the per-nutrient figures in `parsed/chipotle/nutrition/item_nutrition` are not
  in bronze at all, so making them chunk metadata is a bronze source, a silver
  table and a seventh field group — a separate argument, and one #48 does not
  need to start. What *is* here is the published nutrition sheet's own rows, as
  `NUTRITION_ROW` chunks, whole.
- **No PDF prose.** `silver_conform.py` defines an `analysis_paragraphs` UDF and
  never calls it, so the paragraphs around the nutrition tables — including the
  footnote saying the chart does not reflect cross-contact — land in no silver
  table and there is nothing here to chunk. Filed as `cc-4r2`; the fix is a
  fourth corpus table in silver and a seventh kind here.
- **No NDJSON export.** `chip_chat.search.corpus` reads the index build's input
  from `corpus/runs/<run_id>/chunks/*.jsonl`, and this pipeline writes a Delta
  table in Unity Catalog, which a build on a laptop cannot read without a
  cluster. The export step belongs to whichever job owns the release pointer and
  is tracked as `cc-2yw`; until it lands, `make search-build CHUNKS=<dir>` reads
  a directory directly. Nothing about the *schema* changes when it does — the
  export is `gold_chunks.FIELDS` one key per column.
- **No embedding, and no index.** Integrated vectorization is
  [#48](https://github.com/gganssle/chip_chat/issues/48)'s, and this layer
  deliberately stops at text. It also hands #48 an instruction it should follow:
  configure vectorization against `text` and do **not** add a split skill.
  The skillset's own text-split skill is the fixed window §3 exists to refuse,
  and running it over these chunks would undo the whole of this issue.
- **No retrieval tuning.** Which fields are weighted, how hybrid scoring is
  balanced and where the semantic reranker sits are #48's and the retrieval
  evaluation's. What is settled here is only which fields exist and which of
  them can be filtered on.
- **No synthetic chunks, ever.** `gold_synthetic` stays empty of anything a
  retriever can see. See §1.
