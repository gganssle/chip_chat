# Corpus freshness and the weekly re-harvest

**Issue:** [#38](https://github.com/gganssle/chip_chat/issues/38) ·
**Code:** `harvest/src/chip_chat/harvest/{freshness,changes,release}.py`,
`harvest/src/chip_chat/harvest/sources/chipotle/reharvest.py`,
`.github/workflows/reharvest.yml`

A real freshness story is what makes the difference between a corpus and a
snapshot someone took once. Before this, "the corpus is current" was asserted by
nobody and checkable by nothing: every harvest ticket shipped with fixture-based
tests, and a green suite proves the parsers handle *recorded* HTML and JSON, not
that the live site still matches those recordings.

Three things now exist that did not: the corpus is re-harvested on a schedule,
every run says what changed, and a corpus that has stopped moving is an exit
status rather than a number on a page.

```bash
make reharvest                 # refresh, diff, publish if it completed
make freshness                 # how old is the corpus, and is that acceptable
```

---

## 1. The shape

```
landing/
  raw/                          the fetch-once cache. Shared, append-only.
    index/<shard>/<id>.json       one pointer per URL
    blobs/sha256/<shard>/<hex>    one body per distinct set of bytes
  corpus/
    current.json                THE POINTER. One write of this is the swap.
    runs/<run_id>/
      run.json                  what the run did, complete or failed
      change-report.md          what changed, for a human
      parsed/<dataset>/*.jsonl  the tables this run produced
```

A run stages everything under `corpus/runs/<run_id>/` and touches nothing else
except the raw cache. Then, and only if the whole run succeeded, it writes
`corpus/current.json`. Downstream resolves the corpus through that pointer, so
one small write moves the entire corpus from last week's to this week's, and a
run that dies halfway moves nothing.

That is RFC-001 §08's rule — *the index is rebuilt, never patched* — applied at
the layer that exists today. [#48](https://github.com/gganssle/chip_chat/issues/48)
will do the same thing to an Azure AI Search index with an alias, and it is
deliberately the same rule rather than a different one that resembles it: the
index build reads the release pointer, so the two swaps are the same swap.

---

## 2. A re-harvest refreshes, it does not re-download

The issue asks for a weekly job "respecting the same rate limits and cache
semantics — a re-harvest refreshes, it does not re-fetch what has not changed."
A client cannot know whether a page changed without asking. What it can do is
ask *conditionally*.

Every response's `ETag` and `Last-Modified` are now recorded on its pointer. A
refresh offers them back as `If-None-Match` and `If-Modified-Since`; a 304 means
the stored body is still current, so `harvested_at` moves forward and no bytes
cross the wire. `revalidated_at` records that the timestamp came from a
confirmation rather than from a download, so the distinction stays recoverable.

**Measured against the live site, 26 August 2026:**

| | requests | bodies fetched | 304s | documents changed |
|---|---|---|---|---|
| cold run | 83 | 8.0 MB | 0 | 76 (all new) |
| re-harvest, 3 minutes later | 79 | 2.7 MB | 34 | 0 |

Bodies fell by 65%. The report prints both numbers on every run, so the claim is
checkable rather than asserted.

**What the source does not offer, stated plainly.** Only two of the four origins
publish validators at all:

| origin | documents | `ETag` | `Last-Modified` |
|---|---|---|---|
| `locations.chipotle.com` | 34 | 4 of 34 | all 34 |
| `catering.chipotle.com` | 3 | yes | yes |
| `services.chipotle.com` | 37 | **none** | **none** |
| `www.chipotle.com` | 6 | **none** | **none** |

So the 34 revalidations are the store pages, and the API endpoints — which are
the large ones — are re-fetched in full every week no matter what we send. That
is a property of Chipotle's CDN configuration, not of this code, and it is the
reason the numbers above are 65% and not 95%. Nothing here can improve it; a
future run that reports a much higher revalidation count means the source
started sending validators, which would be worth noticing.

**Politeness for a scheduled job.** `robots.txt` is a 404 on both
`www.chipotle.com` and `services.chipotle.com` — no rules published, which the
framework reads as allow-all — so nothing in the source constrains a weekly
crawl. The constraint comes from us instead:

- Every request goes through the framework's existing process-wide politeness
  gate. There is no second fetch path; the re-harvest is built on `cc-3np`
  rather than beside it.
- `--stores` defaults to **30** for a weekly run against 50 for a one-off
  harvest. Store profiles are the bulk of the request count and the least of
  what moves week to week.
- One run takes about two minutes forty against the live site, of which almost
  all is the gate waiting.

---

## 3. The change report

Two levels, because one of them is not enough. Comparing raw response digests
tells you that `/api/menu` changed; it does not tell you that the Barbacoa Bowl
is gone, and the issue's own example is that *a menu item disappearing is
interesting*.

**Documents.** One row per harvested URL — added, changed, unchanged, removed —
with both digests. Free, because the fetch-once cache already keeps the previous
digest, and it is the level at which "the site restructured its API" shows up.

**Rows.** One row per parsed record across all 32 tables of the four datasets,
keyed by whatever identifies it. `TABLE_KEYS` in each records module declares
that identity next to the table it belongs to.

Two decisions here are what make the difference between a report worth reading
and one that gets skimmed.

**`harvested_at` is excluded from the row comparison.** Every parsed row carries
it and it moves on every single run. Diff whole rows and *every row in the
corpus* is reported as modified every week, which is the same as reporting
nothing.

**A key that turns out not to be unique degrades the diff rather than corrupting
it.** These identities are declared by hand and a hand-declared key can be
wrong. When one collides, that table alone falls back to comparing row contents
and the report says so — a modification then shows up as one removal plus one
addition. Less informative, never untrue.

Some tables have no per-row identity to declare, because the source publishes an
ordered list without one: `meal_contents`, `policy_sections`,
`catering_package_options`, `caveats`. Those are keyed positionally, and an
inserted line there reads as a run of modifications. That is noisier than a real
identity would be and it is not wrong.

The two rows where this matters most are `item_allergens` and `item_diets`. A
tag means CONTAINS and an absent tag is *not* a published negative
(`cc-2bv`), so a status flipping from `CONTAINS` to `NOT_LISTED` changes what
the corpus will tell somebody about their allergy — and it degrades silently,
because nothing errors when it happens. Keyed by `(item_id, allergen_code)`, so
that flip is reported as a modification of one named row.

---

## 4. Freshness is enforced, not displayed

The corpus is exactly as fresh as its **stalest** document. An average age hides
the one page that stopped being re-fetched six months ago; a newest age is a
number that is always reassuring and never true. So the reported figure is the
oldest `harvested_at`, along with *which* document it belongs to — "the corpus
is 41 days old" is a fact, and "`…/nutrition` is 41 days old" is something you
can go and fix.

```
Corpus freshness: 76 documents, oldest 0.0 days old
  oldest       2026-08-26T19:58:46+00:00  https://services.chipotle.com/…/ingredients
  newest       2026-08-26T20:01:20+00:00  https://www.chipotle.com/graphql/…/FAQ-Query
  last release 2026-08-26T20:01:21+00:00  20260826T195844Z (0 documents changed)
  verdict      fresh (threshold 8 days)
```

Two documents are excluded from the measurement and both would otherwise flatter
it: `robots.txt`, which the framework re-reads every 24 hours whether or not
anything else is harvested and which never reaches a citation; and any cached
non-200, which is a record of an absence rather than a document.

**The threshold is eight days, not seven.** The job runs weekly; a threshold of
exactly seven fails on the morning of every run that is an hour late, which
trains people to ignore it. Eight means one missed run is a failure and a slow
run is not.

**An empty corpus counts as stale.** A landing zone with nothing in it is not a
fresh corpus. A check that passed on one would pass on a machine where the
harvest had never run, which is exactly the case it exists to catch.

Exit statuses, which are three rather than two:

| | `make reharvest` | `make freshness` |
|---|---|---|
| published, corpus fresh | 0 | 0 |
| harvest failed, nothing published | 1 | — |
| published, corpus still stale | 2 | 1 |

The last one is a different problem with a different fix and should not read as
the same failure as the first.

---

## 5. Where a human sees it

`.github/workflows/reharvest.yml` runs at 07:00 UTC on Mondays and:

- writes the rendered change report into the run's **job summary**, so it is the
  first thing on the page when you open the run;
- uploads it as an artefact with 90-day retention, so "when did this item
  disappear?" is answerable from the reports rather than from memory;
- does both **on failure too** — a weekly job whose only artefact when it breaks
  is a log line is a job whose failures nobody can compare;
- goes red when the harvest fails or the corpus is stale.

### Why GitHub Actions and not a Databricks job

The issue is filed under Phase 3, and this is the one part of it that is not in
the workspace. The reasoning:

- The re-harvest is a politeness-gated crawl of a public website. It is network
  bound, single threaded by construction, and does no Spark work. A Databricks
  job cluster would spend ~300 s of VM startup and a DBU-hour rate to do what a
  free runner does with an HTTP client.
- It could not land anywhere useful anyway. Nothing writes the landing zone to
  ADLS yet — the harvest and the generator are both local-only (`cc-j92`,
  `cc-b15`) — so a workspace-hosted harvest would have no container to write to
  and the bronze pipeline would ingest nothing new.

When those land, this workflow's last step becomes "sync the landing zone to
ADLS and start the bronze pipeline update", and the schedule stays where egress
is free. The release pointer is the signal that connects the two halves.

**The landing zone between runs** lives in GitHub's cache until it lives in
ADLS. That is best-effort by design: an entry untouched for seven days is
evicted, and a weekly run sits right on that edge. A miss is survivable — a cold
run re-harvests everything, reports every document as added, and publishes a
first release — and the report says which happened, so a silent cold start is
not possible.

---

## 6. What this does not do

- **No downstream rebuild past bronze.** The issue asks for silver, chunking and
  a fresh AI Search index alias-swapped into place on change. None of the three
  exists yet: silver is [#34](https://github.com/gganssle/chip_chat/issues/34),
  chunking is `cc-zix`/[#35](https://github.com/gganssle/chip_chat/issues/35),
  and the index is `cc-imu`/[#48](https://github.com/gganssle/chip_chat/issues/48)
  — which is additionally blocked on `cc-3wo`, AI Search Free tier having no
  capacity in East US 2. So the third acceptance criterion is met at the layer
  that exists: a simulated partial harvest leaves the published *corpus*
  untouched, proved by
  `harvest/tests/test_reharvest.py::test_a_partial_harvest_leaves_the_live_corpus_untouched`,
  which compares every released table byte for byte before and after a run that
  dies in the middle. When the index exists it swaps on this same pointer.

- **The raw zone is append-only, not transactional.** `raw/` is shared and is
  written as the harvest goes. Nothing is ever lost — bodies are
  content-addressed and a new one never destroys an old one — but a failed run
  does leave some pointers moved forward, and bronze (gh-33) ingests `raw/`
  directly and therefore sees that. The release pointer is what makes a
  *corpus* atomic; append-only is a weaker and different promise.

- **Releases are never pruned.** Each run stages a full copy of the parsed
  tables, about 5 MB. Fifty-two weeks is roughly 260 MB, which is nothing in
  ADLS and is the price of being able to diff against any past week. A lifecycle
  rule is the right answer once the landing zone is in ADLS; nothing deletes a
  published corpus today, deliberately. Filed as `cc-mwj`.

- **Freshness is not yet visible to a visitor.** The pointer's `harvested_at`
  already travels on every parsed row and RFC-001 §08 requires it to reach the
  response payload as a citation, but the chat reply carries no citations yet
  (`cc-bap`) and the API cannot reach the landing zone. Today freshness is
  visible in the weekly run's summary and queryable with `make freshness`.

- **The vocabulary generation of `cc-z1i` is still unwired.** RFC-001 §07 wants
  the vision enums generated from the live catalogue at build time. That is a
  different staleness detector for a different consumer, and this ticket does
  not close it. `GoldenSet.against(catalog)` (`cc-usl`) is a third. All three
  should end up reading the same release pointer.
