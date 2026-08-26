# Databricks notebook source
# MAGIC %md
# MAGIC # ADLS smoke test
# MAGIC
# MAGIC Proof, rather than assurance, for two claims in issue #31:
# MAGIC
# MAGIC 1. Unity Catalog can reach the ADLS Gen2 landing zone through the storage
# MAGIC    credential — no account key, no SAS token, no secret in this notebook.
# MAGIC 2. The single-node job cluster this runs on terminates on its own
# MAGIC    afterwards, because the cluster policy will not let it be anything else.
# MAGIC
# MAGIC It writes a three-row Delta table, reads it back, checks the count, and
# MAGIC deletes it. Run it from the `chip-chat-adls-smoke` job, which supplies
# MAGIC `raw_uri`. It is deliberately not scheduled: nothing here should be able to
# MAGIC start spending on its own.

# COMMAND ----------

import time

dbutils.widgets.text("raw_uri", "", "abfss:// URI of the raw container")
dbutils.widgets.text("run_id", "", "Job run id, supplied by the job as {{job.run_id}}")

raw_uri = dbutils.widgets.get("raw_uri").rstrip("/")
if not raw_uri.startswith("abfss://"):
    raise ValueError(
        f"raw_uri must be an abfss:// URI on the /dfs endpoint, got {raw_uri!r}. "
        "wasbs:// would reach the same bytes without the hierarchical namespace, "
        "which is not what the lakehouse is built on."
    )

# A fresh path per run, so two runs cannot pass by reading each other's output.
# The job supplies the id as the dynamic value {{job.run_id}}; the fallback keeps
# the notebook runnable by hand.
run_id = dbutils.widgets.get("run_id") or "manual"
path = f"{raw_uri}/_smoke/{run_id}"
print(f"writing to {path}")

# COMMAND ----------

started = time.monotonic()

rows = [
    ("burrito", 1075),
    ("bowl", 1010),
    ("salad", 640),
]
df = spark.createDataFrame(rows, "item string, calories int")
df.write.format("delta").mode("overwrite").save(path)

write_seconds = time.monotonic() - started
print(f"wrote {df.count()} rows in {write_seconds:.1f}s")

# COMMAND ----------

read_back = spark.read.format("delta").load(path)
count = read_back.count()
read_back.show()

if count != len(rows):
    raise AssertionError(f"round trip lost rows: wrote {len(rows)}, read {count}")

# COMMAND ----------

# Leave the landing zone as we found it. The smoke marker is not data anyone
# downstream should ever see, and `raw` has no lifecycle rule to sweep it —
# the lifecycle policy in storage.tf deliberately covers `uploads` only.
dbutils.fs.rm(path, recurse=True)
print(f"ok: round-tripped {count} rows through {raw_uri} and cleaned up")
