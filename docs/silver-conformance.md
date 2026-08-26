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

> **Status.** Run. `chip-chat-silver-conform` completed against `dbw-chip-chat`
> on 2026-08-26 and `chip-chat-silver-verify` returned SUCCESS over what it
> wrote. §7 is what it did, including the six things that only failed once
> there was a cluster: five declarations that contradicted the package they
> were copied from, and one assertion that was true of the algorithm and false
> of this corpus.

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

### A page with nothing in it is not a document

Some pages the harvest fetched are not prose and were never meant to be.
`chipotle.policy.CATERING_URL` is read "for the address of its script bundle,
nothing else", and what came back is a 395-byte Vue shell: a `<title>`, a
`<noscript>`, an empty `<div id="app">` and two `<script>` tags. Every element
in it is furniture by the list above, correctly, so it extracts to nothing. It
is in the fetch-once cache because everything the harvest fetches goes through
the cache, not because anybody wanted its text.

So a fetched page that carries no prose is **not a document** and does not enter
the corpus. That exclusion is the one row removal in this layer that does not
stop an update — the thing §5 refuses everywhere else — so it is bounded rather
than trusted. `silver.MAXIMUM_PROSELESS_SHARE` is a quarter, the verify job
asserts against it, and it prints every URL it excluded by name. A stripper that
regressed would empty most of the corpus rather than one page of it; one page in
thirty-eight is a publisher rendering a page in the browser.

