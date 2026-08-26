# Bronze ingestion

How both streams get out of the ADLS landing zone and into Unity Catalog, what
each row carries when it arrives, and what happens to a document that does not
parse. Issue [#33](https://github.com/gganssle/chip_chat/issues/33).

Everything below is `infra/terraform/databricks_bronze.tf`,
`databricks/notebooks/bronze_ingest.py` and
`databricks/src/chip_chat/databricks/bronze.py`. Nothing was made by hand in the
workspace UI.

## 1. The shape

One Lakeflow Spark Declarative Pipeline, `chip-chat-bronze-ingest`, carrying both
streams. Ten streaming tables and two views:

```
abfss://raw@stchipchat….dfs.core.windows.net/
├── raw/index/**.json      → chip_chat.bronze_harvested.raw_documents
├── raw/blobs/sha256/**    → chip_chat.bronze_harvested.raw_bodies
├── analysis/**.json       → chip_chat.bronze_harvested.document_analyses
│                            chip_chat.bronze_harvested.quarantine   (view)
└── accounts/synthetic/
    ├── personas.jsonl          → chip_chat.bronze_synthetic.personas
    ├── persona_fixtures.jsonl  → chip_chat.bronze_synthetic.persona_fixtures
    ├── demo_visitors.jsonl     → chip_chat.bronze_synthetic.demo_visitors
    ├── orders.jsonl            → chip_chat.bronze_synthetic.orders
    ├── order_items.jsonl       → chip_chat.bronze_synthetic.order_items
    ├── loyalty_ledger.jsonl    → chip_chat.bronze_synthetic.loyalty_ledger
    └── manifest.json           → chip_chat.bronze_synthetic.population_manifest
                                  chip_chat.bronze_synthetic.quarantine  (view)
```

**One pipeline, not two.** The issue asks for "a declarative pipeline carrying
both streams", and there is a cost argument for the singular as well: one update
starts one cluster, lands both streams, and stops. Two pipelines would start two.
The table names are fully qualified in the notebook because a single pipeline
publishing into two schemas has no default schema that is right for more than
half of its tables.

**The declarations are data, in a stdlib-only module.** `bronze.py` is a table of
paths, formats, reader options, schema hints and row identities;
`bronze_ingest.py` is a loop over it. That split exists so the decisions are
somewhere `make ci` can assert them — `databricks/tests/test_bronze.py` checks
every one against the thing it has to agree with: the harvest's own landing-zone
prefixes, the generator's table list, the Unity Catalog layout, the Terraform,
and the notebook.

It also creates a packaging problem, and the answer is worth stating because it
looks like a shortcut and is not. A pipeline runs a *notebook* on the driver;
there is no wheel on the cluster to import from. So Terraform uploads `bronze.py`
and `catalog.py` as workspace files beside the notebook and the notebook puts
that directory on `sys.path`. Both modules import nothing but the standard
library so that this works, and the files uploaded are the very files pytest
imports — there is no second copy to drift.

### The reference tables, added later by #34

The diagram above is #33's scope: the corpus, "untransformed". The harvest
package's *parsed* tables were deliberately left out of it, and there was a
concrete problem waiting in those directories — five table names collide between
`parsed/chipotle/*` and `catalog/chipotle/`. `menu_items`, `item_prices`,
`stores`, `caveats` and `item_allergens` are each written both by a parser and by
the catalogue build, so landing them in one schema means choosing which file is
the real one. That is conformance, which is the title of
[#34](https://github.com/gganssle/chip_chat/issues/34), so it was left there
rather than settled here by a naming convention.

#34 made the choice — **the catalogue wins for every name it publishes**, because
the parsed tables are its inputs — and then landed fourteen more sources through
this same pipeline, tagged `chip_chat.issue = gh-34`:

```
catalog/chipotle/*.jsonl              → chip_chat.bronze_harvested.menu_items,
                                         item_prices, modifiers, stores,
                                         item_allergens, allergens, caveats,
                                         vocabulary
catalog/chipotle/manifest.json        → chip_chat.bronze_harvested.catalog_manifest
parsed/chipotle/policy/*.jsonl        → chip_chat.bronze_harvested.policy_documents,
                                         policy_sections, faq_categories,
                                         faq_entries, rewards
```

Only those five parsed tables, and only because the catalogue does not
consolidate them: the first four are published prose and the fifth is the
Rewards Exchange line-up every redemption in the loyalty ledger has to resolve
to. `parsed/chipotle/menu`, `parsed/chipotle/nutrition` and `parsed/chipotle/pdf`
are still not ingested at all. `docs/silver-conformance.md` §2 carries the full
argument.

The loop in `bronze_ingest.py` did not change. What changed is the length of
`SOURCES` and the addition of `Source.issue`, so that a reader of the catalogue
browser can tell #33's corpus from #34's reference tables without opening a
repository.

## 2. What every row carries

Four columns beyond the file's own, underscore-prefixed so they cannot collide
with a field the landing zone already has:

| Column | From |
| --- | --- |
| `_ingested_at` | `current_timestamp()` when the pipeline read the row |
| `_source_path` | `_metadata.file_path` — the full `abfss://` path |
| `_source_modified_at` | `_metadata.file_modification_time` |
| `_source_size_bytes` | `_metadata.file_size` |

and, on every parsed source, `_rescued_data` and `_quarantined`.

`_metadata` is Spark's hidden per-file column. It is not part of the inferred
schema, so it survives schema evolution and cannot be shadowed by a field
arriving from a file.

**`source_url` and `harvested_at` are not added here.** They are already columns
on `raw_documents`, because the fetch-once cache captures them at the edge — and
that is the only place they can honestly come from. A response body does not
carry the URL it was fetched from. RFC-001 §08 needs both to survive into a
response payload as citations, so `bronze_verify` asserts that every row of
`raw_documents` that parsed carries both. `raw_bodies` joins back to them on
`content_sha256`, which is the file's own name in a content-addressed store.

## 3. The four properties the issue asks for

### Schema evolution: new columns tolerated

`cloudFiles.schemaEvolutionMode = addNewColumns`. A file carrying a column the
table does not have stops the update, records the wider schema, and is picked up
whole by the retry — which a pipeline does for itself. No row is dropped for
being unexpected.

The alternative worth naming is `rescue`, which keeps a new column's values in
`_rescued_data` and never widens the table. That turns a new field into permanent
quarantine, which is the opposite of tolerating it.

> ⚠️ **`binaryFile` rejects every evolution mode but `none`.** Its schema is four
> fixed columns, so there is nothing for a new column to arrive in — but the
> reader does not treat the setting as vacuous. It refuses the flow outright with
> `CF_UNSUPPORTED_SCHEMA_EVOLUTION_MODE`. Verified 2026-08-26.

### Type changes surfaced, not coerced

`cloudFiles.inferColumnTypes` is on, and Auto Loader never re-types a column that
already exists: a value that no longer fits goes to `_rescued_data`, with the
row's path beside it, and the row still lands.

The schema hints narrow where that can happen to columns something downstream
actually depends on — identities, the two citation fields, and the columns that
are always null today (`thread_id`, `home_store_override`, `stated_preferences`,
`home_store_name`), which would otherwise have no inferable type at all and would
arrive the day a visitor first edits their persona rather than the day the table
was created.

**Money is deliberately not hinted.** `orders.total`, `order_items.unit_price`
and `persona_fixtures.lifetime_spend` are written as strings so that the
population digest is stable across machines. Casting a string to a decimal is a
transformation, and bronze does not transform. Silver casts them, and can say
what it did.

### Bad records quarantined, not dropped

Nothing is dropped anywhere. There is no `DROPMALFORMED` and no `FAILFAST`: the
first loses the record silently and the second fails the update, and the
criterion asks for neither. Every row lands in its own table with a
`_quarantined` flag, and the two `quarantine` views are where somebody notices
them.

A view rather than a directory of rejected files, deliberately. A quarantine path
nobody queries is where bad records go to be forgotten; a table in the same
schema, under the same grants, is where they get looked at.

The predicate has two clauses, because one mechanism does not cover both
failures:

```sql
_rescued_data IS NOT NULL OR (<every identity column> IS NULL)
```

> ⚠️ **The rescued data column does not catch a document that failed to parse as
> a whole.** A truncated JSON file read with `multiLine` produces a row of nulls
> and an *empty* `_rescued_data`, indistinguishable from a legitimately sparse
> record — so a quarantine keyed on the rescued column alone lets a corrupt
> document through. Found by seeding one; see §5. The identity clause is the fix:
> a row with no `order_id`, no `requested_url`, nothing to call it by, did not
> arrive.

That is what `Source.identity` is for. Bronze enforces nothing with it — a
duplicate is a fact about the landing zone, not an error — but it is also what
makes the idempotence check below expressible.

> ⚠️ **`multiLine` is per source and its mistake is silent.** The harvest writes
> its pointers, its Document Intelligence results and the population manifest
> with `indent=2`, and its tables as one compact object per line. Reading either
> the wrong way does not error; it produces an entire file of rescued data.
> `test_bronze.py` asserts the flag against bytes the real writer produced rather
> than against a comment.

### Idempotence

Auto Loader records the files it has consumed in `cloudFiles.schemaLocation`, one
directory per table under `abfss://lakehouse@…/_autoloader/`, and never reads one
twice. So re-running the pipeline over an unchanged landing zone appends nothing.

`cloudFiles.allowOverwrites` is deliberately absent. Turning it on would
re-ingest a file rewritten at the same path, which is exactly the duplication the
criterion forbids.

> ⚠️ **The other half of that is a real gap.** The generator writes
> `accounts/synthetic/orders.jsonl` at a fixed path, so a *regenerated*
> population is invisible to bronze — the file name has not changed and Auto
> Loader will not read it again. Idempotence and freshness are the same mechanism
> pointing in opposite directions here. The population should land under a
> version-qualified prefix; tracked as its own issue.

Deleting a table's directory under `_autoloader/` is what makes that table
re-ingest everything, and `--full-refresh` does it for all of them.

## 4. Cost

`continuous = false`, `development = false`, no schedule.

A continuous pipeline holds a cluster open indefinitely, which is the trap
[#31](https://github.com/gganssle/chip_chat/issues/31) exists to close.
Development mode deliberately keeps the cluster alive after an update so the next
one starts faster; both are the wrong default under a $150/month ceiling.

The cluster is single-node under `chip-chat-pipeline-single-node`. Pipeline
compute has no `autotermination_minutes` and rejects one — termination there is
structural rather than a timeout, because there is no cluster once the update
ends. The header of `databricks_compute.tf` carries the full argument.

> ⚠️ **A cluster policy is not usable by the principal a pipeline runs as until
> it is granted.** Creating the pipeline succeeds and `terraform apply` reports no
> drift; the *update* fails two seconds in with `PERMISSION_DENIED: You are not
> authorized to access this cluster policy`, which reads like the policy is broken
> rather than like a grant is missing. The jobs principal had held `CAN_USE` on
> the *job* policy since #31 and nothing implied it on the pipeline one. Verified
> 2026-08-26; the grant is `databricks_permissions.pipeline_policy_usage`.

## 5. Checking it, rather than believing it

Two commands. The second reads what the first writes.

```bash
cd infra/terraform
databricks pipelines start-update $(terraform output -raw databricks_bronze_pipeline_id)
databricks jobs run-now $(terraform output -raw databricks_bronze_verify_job_id)
```

`chip-chat-bronze-verify` runs `databricks/notebooks/bronze_verify.py` on
single-node job compute as the jobs principal, reads and never writes, and
asserts its own result — SUCCESS means the claims held rather than that the
notebook finished. It returns its counts as the run's notebook output, so a run
can be quoted without opening the workspace.

The landing zone permanently holds two deliberately malformed documents under
`raw/index/zz/`: one truncated pointer and one whose `status_code` is the string
`"two hundred"`. `zz` is not a hex digest shard, so neither can ever collide with
a harvested document. They stay there for the same reason #32's `lineage_probe`
tables do — the quarantine is only observable if something is in it, and a
criterion you have to set up before you can check is a criterion nobody checks.
So `expect_quarantined` defaults to `true`, and an empty quarantine means the
mechanism stopped working rather than that the corpus is clean.

Silver must filter `NOT _quarantined`, which it would have to do anyway.

## 6. What it did when it was run

2026-08-26, against `dbw-chip-chat`, catalog `chip_chat`.

The landing zone was seeded from the committed harvest and catalogue fixtures
through the real writers — `chip_chat.harvest`, `chip_chat.catalog` and
`chip_chat.data_gen` — rather than from a live harvest, which would have made
paid Document Intelligence calls that #22 has already made once. The population
is the full one: 500 customers, the shipped `population.toml`, 18 months.

| Run | Result |
| --- | --- |
| Pipeline update `ff4e1703`, `--full-refresh` (cold start) | **COMPLETED**, 333 s wall clock — 289 s of it waiting for the VM |
| Pipeline update `11f2087d` (unchanged landing zone) | **COMPLETED**, 157 s |
| Verify job run `831364935495820` | **SUCCESS** — 10 tables, both streams, no identity twice |
| Pipeline update `4792873c` (two malformed documents seeded) | **COMPLETED**, 136 s |
| Verify job run `416167014729058`, `expect_quarantined=true` | **SUCCESS** — 2 rows quarantined |

What landed:

```
       1  chip_chat.bronze_harvested.document_analyses
      79  chip_chat.bronze_harvested.raw_bodies
      84  chip_chat.bronze_harvested.raw_documents
     500  chip_chat.bronze_synthetic.demo_visitors
   32234  chip_chat.bronze_synthetic.loyalty_ledger
   48767  chip_chat.bronze_synthetic.order_items
   18898  chip_chat.bronze_synthetic.orders
      28  chip_chat.bronze_synthetic.persona_fixtures
       7  chip_chat.bronze_synthetic.personas
       1  chip_chat.bronze_synthetic.population_manifest
```

Every synthetic count is the generator's own, to the row. `raw_documents` is 84
rather than 82 because the two malformed documents are counted: they landed,
flagged, rather than being dropped.

Against the four acceptance criteria:

- **Both streams land from a cold start.** The `--full-refresh` update rebuilt
  every table from an empty state; all ten hold rows, in both schemas.
- **Re-running is idempotent.** A second update over the unchanged landing zone
  changed no count, and `COUNT(*)` equals `COUNT(DISTINCT identity)` on all ten
  tables.
- **A deliberately malformed input lands in the quarantine and the job still
  completes.** Update `4792873c` completed with both malformed documents in the
  landing zone; both are in `bronze_harvested.quarantine`, and both are still in
  `raw_documents` with `_quarantined = true`.
- **The full ingestion runs inside the auto-terminate window.** The longest
  update was 333 s end to end, of which 289 s was waiting for a VM. There is no
  window to exceed: pipeline compute is torn down when the update ends.

## 7. What this does not do

- **No silver.** [#34](https://github.com/gganssle/chip_chat/issues/34) cleans,
  deduplicates and conforms both streams. It also decided how the parsed and
  catalogue tables reach the lakehouse, and the answer was "through this
  pipeline" — see §1. `docs/silver-conformance.md` is that layer's write-up.
- **No schedule.** [#38](https://github.com/gganssle/chip_chat/issues/38) has
  since argued the weekly re-harvest and the freshness signal, and put the
  schedule in GitHub Actions rather than here — the harvest is a
  politeness-gated crawl with no Spark work in it, and it has nowhere in ADLS
  to land until `cc-j92` closes. Nothing in this workspace can still start
  spending on its own. See [corpus-freshness.md](corpus-freshness.md).
- **No harvest into ADLS.** The deployed harvest still writes to a local
  directory; the landing zone is uploaded. That is a gap in the path #18–#25
  built, not in this pipeline, and it is filed as its own issue.
- **No chunking.** [#35](https://github.com/gganssle/chip_chat/issues/35) takes
  the corpus out of bronze and structures it for retrieval.
