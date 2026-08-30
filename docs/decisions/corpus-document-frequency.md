# Decision: document frequency is evidence across site sections, not across the corpus

**Issue:** `chip-1om` (bd) · **Decided:** 28 August 2026 · **Not measured:** the re-materialised table
**Follow-ups:** `chip-78i` (the verify job), `chip-9o1` (the refresh), `chip-p5r` (the residual)
**Changes:** `silver.MAXIMUM_DOCUMENT_SHARE` (its meaning, not its value), `is_not_furniture_the_stripper_missed`, one new expectation
**Does not change:** the boilerplate stripper, `MAXIMUM_PROSELESS_SHARE`, citation conservation, the four invariants

---

`silver_harvested.document_blocks` has never held a row. Every full refresh of
`chip-chat-silver-conform` since 26 August 2026 fails
`is_not_furniture_the_stripper_missed`, and because every expectation in this
layer is `expect_all_or_fail` — deliberately, and argued at length in
`chip_chat.databricks.silver`'s module docstring — a failed expectation is an
empty table rather than a table with a warning attached to it.

The block that fails it is Chipotle's **"Try our Featured Meals"** promotional
module. It is syndicated onto all thirty `locations.chipotle.com` store pages,
the corpus holds thirty-five documents, and thirty of thirty-five is a document
share of **0.857** against a threshold of **0.5**.

It is not furniture, and the stripper is not at fault. The markup is
`<div class="py-8 sm:py-16 bg-gray-200 px-4">`: Tailwind utility classes, no
`id`, no ARIA role, no semantic element name. `BOILERPLATE_TAGS`,
`BOILERPLATE_ROLES` and `BOILERPLATE_CLASS_HINTS` are structural rules over
exactly those three things, and there is nothing here for any of them to hold
on to. The text itself is real published English that a visitor sees.

## The two answers that were on the table

**(a) Strip syndicated promotional modules some other way.** Reach the module
by something other than tag, role or class — repetition across pages, position
in the document, link density, a class-substring list tuned to this one site's
build output.

**(b) Accept that the threshold is measuring the wrong thing.**
`MAXIMUM_DOCUMENT_SHARE`'s own docstring already says: *"A genuinely shared
fact — the allergen caveat, the same nutrition figure on three pages — is
exactly what deduplication is supposed to collapse into one row with several
citations, and the threshold must not turn that success into a failure."* Seven
blocks published on thirty pages, arriving as seven rows with thirty citations
each, is that sentence describing itself.

## The decision

**(b), with the denominator fixed rather than the number raised.** The share is
now asked only of a block whose documents do **not** all belong to one site
section, and a second expectation is added underneath it so that the corpus's
composition can never buy a block a free pass.

The argument in one paragraph. A ratio against `corpus_documents` answers the
question *"is this on most of the corpus?"*, and that is the same question as
*"is this furniture?"* only when the corpus is several site sections in
comparable proportion. This corpus is eighty-six per cent one section of one
site, so the ratio answers a question about **how the seed list was built** and
reports the answer as a verdict on the stripper. Furniture is not merely
frequent text; it is text on pages that have nothing else in common — the
footer is on the store pages *and* the menu page *and* the FAQ. Crossing that
boundary is the part of "on nearly every page" a lopsided corpus cannot fake,
and it is the part worth checking.

(a) was declined on the ground the module docstring already commits to: *the
mechanism is structural — a tag list, a role list and a small set of class
substrings, all named in this module — because a structural rule can be read
and argued with, and a similarity heuristic cannot.* Every version of (a) is
the heuristic. "Repeated on many pages" is the failing check with a different
name on it. "Link-dense" and "near the bottom" are guesses about layout that
this repository would then own against Chipotle's next deploy. A hint list
tuned to `py-8 sm:py-16 bg-gray-200` is a promise that no content element on
any page in this corpus carries those substrings, which is a promise about a
utility-class framework whose whole design is that the same class is on
everything. And each of them deletes real published prose to satisfy a check,
which is the wrong direction for a layer whose sentence is *bronze is what
arrived; silver is what is true*.