The exclusion also makes conservation say more than it did. Criterion 2's check
is now `citations + proseless pages = fetched URLs` rather than `citations =
fetched URLs`, so every fetched URL is accounted for either way — and a document
that silently lost a citation and a page that was silently dropped stop looking
identical from outside.

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
OR (reason = 'REWARD_REDEEMED'  AND reward_name IS NOT NULL AND order_id IS NOT NULL)
OR (reason IN ('SIGNUP_BONUS', 'POINTS_EXPIRED') AND order_id IS NULL AND reward_name IS NULL)
```

A redemption references **both**, and that is the clause the first live update
found wrong. `loyalty.py` writes `order.order_id` beside the reward's name
because points are spent at the till, on the visit that earned on them, and
`LoyaltyEntry.order_id` says so outright: "a redemption names the order it was
spent on, which is the order that also earned on it". Written as `order_id IS
NULL` the constraint failed 13,684 of the ledger's 32,234 rows, and would have
deleted exactly the link [#27](https://github.com/gganssle/chip_chat/issues/27)
reconciles earned points against.

Those four strings are the generator's, copied here because `silver.py` may not
import a sibling, and `test_silver.py` asserts them against `population.toml`
through `load_config()`. A reason added there and forgotten here fails `make ci`.

### Money

Bronze lands money as the string the writer wrote — deliberately, so a population
digest is stable across machines. Silver casts it to `DECIMAL(10,2)`, which is
where the pipeline can say what it did. Never `DOUBLE`: the harvest goes to the
trouble of parsing money out of the JSON token's own text to avoid binary-float
noise, and a float here would put it straight back.

`order_items` also checks its own arithmetic, and the check is a **floor**:
`line_total >= unit_price * qty`. A line is priced `qty * (unit price + every
modifier's own published price)`, and a modifier the catalogue prices at zero is
free here too — so the item price is a bound the total may sit above and can
never sit below. Written as an equality it fails every line carrying a priced
modifier, which is half of them, and it did: 24,592 of 48,767 on the first live
update. The full identity needs the modifier prices summed over an array, which
is a join and an aggregate rather than a column expression; `data-gen` asserts
it against the catalogue in `test_referential_integrity.py`, and this document
says so rather than implying silver re-derives it.

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
2. **Deduplication reduces and conserves.** Fetched HTML responses against
   distinct documents, and block occurrences against distinct facts — each with
   its citation total, which must match the count before the collapse.

   The two reductions are asserted differently, and the difference is the one
   thing the live run changed here. The **document** reduction is an inequality:
   more URLs fetched than documents kept. The **block** reduction is an
   *equality*, against the distinct `(heading, text)` pairs recomputed in the
   verify job from the text rather than from the digest the pipeline grouped on.
   `distinct < occurrences` looks stronger and is weaker: it cannot tell a
   `block_digest` that stopped collapsing anything from a corpus in which no
   fact is published on two documents, and both produce the same number. The
   equality tells them apart, and catches over-collapsing besides — which the
   inequality never could.
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

## 7. What it did when it was run

2026-08-26, against `dbw-chip-chat`, catalog `chip_chat`. The landing zone is
the one `docs/bronze-ingestion.md` §6 describes, plus `catalog/chipotle/` and
`parsed/chipotle/policy/`, seeded from the committed fixtures through the real
writers.

| Run | Result |
| --- | --- |
| `terraform apply`, targeted at the silver resources | 7 added, 2 changed — the pipeline, the verify job, three notebooks, `silver.py`, and bronze's fourteen new sources |
| Bronze update `6079b5e4`, `--full-refresh` | **COMPLETED** — see "the schema that outlived its table" below |
| Silver update `740e16df` | **COMPLETED**, 24 tables |
| Verify job run `310675037690290` | **SUCCESS**, 398 s |
| Silver update `39c116db`, re-run from the committed tree | **COMPLETED** |
| Verify job run `885499660637937` | **SUCCESS** — every count identical |

The last two are the same commit's notebooks against the same landing zone, and
they are in the table because a materialized view recomputes in full every
update: a second run returning a different number would mean the conformance is
reading something that moved. It returned the same twenty-four.

What landed:

```
chip_chat.silver_harvested                chip_chat.silver_synthetic
      4  allergens                             500  demo_visitors
      1  catalog_manifest                    32234  loyalty_ledger
      5  caveats                             48767  order_items
      8  document_blocks                     18898  orders
      7  document_tables                        28  persona_fixtures
      6  documents                               7  personas
      8  faq_categories                          1  population_manifest
     11  faq_entries
     40  item_allergens
     20  item_prices
     10  menu_items
     10  modifiers
      2  policy_documents
     10  policy_sections
      3  rewards
     30  stores
      8  vocabulary
```

Every synthetic count is the number bronze-ingestion.md §6 recorded, to the row.
Conformance cast money, resolved four foreign keys and deduplicated on the
published key without losing one of 100,435 rows — which is the cheapest
available evidence that the dedup partitions are identities and not display
names.

The corpus: **38 fetched HTML URLs → 6 distinct documents → 8 facts.** Thirty of
those URLs are store-locator pages publishing one identical block, and three are
`robots.txt` served as `text/html`; they collapse to two rows between them. The
four that are left are `chipotle.com`, `/rewards`, `/rewards-terms` and
`/allergens`. One further URL — the catering home page — carries no prose and is
excluded, which is §4's rule earning its place on the first run rather than on a
hypothetical one.

Against the three acceptance criteria:

- **Silver tables for both streams, expectations enforced.** All 24 exist and
  hold rows, in both schemas, and every constraint re-run as a filter matched
  nothing. The enforcement is not a claim about the code: four of the six
  defects below were *found* by an expectation stopping the update, and a
  fifth by the graph refusing to validate.
- **Deduplication reduces and conserves.** 38 → 6 → 8, with citations equal to
  occurrences at both levels and the excluded page counted rather than dropped.
- **Boilerplate removal, on a sample.** No surviving block appears in more than
  half the corpus; the most repeated blocks and three whole documents are in the
  run's output.

Plus the one the criteria do not name: silver holds one row per distinct
`item_id` bronze landed.

### The schema that outlived its table

Every `chip_chat.bronze_synthetic` table arrived carrying 50 columns — the union
of all seven files in `accounts/synthetic/` — so `order_items` had an all-null
`demo_id` on it, `personas` had `line_total`, and so on. The data was right; the
schema was not. `SELECT * EXCEPT (...)` then carried the spurious `demo_id` into
silver, where the join off `orders` adds the real one, and the update failed
graph validation before it read a row: *the column name(s) 'demo_id' are
duplicated in dataset 'chip_chat.silver_synthetic.order_items'*.

It is not a defect in the reader options. The fourteen sources #34 added use the
identical `path` + `pathGlobFilter` mechanism over `catalog/chipotle/` (nine
files) and `parsed/chipotle/policy/` (ten), and inferred cleanly on this same
run — `menu_items` 22 columns, `catalog_manifest` 11, `rewards` 12.
`bronze_ingest.py` and `autoloader_options` are byte-identical to what #33 ran.
What is stale is the *state*: version 0 of each schema, written during #33's run
without an effective glob.

> ⚠️ **A full refresh does not reset an explicitly configured
> `cloudFiles.schemaLocation`.** `schemaEvolutionMode = addNewColumns` widens
> and never narrows, and the schema location is a path this repository chooses
> rather than one the pipeline allocates — so a schema inferred wrong once
> outlives every table built from it, and the ordinary remedy does not touch it.
> Deleting `_autoloader/bronze_synthetic/*/_schemas` and re-running with
> `--full-refresh` re-inferred all seven correctly. The file ledger is not in
> there — DLT keeps its own — so this resets inference and nothing else.

### The five declarations that were wrong

Four of the five are `silver.py` disagreeing with the package it copied from;
the first is the notebook, and is the one of the six a reader is most likely to
write again. Each cost a cluster start to find, and none is reachable by
`make ci` — which is the argument for this section existing.

| Update | What stopped it | What was actually true |
| --- | --- | --- |
| `81d96b7d` | `ModuleNotFoundError: No module named 'silver'`, all three corpus flows | A UDF is cloudpickled and unpickled in a Python worker that never saw the driver's `sys.path`. A module global is pickled **by name**; the four UDFs now call a notebook-local `lib()` instead, so what crosses is a string and an import |
| `45f3c011` | `line_total = unit_price * qty` | A line is `qty * (unit + modifiers)`. 24,592 of 48,767 rows carry a priced modifier. Now a floor, `>=` |
| `7fbbd70c` | `says_something` on `documents` | `catering.chipotle.com` is fetched for its script bundle's address and is a 395-byte Vue shell. Not a document; excluded and bounded — §4 |
| `59b85541` | `references_a_real_order_or_a_real_reward` | A redemption names **both** the reward and the order it was spent on. 13,684 of 32,234 rows |
| `54bec6b9` | `every_term_resolves_to_an_item` | `item_ids` is empty by design for a vessel and a protein — each is half of an entree. 4 of 8 terms. Now `every_modifier_term_resolves_to_an_item`, exempting `silver.ENTREE_DERIVATIONS` |

The two that were copied constants — the ledger's reasons and the vocabulary's
derivations — are now asserted against `data-gen` and against
`catalog.records.Derivation` in `test_silver.py`, so the next disagreement is a
`make ci` failure rather than a cluster start.

And one that was not a declaration at all: the verify job's own criterion 2
asserted `distinct_blocks < occurrences`, which this corpus fails while working
correctly. §6 says what replaced it and why the equality is the stronger claim.

## 8. What this does not do

- **No chunking.** [#35](https://github.com/gganssle/chip_chat/issues/35) takes
  `document_blocks` and `document_tables` and structures them for retrieval.
  Silver's job is to make sure there is nothing in them that should not be
  chunked, and that every row can cite itself.
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
