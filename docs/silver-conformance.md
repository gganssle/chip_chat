# Silver conformance

How both streams stop being what arrived and start being what is true: what gets
deduplicated and on which key, what counts as boilerplate and how its removal is
checked, and which violations stop the pipeline outright. Issue
[#34](https://github.com/gganssle/chip_chat/issues/34).

Everything below is `infra/terraform/databricks_silver.tf`,
`databricks/notebooks/silver_conform.py` and
`databricks/src/chip_chat/databricks/silver.py`, plus the fourteen sources #34
added to `databricks/src/chip_chat/databricks/bronze.py`. Nothing was made by
hand in the workspace UI.

> **Status.** The code, the declarations and the Terraform are here and
> `make ci` is green over them, including the boilerplate stripper and the
> deduplication rule, which are run against known documents rather than
> described. The pipeline has **not** been run against `dbw-chip-chat` yet —
> doing so needs a `terraform apply` and a landing zone carrying
> `catalog/chipotle/` and `parsed/chipotle/policy/`. §6 says exactly what the
> live run has to show, and it is a job you run rather than a screenshot you
> take. Tracked separately.

## 1. The shape

One Lakeflow Spark Declarative Pipeline, `chip-chat-silver-conform`, reading
bronze and writing twenty-four tables into the two silver schemas.

```
chip_chat.bronze_harvested                 chip_chat.silver_harvested
├── raw_documents ─┐                       ├── documents          (cleaned, deduplicated)
├── raw_bodies ────┴──── extract ────────► ├── document_blocks    (one fact, N citations)
├── document_analyses ── read tables ────► ├── document_tables    (whole rows, with headings)
├── menu_items ─────────── conform ──────► ├── menu_items
├── item_prices, modifiers, stores,        ├── item_prices, modifiers, stores,
│   item_allergens, allergens, caveats,    │   item_allergens, allergens, caveats,
│   vocabulary, catalog_manifest           │   vocabulary, catalog_manifest
└── policy_documents, policy_sections,     └── policy_documents, policy_sections,
    faq_categories, faq_entries, rewards       faq_categories, faq_entries, rewards

chip_chat.bronze_synthetic                 chip_chat.silver_synthetic
├── personas, demo_visitors,               ├── personas, demo_visitors,
│   persona_fixtures ────── conform ─────► │   persona_fixtures
├── orders, order_items ─────────────────► ├── orders, order_items
├── loyalty_ledger ──────────────────────► ├── loyalty_ledger
└── population_manifest ─────────────────► └── population_manifest
```

**Silver reads bronze and never the landing zone.** There is no `cloudFiles` and
no `readStream` anywhere in `silver_conform.py`, and `test_silver.py` asserts
their absence. That is what keeps "bronze is what arrived" a property of the
whole layer rather than of most of it — a silver table reading ADLS directly
would be a fact with no checkpoint behind it and no quarantine in front of it.

**A second pipeline, not a second half of the first.** Bolting silver onto
`chip-chat-bronze-ingest` would save one cluster start per full run, and this
project counts cluster starts. It is still the wrong shape: you could not
re-conform without re-listing the landing zone, which is exactly the loop you
want when a boilerplate rule or an expectation changes. What the split actually
costs is one extra single-node cluster start on a manual trigger, and the trap
[#31](https://github.com/gganssle/chip_chat/issues/31) exists to close is an
always-on cluster, which `continuous = false` closes here as it does there.

**Materialized views, not streaming tables.** Silver deduplicates over a window
and resolves foreign keys by joining, and an append-only stream can do neither
honestly — a duplicate arriving in a later update has to be able to displace the
row already written. There is therefore no `checkpoint_uri` in this pipeline's
configuration, and its absence is a decision rather than an omission: Auto
Loader's file ledger belongs to the layer that reads files.

**The declarations are data, in a stdlib-only module**, same as bronze and for
the same packaging reason — Terraform uploads `silver.py` beside the notebook and
the notebook puts that directory on `sys.path`. It matters more here, because
silver *parses HTML*: the boilerplate stripper is `html.parser.HTMLParser`, the
standard library, and the same parser the harvest reads Chipotle's pages with. A
readability model or a third-party extractor would be a cluster library, and a
cluster library would end the arrangement where the file pytest imports is the
file the driver runs.

## 2. The collision #33 deferred, settled

Five table names — `menu_items`, `item_prices`, `stores`, `caveats`,
`item_allergens` — are each written twice into the landing zone: once by a parser
under `parsed/chipotle/*`, and once by the catalogue build under
`catalog/chipotle/`. #33 refused to choose by naming convention and left it to
conformance, which is this issue.

**The catalogue wins, for every name it publishes.** The two files are not two
candidates for one fact; one is an *input* to the other. `catalog/records.py`
says so in its own header: the parsed tables are "one harvest of one site", and
the catalogue is "the consolidation three other subsystems resolve against".
Landing both would land the same item twice under two names, which is precisely
the duplication this issue exists to remove.

So `parsed/` is read for nothing the catalogue publishes. What *is* read from it
is the two things the catalogue does not consolidate:

| Landed from `parsed/chipotle/policy/` | Why the catalogue cannot cover it |
| --- | --- |
| `policy_documents`, `policy_sections` | Published prose. The catalogue consolidates food. |
| `faq_categories`, `faq_entries` | The same, plus the FAQ's published order |
| `rewards` | Every redemption in the loyalty ledger has to resolve to a row here |

`parsed/chipotle/menu`, `parsed/chipotle/nutrition` and `parsed/chipotle/pdf` are
not ingested at all — everything in them reaches the lakehouse through the
catalogue, and the PDFs reach it as bytes and as a Document Intelligence reading
already. `stores`, `store_profiles`, `store_hours` and the catering tables from
the policy parser are likewise not landed: the first three are consolidated into
the catalogue's `stores`, with its week of hours nested on the row.

The fourteen new sources go into **bronze**, tagged `chip_chat.issue = gh-34`,
rather than being read straight into silver. A reference table silver resolves
against has to arrive the way everything else does — through a checkpoint that
makes a re-run idempotent and a quarantine that catches a document that did not
parse. `Source.issue` is what lets a reader of the catalogue browser tell #33's
corpus from #34's reference tables without opening a repository.

## 3. Deduplication

Two mechanisms, because there are two different kinds of duplicate.

### The row duplicate: a landing-zone artefact

The harvest and the generator rewrite their tables under new file names, Auto
Loader consumes both files because they are both new files, and bronze holds the
row twice — correctly, because bronze does not transform. Silver keeps the latest
arrival per published key:

```sql
ROW_NUMBER() OVER (PARTITION BY <identity> ORDER BY _ingested_at DESC, _source_path DESC) = 1
```

`_source_path` breaks the tie when two files landed in the same update. A
deterministic order matters more here than which of two identical rows wins.

The same window runs over the two bronze tables the corpus is built from — the
fetch-once cache's pointers, keyed on `requested_url`, and its bodies, keyed on
the digest that is the blob's own file name. Neither is declared in
`silver.TABLES`, so without that they would be deduplicated a second way that
happens to agree today, or not at all — and "not at all" would double every
citation in the corpus after a re-ingest, which is exactly the number the second
acceptance criterion is measured on.

> ⚠️ **The partition is the published key and never a display name.** #34's brief
> is blunt about this and it is worth restating: two Chipotle items share a name
> across categories, so a dedup keyed on `name` would keep one of them and delete
> a menu item nobody removed. The symptom would surface three layers away as an
> order item that stopped resolving to food that is still on the menu.
> `test_no_identity_is_a_display_name` asserts it for every table, and
> `silver_verify` asserts against the live tables that silver holds one row per
> distinct `item_id` in bronze, however many names collide.

### The fact duplicate: the one the issue actually names

*"The same nutrition figure published on three pages should be one fact with
three citations, not three facts."*

A document is identified by the digest of its **prose**, not of its bytes.
Bronze already content-addresses the bytes, which is the right identity for a
landing zone and the wrong one for a corpus: two responses differing only in a
cache-busting parameter, a build hash in a script tag or an A/B test's class name
are two files and one document.

Below that, a *block* — a heading and the text under it — is identified by
`block_digest(heading, text)` over its normalised text. Three blocks whose
heading and text agree are one row of `document_blocks`, and the three pages
become three entries in that row's `citations`.

The heading is part of the identity rather than metadata beside it. The same
sentence under "Allergens" and under "Nutrition" is being said about two
different things, and merging them would produce a fact whose citations were not
making the same claim.

Normalisation is NFKC plus whitespace collapse. **Case is deliberately not
folded** — whitespace carries no meaning here and case sometimes does, and
folding it is the kind of normalisation that eventually merges two proper nouns.

### What deduplication conserves

Citations. Every collapsed row adds an entry to the array rather than
disappearing, which is what makes the criterion checkable in both directions:

- **Reduction** — fewer documents than fetched URLs, fewer facts than block
  occurrences. This is what the criterion asks for.
- **Conservation** — citations after deduplication equal occurrences before it.
  This is what stops the reduction from being achieved by throwing things away.

`source_url` and `harvested_at` are promoted out of the array onto the row — the
most recent citation, because that is the harvest the text currently reflects —
so that a chunk cites itself with one field and the issue's fourth expectation is
a column check rather than a traversal.

## 4. Boilerplate removal

The mechanism is **structural**, and that is a choice. A frequency heuristic
would strip whatever happened to repeat, which on a menu site includes the
allergen caveat that must never be stripped. A readability model would be a
cluster library and an unarguable decision. A tag list can be read, argued with,
and extended by somebody who has actually looked at the page.

Four rules, cheapest first, all of them in `silver.py`:

| Rule | Examples |
| --- | --- |
| Where the page has a `<main>`, nothing outside it is read | the blunt instrument, and the reliable one |
| Tag | `head`, `title`, `nav`, `header`, `footer`, `aside`, `script`, `style`, `noscript`, `form`, `button`, `dialog`, … |
| ARIA landmark role | `navigation`, `banner`, `contentinfo`, `search`, `dialog` |
| `class` / `id` substring | `cookie`, `consent`, `onetrust`, `skip-link`, `sr-only`, `visually-hidden`, `screen-reader`, `breadcrumb`, … |

Plus `aria-hidden="true"` and `hidden`, which say it outright.

The screen-reader classes are on the list for a subtler reason than the rest.
Their text is real English, invisible to a reader, and repeated on every page —
"skip to main content", "opens in a new window". It is the single best way to
poison a chunk embedding with words no visitor ever saw.

`banner` alone is deliberately **not** a class hint, and `privacy-banner` is: a
hint here is a promise that no content element in this corpus carries the
substring, and hero images that hold real copy break the bare word's promise.

> ⚠️ **Every tag on the boilerplate list has a closing tag, and that is a
> requirement rather than a coincidence.** The skip is bounded by nesting depth,
> so a *void* element there would open a subtree that never closes and silence
> the rest of the document. The related bug is worse and easier to write:
> `HTMLParser` reports a bare `<img>` as a start tag and never as an end tag, so
> counting it against the depth leaves the count one too high for everything
> after it — and the next `</div>` closes a skipped subtree that was still open.
> A page with one image would then extract its navigation and lose its prose.
> Real pages are full of images. `silver._VOID` is checked before anything else,
> and `test_a_void_element_does_not_swallow_the_rest_of_the_page` is the guard.

### How its removal is checked, rather than admired

The evidence is separate from the mechanism, and it is `document_frequency`: how
many distinct documents a block appears in, beside `corpus_documents`, which is
how many there are.

Boilerplate is, by definition, the text that is on nearly every page. So if the
stripper missed the footer, the footer arrives as one block whose document
frequency is the size of the corpus — and the expectation

```sql
document_frequency <= corpus_documents * 0.5
```

stops the update. That is #34's third acceptance criterion — "boilerplate removal
verified by inspecting a sample of chunks" — turned into something that runs. The
sample is still printed: `silver_verify` shows the ten most widely repeated
blocks, which is exactly where a missed footer would be.

Half is generous on purpose. A genuinely shared fact — the allergen caveat, the
same nutrition figure on three pages — is precisely what `document_blocks` is
*supposed* to collapse into one row with several citations, and the threshold
must not turn that success into a failure. Furniture does not appear on half a
site; it appears on all of it.

## 5. Conformance, and the expectations that are fatal

### Every expectation fails the pipeline

There is no warn level in this layer, no `expect`, and no `expect_or_drop`. The
issue asks for expectations "enforced, failing the pipeline on violation", and
#34's brief is blunter still: an order item that does not resolve to a catalogue
row is a hard failure, not a warning.

That is a real commitment rather than a slogan. It means a corrupt harvest stops
the lakehouse instead of quietly serving a menu with a hole in it. Bronze is where
a bad record is allowed to land — flagged, kept, queryable. Silver is where it is
not allowed through, and `_quarantined` rows never enter it at all.

### Referential integrity carries a column instead of leaving a receipt

`order_items` comes out of silver with `item_name` on it, from the real
catalogue, and the expectation is that the name is not null. A boolean
`_item_id_resolved` column would be a receipt for a check and nothing else;
`item_name` is a column the serving layer wants anyway, and its nullness *is* the
violation. One column, two jobs.

The join is a **left** join. An inner join would drop the violating row and
quietly satisfy the expectation it exists to test — the single easiest way to
write a referential-integrity check that can never fail.

The four expectations the issue enumerates, and where each lives:

| The issue says | Silver does |
| --- | --- |
| Every order item references a `menu_items` row | `order_items` joins the catalogue and carries `item_name`; `item_id_resolves` is fatal |
| Every loyalty entry references a real order or a real reward | three-clause constraint over the four published `reason` values |
| No null `demo_id` on any visitor-scoped row | `demo_id_is_present` on `orders`, `order_items`, `loyalty_ledger`, `demo_visitors` — and `order_items` gets its `demo_id` by carrying it off the order |
| No corpus chunk without `source_url` and `harvested_at` | `carries_its_citation` on all three corpus tables, and on every harvested table besides |

The loyalty rule is worth spelling out, because the issue's sentence is not
decidable as written. Points are earned on an order or spent on a reward — but
`order_id` and `reward_name` are both null for an opening balance and for an
expiry, and a check that did not know that would either fail every seventh row or
pass vacuously. So the constraint names all four published movements:

```sql
(reason = 'ORDER'            AND order_id IS NOT NULL AND reward_name IS NULL)
OR (reason = 'REWARD_REDEEMED'  AND reward_name IS NOT NULL AND order_id IS NULL)
OR (reason IN ('SIGNUP_BONUS', 'POINTS_EXPIRED') AND order_id IS NULL AND reward_name IS NULL)
```

Those four strings are the generator's, copied here because `silver.py` may not
import a sibling, and `test_silver.py` asserts them against `population.toml`
through `load_config()`. A reason added there and forgotten here fails `make ci`.

### Money

Bronze lands money as the string the writer wrote — deliberately, so a population
digest is stable across machines. Silver casts it to `DECIMAL(10,2)`, which is
where the pipeline can say what it did. Never `DOUBLE`: the harvest goes to the
trouble of parsing money out of the JSON token's own text to avoid binary-float
noise, and a float here would put it straight back.

`order_items` also checks its own arithmetic — `line_total = unit_price * qty` —
which is what turns "prices are computed from the catalogue rather than invented"
from a claim into something a reviewer can re-derive.

### What silver drops, and the one thing it keeps

`_source_path`, `_source_modified_at`, `_source_size_bytes`, `_rescued_data` and
`_quarantined` do not survive. The last two describe a failure mode silver has
already excluded; the first three describe the *file* rather than the fact, and
change on every re-harvest without the fact changing at all.

`_ingested_at` survives, because "when did this row arrive" still has an honest
answer about a silver row. `_conformed_at` is added beside it. Three clocks,
three columns: `harvested_at` is when the page was fetched, `_ingested_at` is
when bronze read it, `_conformed_at` is when this update ran.

## 6. Checking it, rather than believing it

Two commands, and the second reads what the first writes.

```bash
cd infra/terraform
databricks pipelines start-update $(terraform output -raw databricks_silver_pipeline_id)
databricks jobs run-now $(terraform output -raw databricks_silver_verify_job_id)
```

Run the bronze pipeline first: silver reads bronze, and the fourteen reference
tables #34 added to it have to have landed.

`chip-chat-silver-verify` runs `databricks/notebooks/silver_verify.py` on
single-node job compute as the jobs principal, reads and never writes, and
asserts its own result — SUCCESS means the claims held rather than that the
notebook finished. It returns its counts as the run's notebook output, so a run
can be quoted without opening the workspace.

What it asserts, against the three acceptance criteria:

1. **Silver tables for both streams, expectations enforced.** Every declared
   table exists and holds rows — and then every constraint is re-run as a filter
   and required to match nothing. This separates two cases worth separating: an
   `expect_all_or_fail` that stopped the update leaves the *previous* version of
   the table in place and looks healthy from outside, while a constraint
   downgraded to a warning leaves violating rows in the current one. Only the
   second is visible here, and it is the one that is a lie.
2. **Deduplication reduces and conserves.** Four numbers: fetched HTML responses
   against distinct documents, and block occurrences against distinct facts —
   each with its citation total, which must match the count before the
   collapse. A corpus that happens to contain no duplicates at all passes
   conservation and fails reduction, which is the right way round: it means the
   harvest changed and somebody should look.
3. **Boilerplate removal, on a sample.** The ten most widely repeated blocks are
   printed, and the five worst are asserted against the 50 % threshold. Three
   whole documents are printed end to end underneath, which is the "inspect a
   sample of chunks" the criterion literally asks for.

Plus the one #34's brief is bluntest about and the criteria do not name: silver
must hold one row per distinct `item_id` bronze landed, whatever the names do.

The part `make ci` can check for free, and does: the boilerplate stripper and
the deduplication rule are algorithms, and `test_silver.py` runs them over
documents written into the test file — a page with a cookie banner, a nav, a
footer and screen-reader-only text, and two item pages publishing the same
nutrition sentence. Four block occurrences, three distinct facts, four citations.

## 7. What this does not do

- **It has not been run.** See the status note at the top. The three acceptance
  criteria are claims about a live system, and until `silver_verify` has returned
  SUCCESS against `dbw-chip-chat` they are claims about an unrun pipeline.
- **No chunking.** [#35](https://github.com/gganssle/chip_chat/issues/35) takes
  `document_blocks` and `document_tables` — and four more of the tables here —
  and structures them for retrieval. It has since landed; see
  [corpus-chunking.md](corpus-chunking.md). Silver's job is to make sure there is
  nothing in them that should not be chunked, and that every row can cite
  itself.
- **No marts.** [#36](https://github.com/gganssle/chip_chat/issues/36) builds
  `customer_360`, `usual_order`, `item_affinity` and `spend_summary` on top of
  these tables.
- **No nutrient detail beyond calories.** The catalogue carries `calories` and
  the allergen marks, which is what the expectations here need. The per-nutrient
  figures in `parsed/chipotle/nutrition/item_nutrition` are not landed; if #35
  wants them as chunk metadata that is a separate source and a separate
  argument.
- **No schedule.** [#38](https://github.com/gganssle/chip_chat/issues/38) argues
  the weekly re-harvest. Nothing in this workspace should be able to start
  spending on its own.
