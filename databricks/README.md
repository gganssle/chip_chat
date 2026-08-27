# databricks

The lakehouse: a medallion pipeline in Unity Catalog that turns two harvested
sources into a chunked knowledge index and four personalization marts, plus the
nightly hand-off that publishes those marts to Snowflake. Issues
[#31](https://github.com/gganssle/chip_chat/issues/31)–[#37](https://github.com/gganssle/chip_chat/issues/37)
and [#39](https://github.com/gganssle/chip_chat/issues/39), RFC-001 §04 and §05.

This is the slow clock. Nothing here is on a turn's critical path: the marts a
visitor's personalization lane reads were computed last night, and the corpus
their menu question searches was chunked and published the same way.

## The one thing that shapes every file here

**Notebooks are loops; the decisions live in `src/`.** Every pipeline notebook is
close to empty — every decision that is not "call Spark" lives in
`chip_chat.databricks.<x>`, where the tests read it without a cluster. That is
why `databricks/tests/` is 5,800 lines against a `notebooks/` directory that
mostly reads a table and writes another one.

**And every module in `src/chip_chat/databricks/` imports nothing but the
standard library, which is load-bearing rather than stylistic.** A Lakeflow
pipeline runs a *notebook in the workspace*, not an installed wheel, so Terraform
uploads the same source file beside the notebook and the notebook puts its
directory on `sys.path`. Each module therefore has to import two ways: as
`chip_chat.databricks.silver` under pytest, and as a flat top-level `silver` on
the driver. Two consequences a reader will otherwise file as sloppiness:

- Constants are **re-spelled** across `bronze.py`, `silver.py` and `catalog.py`
  rather than imported, and the tests assert the copies are equal.
- `publish.py` **transcribes** Snowflake's column lists rather than importing
  `chip_chat.snowflake.schema`, and `test_publish.py` holds the transcription to
  the original.

One file is exempt in the other direction. `recommender_model.py` is the only
file that imports `mlflow`, uses a flat `import recommender`, and is
**deliberately not importable in CI** — `test_recommender.py` reads it as *text*.
The same trick the repository uses on the Azure Functions host and the Terraform.

## The map

| Module | Holds |
|---|---|
| `catalog.py` | The medallion layout, as data. Six schemas: three layers × two streams. |
| `bronze.py` | What lands in bronze, and the Auto Loader options that make a re-run idempotent (#33). |
| `silver.py` | What silver keeps, what it discards, and what it refuses outright (#34). |
| `gold_chunks.py` | Structure-aware chunking of the corpus — one menu item is one chunk, carrying its nutrition and allergens as metadata (#35). |
| `gold.py` | The four personalization marts (#36). |
| `recommender.py` | The item-affinity recommender: scoring, thresholds, and the holdout it has to beat a popularity baseline on (#37). |
| `recommender_model.py` | The `mlflow.pyfunc` wrapper. The only file here that knows MLflow exists. |
| `publish.py` | The nightly Databricks→Snowflake hand-off, and the credit arithmetic it reports on every run (#39). |

`catalog.py` is worth one more sentence, because it is where a boundary this
project cares about is made structural. `STREAMS = ("harvested", "synthetic")`,
and `schema(layer, stream)` takes the stream as a **required** argument — so a
caller has to say which population it is writing to before it can name a table.
Real published menu data and entirely synthetic accounts never share a schema,
and the code makes mixing them a thing you have to type on purpose. Unity Catalog
itself is created by `infra/terraform/databricks_catalog.tf`; this module makes
nothing, and `test_catalog_layout.py` asserts the two agree.

## `notebooks/`

Sixteen files, in three kinds. **Pipeline sources** (`bronze_ingest`,
`silver_conform`, `gold_chunk`, `gold_marts`) are the bodies of the four Lakeflow
pipelines. **Verify notebooks** (`bronze_verify`, `silver_verify`, `gold_verify`,
`gold_chunk_verify`, `recommender_verify`, `publish_verify`) are each issue's
acceptance criteria written as assertions and run as a job, so "criterion 3
passed" is a job run rather than an opinion. **Probes** (`adls_smoke`,
`lineage_probe`, `readonly_denied`) answer the Phase 2 questions about Unity
Catalog reaching ADLS, lineage surviving raw file → gold mart, and a read-only
principal being refused a write.

They are ruff-ignored for `F821` — `spark` and `dbutils` are injected by the
driver — and mypy excludes the directory entirely.

## Running it

**There is no `make` target that touches this directory.** Everything here is
deployed by Terraform and run by the Databricks CLI, which is deliberate: a job
that needs a workspace login is not a gate, and `make ci` has to run on a laptop
with no cloud at all. What `make` does cover is the part that matters most —
`make test` runs 5,800 lines of tests against every decision in `src/`, without a
cluster.

```bash
# Everything below needs `databricks current-user me` to work first.
databricks pipelines start-update $(terraform -chdir=infra/terraform output -raw databricks_bronze_pipeline_id)
databricks jobs run-now           $(terraform -chdir=infra/terraform output -raw databricks_bronze_verify_job_id)
make infra-output                 # every pipeline and job id, by name
```

The outputs follow one naming scheme: `databricks_{bronze,silver,gold,gold_chunk}_pipeline_id`
for the four pipelines, `databricks_*_verify_job_id` for each verify job, and
`databricks_{smoke,lineage,readonly,recommender,publish}_job_id` for the rest.

**Compute posture, because this is the easiest place in the project to burn a
month of credits.** Single-node job clusters, `Standard_F4ads_v7`, auto-terminate
at ten minutes, and **no all-purpose cluster** — verified against the deployed
workspace in `docs/cost.md` §14, where every cluster is `TERMINATED` and every one
is `JOB` or `PIPELINE` sourced. What actually costs money is the NAT gateway in
the Databricks-managed resource group, at $36.50 a month for existing; `docs/cost.md`
§6.3 has that argument.

## The write-ups

Read these for the decisions; this file is the map.

[docs/lakehouse-catalog.md](../docs/lakehouse-catalog.md) ·
[docs/bronze-ingestion.md](../docs/bronze-ingestion.md) ·
[docs/silver-conformance.md](../docs/silver-conformance.md) ·
[docs/corpus-chunking.md](../docs/corpus-chunking.md) ·
[docs/gold-marts.md](../docs/gold-marts.md) ·
[docs/recommender.md](../docs/recommender.md) ·
[docs/nightly-publish.md](../docs/nightly-publish.md)