**The number did not move, and moving it was the option to avoid.** 0.5 is
still 0.5. Raising it to 0.9 would have let this corpus through and failed the
next one that was ninety-five per cent store pages, for the identical wrong
reason, and the failure would then have been harder to argue about because the
threshold would look tuned.

## The expectation, before and after

Before — one expectation on `document_blocks`:

```sql
document_frequency <= corpus_documents * 0.5
```

After — two, which fail for different reasons and print different names into
the pipeline event log:

```sql
-- is_not_furniture_the_stripper_missed
document_frequency <= corpus_documents * 0.5
OR size(array_distinct(transform(citations, citation ->
     regexp_extract(lower(citation.source_url),
                    '^[a-z][a-z0-9+.-]*://(?:www\\.)?([^/?#]+)', 1)))) = 1

-- is_not_on_every_document_in_the_corpus
corpus_documents < 2 OR document_frequency < corpus_documents
```

Both are assembled in `chip_chat.databricks.silver` from the same constants the
Python rule uses, and `furniture_verdict()` states the pair once more in Python
so it can be run on a laptop and, when `silver_verify.py` is moved onto it,
against the live table. Neither reads a column the pipeline was not already
writing: the site section comes out of the `citations` array that deduplication
conserves anyway, which is why this fix is a declaration change and the
alternative shape below is not.

Three notes on the SQL, each of which is a bug avoided rather than a
preference. It is `regexp_extract` and not `parse_url`, because a regular
expression cannot raise on a malformed URL and a constraint that throws reports
a parsing problem instead of a corpus problem. A leading `www.` is stripped,
because `www.chipotle.com` and `chipotle.com` are one section and a footer
spanning both spellings has crossed no boundary. And the pattern is written
once in Python and doubled for the SQL string literal by
`site_section_expression()`, so the two dialects cannot drift apart unless
someone edits one of them to say something else.

### What this still fails on

A footer, a nav or a cookie banner the tag list missed appears on the store
pages and on the menu pages and on the FAQ. It spans two sections, the share is
asked about it, and it fails — at twenty documents out of thirty-five, well
before it reaches all of them. That is the case the expectation was written for
and it is unchanged; there is a test named after it.

## What it costs

**A page template's own furniture, confined to the largest site section, now
passes both expectations.** A footer on all thirty store pages and nowhere else
has one section and a frequency below thirty-five, and nothing here catches it.

That residual is accepted rather than overlooked, and it is asserted in
`databricks/tests/test_silver.py::test_the_residual_this_rule_accepts_is_the_one_written_down`
so that it is a fact in the test output rather than a paragraph in a document.

Closing it needs the share taken against **the largest single site section**
rather than against the corpus, and that is a different kind of change: it
needs a per-section document count as a second broadcast scalar in
`silver_conform.py`'s `_document_blocks`, which is a pipeline edit and a new
column, not a declaration. It was not made here for a reason worth recording —
this change had to be confined to the declarations and their tests, because the
notebooks were being edited concurrently — and it is `chip-p5r`.

**`silver_verify.py` has not been moved onto the new rule, and until it is, the
verify job will still fail on these blocks.** Its criterion-3 cell restates the
old ratio by hand (`row["share"] <= silver.MAXIMUM_DOCUMENT_SHARE` over the
five widest blocks) rather than calling into `silver.py`, so `chip-chat-silver-
conform` will now succeed and `chip-chat-silver-verify` will now be the thing
that fails. The one-line replacement is `silver.furniture_verdict(...) is None`,
which is why that function returns an expectation *name* rather than a boolean.
That is `chip-78i`, and it is P1 because it turns one job's silence into
another job's noise until it lands.

## The numbers that could not be measured, and why

**The table was not re-materialised.** There is no Databricks workspace or
credential in this change's environment, and `make ci` cannot see a Lakeflow
expectation in any case — that is the first sentence of the bead. Everything
below the line "0.857 > 0.5" is arithmetic redone offline over a corpus written
out in the test file, not a reading from `dbw-chip-chat`. The row count after a
refresh is therefore an **expectation and not a measurement**, and it is stated
as one in the next section.

