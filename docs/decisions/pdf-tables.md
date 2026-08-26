# Decision: a table stays a table, and a mismatch is a finding

**Issue:** [#22](https://github.com/gganssle/chip_chat/issues/22) (bead `cc-5h0`) · **Decided:** 26 August 2026
**Implements:** RFC-001 §08, *"chunking follows structure, not length"*
**Unblocks:** [#24](https://github.com/gganssle/chip_chat/issues/24) (`menu_catalog`), and the chunking work

---

Two questions had to be answered before a PDF could be read at all, and both have an
easy answer that is wrong in the same way.

## 1. What shape does an extracted table land in?

The easy answer is text. Document Intelligence returns a `content` string with the whole
document flattened into it, and a chunker that took that string and cut it every eight
hundred characters would work, in the sense of running without error.

RFC-001 §08 says exactly what it would produce:

> Fixed-window chunking splits nutrition tables across boundaries and produces exactly
> the confident wrong answers that allergen questions cannot tolerate.

Consider the concrete failure. A window ends after `Cheese | 1 oz | 110 | 8`. The next
begins `| 5 | 260 | Guacamole | 4 oz | 230`. Retrieval scores the second chunk highly for
a question about guacamole and returns it. It contains the number 260 — Cheese's sodium —
sitting immediately before the word Guacamole, under no heading at all. Every ingredient
of a confident wrong answer is present, and nothing downstream can detect it, because by
then there is no column left to check against.

The decision is that **the extraction never becomes text in the first place**.
`pdf_table_cells` holds one row per cell, carrying `row_index`, `column_index`,
`row_span` and `column_span` exactly as the service reported them, and `pdf_tables`
carries the column headings beside them. `LayoutTable.rows()` is the only way to iterate
a table, and it yields whole rows. A row is available complete, with its headings, or it
is not available.

That does not *stop* a later chunker from flattening a row into a sentence — it should,
because a retrievable chunk has to be text. What it stops is the flattening happening
where the structure is still the only copy of the information. Once a row and its
headings are two columns in a table, the sentence can be rebuilt any number of times; a
window that cut through the row cannot be un-cut.

**Cost.** Four tables instead of one, and a consumer that wants a paragraph has to
assemble it. That is the intended direction: assembling is a decision someone makes
deliberately, and losing a boundary is one nobody makes at all.

## 2. What happens when the PDF and the calculator disagree?

Chipotle already publishes nutrition figures through the endpoints its own calculator
reads, and issue #20 harvests them. A nutrition sheet would cover much the same items.
So the two sources will overlap, and overlapping sources eventually disagree.

The easy answer is to merge — prefer one source, or take the newer `harvested_at`, and
write a single number. The result is a table where the sodium figure for one item is a
different kind of fact from the sodium figure for its neighbour, and nothing says which.
The issue is explicit that this is not wanted:

> Reconcile extracted tables against the structured nutrition data from the calculator
> endpoints where both exist — **a mismatch is a finding, not a merge conflict to
> resolve silently.**

So `pdf_nutrition_findings` records the comparison and both numbers, and nothing is
overwritten. The verdicts are:

| Finding | What it means |
| --- | --- |
| `AGREES` | Both publish it; equal. |
| `DISAGREES` | Both publish it; different. Both numbers kept. |
| `UNIT_MISMATCH` | Both publish a figure, in different units. |
| `PORTION_MISMATCH` | The sheet's serving is not the portion the calculator's figure is for, so the two are not comparable and are not compared. |
| `NOT_PUBLISHED` | The sheet publishes a figure the calculator does not. New information, not a conflict. |
| `UNMATCHED_ITEM` | The row's label matches no menu item. |
| `UNMATCHED_COLUMN` | The heading matches no published nutrient. |

Three of those deserve their own sentence.

**`UNIT_MISMATCH` is worse than `DISAGREES`, not milder.** Twenty-two grams and
twenty-two milligrams compare as equal. A reconciliation that only compared numbers would
report that pair as agreement — the most confident wrong answer available.

**`PORTION_MISMATCH` refuses to compare rather than comparing carelessly.** Chipotle
publishes each figure for a stated portion — four ounces of steak, one ounce of cheese.
Eight grams of fat per ounce and eight grams per four ounces are not the same claim, and
declaring either agreement or disagreement between them would be inventing a
relationship that does not exist.

**`UNMATCHED_COLUMN` exists so that a column cannot be quietly dropped.** Which is the
other half of this decision.

## 3. Which column is which is asked of the data, not spelled out

A parser needs to know which column holds the item name, which holds the serving, and
which nutrient each remaining column carries. Two ways to decide, and again the easy one
is wrong.

The easy way is a table of spellings: `"item"`, `"menu item"`, `"product"` all mean the
item column; `"calories"` means `tcal`. It works until a sheet uses a fourth spelling,
and then the parser silently stops reconciling and reports nothing wrong.

The way taken is to ask the published vocabulary, which issues #19 and #20 already
harvested:

- **The item column** is the one whose cells are published item names. Not the one whose
  *heading* is recognised — the one whose *contents* are.
- **The serving column** is the one whose cells parse as a number and a portion unit the
  nutrition data actually uses.
- **A nutrient column** is one whose heading matches a published nutrient name exactly,
  after collapsing whitespace, folding case, and taking off a parenthesised unit. `Total
  Fat (g)` matches the published `Total Fat` and states the unit `g`; `Calories` does not
  match the published `Total Calories`, and that is deliberate.

The last point is the one to argue with, so: **an unmatched heading is recorded, not
guessed at.** Attaching `Calories` to `tcal` by prefix, or by edit distance, or by
"closest published name" is exactly the mechanism that would one day attach `Calories
from Fat` to `Total Calories` and publish a calorie count off by a factor. A
`UNMATCHED_COLUMN` row in the findings table is visible, greppable and cheap to fix by
hand; a mis-attached column is invisible and produces a number that looks like every
other number.

**Cost.** The first real Chipotle nutrition sheet will very likely have at least one
heading that does not match, and someone will have to look at the findings and decide
what it is. That is a few minutes of work, once, in exchange for never silently
mis-labelling a nutrient.

## 4. A related consequence: a link ending in `.pdf` is not a PDF

Checked separately, before anything is sent to Azure, by looking at the first five bytes.
A stale link answered with an HTML error page would otherwise buy a structured extraction
of the words "page not found" — and file it as nutrition data. The URL is still recorded
as discovered, in `rejected_urls`, because a link that used to be a sheet and now is not
is worth seeing.

`unread_urls` is kept apart from it for the same reason: "Chipotle changed that link" and
"this landing zone predates the link" want different responses, and one list would say
neither.

---

## What this decision does not settle

- **How a row becomes a retrievable chunk.** That is the chunking work's job, and issue
  #22's third criterion — *no nutrition row is split across a chunk boundary* — is
  asserted there. What is settled here is that the row survives intact to that point,
  which is asserted by `test_no_nutrition_row_is_split_across_a_boundary`.
- **What to do about a disagreement.** The dataset records it. Deciding that the sheet is
  stale, or that the calculator changed, is a human reading the findings table.
- **Whether a PDF's figure may ever be served to a customer.** It may not, yet. Nothing
  downstream reads `pdf_nutrition_findings` as a source of nutrition facts; the
  calculator data of issue #20 remains the only one. This dataset is evidence about that
  data, not a second copy of it.
