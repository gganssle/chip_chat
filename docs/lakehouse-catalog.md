# The lakehouse catalogue

What Unity Catalog holds, who may write to it, and how to check that both are
still true. Issue [#32](https://github.com/gganssle/chip_chat/issues/32).

The governance layer was created before there were any tables in it. That is not
tidiness: ownership and grants are cheap to set on an empty catalog and tedious
to retrofit onto a populated one, because every object created in between
inherits whatever was true when it was made.

Everything below is `infra/terraform/databricks_catalog.tf`. Nothing here was
made by hand in the UI, which is the first of the issue's three acceptance
criteria and the reason the other two are jobs rather than screenshots.

## 1. The shape

```
chip_chat                       ← one catalog, managed storage in lakehouse/_catalog
├── bronze_harvested            ← real, as landed
├── bronze_synthetic            ← generated, as landed
├── silver_harvested            ← real, conformed
├── silver_synthetic            ← generated, conformed
├── gold_harvested              ← catalogue → Snowflake, chunks → AI Search
└── gold_synthetic              ← the four personalization marts → Snowflake
```

Each schema has its own managed storage root, so the boundary is visible with a
storage browser as well as with a SQL client:

```
abfss://lakehouse@stchipchat….dfs.core.windows.net/schemas/<schema>/
```

and each carries `layer` and `stream` in its `properties`, so the layout can be
asserted rather than parsed out of names:

```sql
SELECT schema_name, schema_owner FROM chip_chat.information_schema.schemata;
DESCRIBE SCHEMA EXTENDED chip_chat.gold_synthetic;
```

### Why six schemas and not three

Issue #32 asks for `bronze`, `silver` and `gold`, "with the two streams —
harvested web corpus and synthetic accounts — kept visibly separate within
them". Unity Catalog has exactly three levels: catalog, schema, table. There is
no fourth level to put a stream on, so *within a schema* can only mean a
table-naming convention — and a naming convention is invisible on the day this
issue ships, because there are no tables yet.

Making the stream a schema suffix costs a longer name and buys three things a
prefix cannot:

- **It is visible in an empty catalogue.** The boundary is the deliverable of
  this issue, so it has to be something you can see before #33 lands a row.
- **It is grantable.** A principal can be given the real catalogue and not the
  synthetic accounts. No table-name prefix can express that, and RFC-001 §05
  cares about exactly this kind of narrowing.
- **It is a property of the object**, not of a string. `properties.stream` is
  queryable.

RFC-001 §04: *"Keeping the boundary explicit in the schema keeps it explicit in
conversation."* This is that sentence, spelled as six schemas.

Gold is split like the other two rather than being one shared mart schema,
because both streams really do have a serving layer — the harvested side
publishes the versioned catalogue and the retrieval chunks, the synthetic side
publishes `customer_360`, `usual_order`, `item_affinity` and `spend_summary`. A
single `gold` would be the one place in the design where the two populations
share a namespace, which is the blur this layout exists to prevent.

`databricks/src/chip_chat/databricks/catalog.py` carries the same layout for the
pipelines in #33 and #34, and `databricks/tests/test_catalog_layout.py` fails
`make ci` if the two ever disagree. It is also uploaded verbatim into the
workspace and imported by the bronze pipeline, which is why it imports nothing
but the standard library — see [bronze-ingestion.md](bronze-ingestion.md) §1.

## 2. Who may do what

| Principal | Catalog | Schemas | `chip-chat-raw` external location |
| --- | --- | --- | --- |
| `chip-chat-jobs` | `USE_CATALOG` | `USE_SCHEMA`, `SELECT`, `MODIFY`, `CREATE_TABLE`, `CREATE_MATERIALIZED_VIEW`, `REFRESH` | `READ_FILES`, `WRITE_FILES`, `CREATE_EXTERNAL_TABLE` |
| `chip-chat-readonly` | `USE_CATALOG` | `USE_SCHEMA`, `SELECT` | `READ_FILES` |
| `chip-chat-app` | — | — | — |
| `account users` | — | — | — |

Four things about that table are deliberate.

**`CREATE_MATERIALIZED_VIEW` is not implied by `CREATE_TABLE`.** Lakeflow
declarative pipelines create materialized views and streaming tables, so #33 and
#34 would otherwise open by widening a grant.

**The app tier is absent.** RFC-001 §05 puts every per-turn read in Snowflake
behind a row access policy; the app never queries the lakehouse. A grant it does
not use would widen the trust boundary that section exists to keep narrow.

**`account users` is absent, and the absence is enforced.** Databricks grants
that group `ALL PRIVILEGES` on the legacy `main` catalog of a new metastore, and
it is the grant people copy without noticing. `databricks_grants` is
*authoritative* for the object it names — it replaces rather than adds — so
somebody who grants themselves something in the UI has it removed by the next
`terraform apply`. That is what "no ambient full access" means here.

**The reader holds `READ_FILES` and not `WRITE_FILES`.** The catalog is not the
only way out of a workspace. A principal that were read-only in the catalog but
could still write to the landing zone would not be read-only in any sense worth
verifying.

### Ownership

The catalog and its schemas are owned by whoever ran `terraform apply`, and that
is the smallest honest version of the arrangement rather than a good one. An
account-level *group* is the right owner. It cannot be created from this stack:

> ⚠️ **A Unity Catalog owner must be an account-level principal.** Setting
> `owner = "admins"` — the workspace admin group, which exists in every
> workspace — fails with `cannot create catalog: Could not find principal with
> name admins`. That reads like a typo and is not. `admins` and `users` are
> workspace-local groups; Unity Catalog resolves principals against the account.
> Verified on `dbw-chip-chat`, 2026-08-26.

Creating an account group needs a provider pointed at
`accounts.azuredatabricks.net` and an account admin to run it — the same wall
`system.access` and on-behalf-of tokens are behind. `var.databricks_catalog_owner`
is the seam: set it to that group once it exists and the catalog changes hands in
one apply.

## 3. Checking it, rather than believing it

Two jobs, neither scheduled. Nothing in this workspace should be able to start
spending on its own.

```bash
cd infra/terraform
databricks jobs run-now $(terraform output -raw databricks_lineage_job_id)
databricks jobs run-now $(terraform output -raw databricks_readonly_job_id)
```

Run them in that order — the second reads what the first writes. Both run on
single-node job compute under the `chip-chat-job-single-node` policy, so they
inherit the cost guardrail from #31 and cannot outlive themselves.

### `chip-chat-uc-lineage` — acceptance criterion 2

`databricks/notebooks/lineage_probe.py`, run as `chip-chat-jobs`. Writes one
small JSON document into the ADLS landing zone with `dbutils.fs.put` (not with
Spark, so the only lineage edge touching that path is the *read*), then:

```
abfss://raw@…/_lineage_probe/<run>/menu.json
  → chip_chat.bronze_harvested.lineage_probe     as landed, + _metadata.file_path
  → chip_chat.silver_harvested.lineage_probe     conformed to the §04 menu_items shape
  → chip_chat.gold_harvested.lineage_probe       aggregated: a mart
```

and then asks Unity Catalog to describe what happened. The assertion is on the
platform's answer, not on the code above it.

> ⚠️ **`system.access.table_lineage` is not available here.** The `system`
> catalog on this metastore has only `ai` and `information_schema`; enabling
> `system.access` is an *account*-admin action with no workspace API. The
> notebook uses `/api/2.0/lineage-tracking/table-lineage` instead, which is
> workspace-level and needs nothing turned on. Verified 2026-08-26.

> ⚠️ **That endpoint is a GET, not a POST.** The documentation shows a POST with
> a JSON body; a POST answers `404 ENDPOINT_NOT_FOUND: No API found for 'POST
> /lineage-tracking/table-lineage'`, which reads like the feature is off. The
> same path answers 200 to a GET with `table_name` and `include_entity_lineage`
> in the query string.

Lineage is recorded asynchronously, so the notebook polls for up to five minutes
rather than reading once and calling an early answer a failure.

The three `lineage_probe` tables are left in place, because lineage is a property
of objects that exist and dropping them would delete the evidence. Re-run with
`cleanup=true` to remove them.

Because it runs as the jobs principal on single-user compute, this job is also a
proof that the grants above are sufficient to build a medallion: every statement
in it is one #33 and #34 will issue.

### `chip-chat-uc-readonly-denied` — acceptance criterion 3

`databricks/notebooks/readonly_denied.py`, run as `chip-chat-readonly`. It first
*reads* — all six schemas visible, rows returned from the gold mart — because a
refusal proves nothing if the principal has no access at all; that would be a
different bug wearing the same error. Then five writes, each of which must be
refused:

| Attempt | Missing privilege |
| --- | --- |
| `INSERT INTO chip_chat.gold_harvested.lineage_probe` | `MODIFY` |
| `CREATE TABLE chip_chat.gold_synthetic.…` | `CREATE_TABLE` |
| `DROP TABLE chip_chat.gold_harvested.lineage_probe` | ownership |
| `CREATE SCHEMA chip_chat.…` | `CREATE_SCHEMA` |
| `dbutils.fs.put` into `abfss://raw@…` | `WRITE_FILES` |

The notebook fails if any of them succeeds, and also if one fails for a reason
that is *not* a permission refusal — a syntax error would otherwise let this
pass for the wrong reason.

## 4. What it did when it was run

2026-08-26, against `dbw-chip-chat`. Both jobs assert their own result, so
SUCCESS is the assertion passing rather than the notebook merely finishing.

| Job | Run | Result |
| --- | --- | --- |
| `chip-chat-uc-lineage` | `1113423536362313` | **SUCCESS** |
| `chip-chat-uc-readonly-denied` | `420068911637398` | **SUCCESS** — read returned rows, all five writes refused |

What Unity Catalog recorded, read back through the API:

```
abfss://raw@stchipchat….dfs.core.windows.net/_lineage_probe/<run>/menu.json
   [securable_type EXTERNAL_LOCATION, securable_name chip-chat-raw]
 → chip_chat.bronze_harvested.lineage_probe
 → chip_chat.silver_harvested.lineage_probe
 → chip_chat.gold_harvested.lineage_probe
```

The raw file appears as an upstream of the bronze table with its external
location named — which is the half of "from a raw file through to a gold mart"
that table-to-table lineage alone would not have shown.

## 5. What this does not do

- **No tables.** The `lineage_probe` tables are evidence, not data. #33 has
  since landed bronze from ADLS with Auto Loader — see
  [bronze-ingestion.md](bronze-ingestion.md) — and #34 builds silver and gold.
- **No MLflow model registry.** The recommender registered in Unity Catalog is
  later in Phase 3.
- **No Snowflake publish.** Gold is where this project's two halves meet, and
  the publish is its own issue.
- **No column masks or row filters.** RFC-001 §05 puts visitor isolation in
  Snowflake, on the serving side. The lakehouse holds whole populations, not
  per-visitor slices.