**How many rows `document_blocks` will hold is not known.** It is the number of
distinct `(heading, text)` pairs across thirty-five documents, and nothing
offline can count it: the documents are in the landing zone and in bronze, not
in this repository. What can be said is that it is greater than zero, which is
the entire finding, and that seven of the rows will be Featured Meals blocks
carrying `document_frequency = 30`.

**Seven blocks, or one record?** `docs/menu-data.md` §3 names seven blocks and
quotes three of them — `ORDER NOW`, `MOST POPULAR`, `IT'S FUN TO GET REWARDED`.
The bead says the update fails "with exactly one record violating". Both are
reports of the same run's event log and they are not reconciled here, because
reconciling them means reading the event log, which means the workspace. It
does not change the decision: the rule is wrong for one such block and for
seven of them equally.

**The effect on retrieval was not measured.** The index holds 358 chunks and no
`DOCUMENT_BLOCK` chunks at all (`docs/menu-data.md` §6). Whether the blocks this
unblocks improve an answer, or merely add promotional copy to the index, is a
question for `make retrieval` and `make grounding` against a rebuilt index, and
neither was run. It is worth stating plainly that a promotional module is not
obviously *worth* indexing — the argument here is only that it is not furniture
and must not silence an entire table. If the chunker should skip it, that is a
judgement for #35's layer, made where a chunk type is chosen, and not one to
smuggle in as a data-quality failure two layers down.

**Whether any other block trips the new pair is unknown.** The old rule failed
the update at the first violation, so the event log has never reported what
sits underneath these blocks.

## Confirming it, from a workspace

Three steps, in this order. Terraform owns the deployed copy of `silver.py`, so
a full refresh against a stale workspace copy would re-run the old rule and
prove nothing — that failure has happened here before, on `publish.py`, which
sat thirty-seven lines behind `main` in the workspace while the committed fix
was believed to be live (`docs/workspace-drift.md`, `docs/menu-data.md` §3, and
the comment above `infra-check-databricks` in the `Makefile`).

```bash
# 1. Upload the changed declaration, then prove the workspace matches the repo.
terraform -chdir=infra/terraform apply \
    -target=databricks_workspace_file.silver_module     # make infra-apply
uv run python -m chip_chat.infra.workspace_drift        # make infra-check-databricks

# 2. Full-refresh the silver pipeline.
databricks pipelines start-update \
    $(terraform -chdir=infra/terraform output -raw databricks_silver_pipeline_id) \
    --full-refresh

# 3. Count what it wrote.
databricks sql ... -- or a SQL warehouse query:
SELECT COUNT(*) AS blocks,
       COUNT_IF(document_frequency = 30) AS featured_meals
FROM   <catalog>.silver_harvested.document_blocks;
```

**What to expect, framed as an expectation:** step 2 completes rather than
failing an expectation, and step 3 returns a `blocks` count in the hundreds
with `featured_meals` at or near seven. If `blocks` is zero the update failed
again and the event log names which of the two expectations did it; if it is
`is_not_on_every_document_in_the_corpus`, the stripper really has missed
something that is on all thirty-five documents and this decision was wrong.

`databricks jobs run-now $(terraform -chdir=infra/terraform output -raw
databricks_silver_verify_job_id)` is deliberately **not** in that list. It will
fail on criterion 3 until its own copy of the rule is replaced, as described
above, and a run of it today would be a false negative about work that is
finished.

## What this does not change

**The stripper.** Not one tag, role or class hint moved. This was never a
stripper bug and fixing it by widening the stripper would have deleted
published prose to make a check pass.

**Citation conservation.** The blocks this admits arrive the way #34 requires:
one row, thirty citations, every URL that served the text still on it. The
verify job's equality between citations after deduplication and occurrences
before it is untouched.

**`MAXIMUM_PROSELESS_SHARE`.** The other bound on this layer is about pages
that extract to nothing, and it is a separate mechanism with a separate number.

**The fourth invariant.** Everything in this document is the harvested corpus —
what Chipotle publishes. No part of it touches the synthetic accounts, and the
two remain different schemas reached through a required `stream` argument.
