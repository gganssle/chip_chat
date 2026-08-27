# Databricks notebook source
# MAGIC %md
# MAGIC # The publish — the acceptance criteria, as assertions
# MAGIC
# MAGIC Issue [#39](https://github.com/gganssle/chip_chat/issues/39) has five
# MAGIC acceptance criteria and four of them are claims about a live system. This
# MAGIC notebook is those four, run as the `chip-chat-publish-verify` job, so
# MAGIC SUCCESS means the assertions passed rather than the notebook merely
# MAGIC finishing. Same shape as `gold_verify.py` and `recommender_verify.py`,
# MAGIC and separate from the publish job for the reason those are separate from
# MAGIC the pipelines they check: a check that runs only as part of the thing it
# MAGIC checks cannot be run to ask whether the thing is still true.
# MAGIC
# MAGIC 1. **The nightly job publishes all marts and the catalogue.** All eleven
# MAGIC    tables exist in Snowflake and hold rows, the row counts equal the
# MAGIC    lakehouse tables they were published from, and the publish job carries
# MAGIC    a cron schedule read back off the Jobs API.
# MAGIC 2. **A killed mid-run job leaves a consistent, previous-generation
# MAGIC    state.** No primary key is duplicated in any published table — which
# MAGIC    is what a truncate that removed only *some* rows would produce — and
# MAGIC    `CHIP_CHAT.STAGING` is empty, because a staging table is dropped when
# MAGIC    its swap succeeds and left behind when it does not.
# MAGIC 3. **A failed run raises an alert that reaches a human.** The publish
# MAGIC    job's `email_notifications.on_failure` is read back off the Jobs API
# MAGIC    and must name at least one address.
# MAGIC 4. **`derived_at` is readable from Snowflake** — non-null on every mart
# MAGIC    row — **and is the gold mart's own timestamp**, not the publish's. The
# MAGIC    two are compared directly. A publish that restamped this column would
# MAGIC    present a mart republished from an unchanged gold table as fresh,
# MAGIC    which is the one outcome RFC-001 §10 names.
# MAGIC
# MAGIC The fifth — one full publish's cost in DBUs and credits — is a
# MAGIC measurement rather than an assertion. The publish job emits it in its own
# MAGIC output and `docs/nightly-publish.md` records it.
# MAGIC
# MAGIC Read-only on both sides. Safe to run at any time.
# MAGIC
# MAGIC **Parameters**, all supplied by the job: `catalog`, `lib_path`,
# MAGIC `snowflake_url`, `snowflake_user`, `secret_scope`, `job_name`.

# COMMAND ----------

import json
import sys

from databricks.sdk import WorkspaceClient

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding publish.py")
dbutils.widgets.text("snowflake_url", "", "Snowflake account URL")
dbutils.widgets.text("snowflake_user", "CHIP_CHAT_PUBLISHER", "Snowflake user")
dbutils.widgets.text("secret_scope", "chip-chat-snowflake", "Databricks secret scope")
dbutils.widgets.text("job_name", "", "Name of the nightly publish job")

sys.path.insert(0, dbutils.widgets.get("lib_path"))

import catalog  # noqa: E402
import publish  # noqa: E402

CATALOG = dbutils.widgets.get("catalog")
JOB_NAME = dbutils.widgets.get("job_name")
SCOPE = dbutils.widgets.get("secret_scope")

CONNECT = dict(
    publish.options(
        dbutils.widgets.get("snowflake_url"),
        dbutils.widgets.get("snowflake_user"),
        publish.STAGING_SCHEMA,
    ),
    pem_private_key=dbutils.secrets.get(scope=SCOPE, key=publish.PRIVATE_KEY_SECRET),
)

failures = []


def check(condition, message):
    """Record a failed claim rather than raising on the first one.

    A run that stops at the first failure tells you one thing per cluster
    start, and a cluster start here is four minutes and a few cents.
    """
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


def rows(query):
    """Return every row of a Snowflake query, read as CHIP_CHAT_PUBLISH."""
    return (
        spark.read.format(publish.SOURCE)
        .options(**CONNECT)
        .option("query", query)
        .load()
        .collect()
    )


def scalar(query):
    """Return the single value of a one-row, one-column Snowflake query."""
    return rows(query)[0][0]


# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1 — every table published, and the counts agree
# MAGIC
# MAGIC Counting on both sides rather than asking Snowflake whether the table is
# MAGIC there. A table that exists and holds last month's rows passes an
# MAGIC existence check and fails this one, and "the publish ran" is the claim
# MAGIC being made.
# MAGIC
# MAGIC The lakehouse count is taken from the same `publish.select` the job runs,
# MAGIC so a projection that dropped rows through a join would show up here as a
# MAGIC disagreement rather than as two matching wrong numbers.

# COMMAND ----------

counts = {}
for target in publish.TARGETS:
    source = catalog.table(target.layer, target.stream, target.table)
    expected = spark.sql(publish.select(target, catalog.table)).count()
    landed = scalar(f"SELECT COUNT(*) FROM {target.qualified}")
    counts[target.qualified] = landed
    check(landed > 0, f"{target.qualified} holds rows ({landed})")
    check(
        landed == expected,
        f"{target.qualified} holds what {source} produced "
        f"({landed} published, {expected} in the lakehouse)",
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1, second half — the publish is scheduled
# MAGIC
# MAGIC Read back off the Jobs API rather than off the Terraform, for
# MAGIC `recommender_verify.py`'s reason: the Terraform is what was declared and
# MAGIC this is what the workspace has. A job with **no** schedule is the
# MAGIC criterion failing. A job with a **paused** schedule is the shipped
# MAGIC default — `databricks_publish_schedule_enabled` turns it on, and
# MAGIC `infra/terraform/databricks_publish.tf` argues why it ships off.

# COMMAND ----------

workspace = WorkspaceClient()
jobs = list(workspace.jobs.list(name=JOB_NAME))
check(len(jobs) == 1, f"exactly one job is named {JOB_NAME!r} ({len(jobs)} found)")

settings = workspace.jobs.get(jobs[0].job_id).settings if jobs else None
schedule = settings.schedule if settings is not None else None
check(
    schedule is not None and bool(schedule.quartz_cron_expression),
    "the publish job carries a cron schedule"
    + (
        f": {schedule.quartz_cron_expression} {schedule.timezone_id}"
        if schedule is not None
        else ""
    ),
)
if schedule is not None:
    print(f"  the schedule is {schedule.pause_status}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 2 — a killed run leaves the previous generation
# MAGIC
# MAGIC Two live consequences of the design, both checkable without killing
# MAGIC anything.
# MAGIC
# MAGIC **No duplicated primary key.** `INSERT OVERWRITE` truncates and inserts
# MAGIC in one transaction, so the only way a published table can hold two rows
# MAGIC with one key is a truncate that removed some rows and not others. That is
# MAGIC exactly what a row access policy filtering `CHIP_CHAT_PUBLISH` would
# MAGIC produce, and it is the reason #43's policy must not.
# MAGIC
# MAGIC **`CHIP_CHAT.STAGING` is empty.** A staging table is dropped after its
# MAGIC swap succeeds, so anything still in there belongs to a run that stopped.
# MAGIC Finding one is not a failure of the last publish's *output* — the serving
# MAGIC tables are consistent either way — it is the evidence that a publish did
# MAGIC not finish, which is worth surfacing rather than tidying away.

# COMMAND ----------

for target in publish.TARGETS:
    key = ", ".join(target.key)
    duplicates = scalar(
        f"SELECT COUNT(*) FROM (SELECT {key} FROM {target.qualified} "
        f"GROUP BY {key} HAVING COUNT(*) > 1)"
    )
    check(
        duplicates == 0,
        f"{target.qualified} has one row per ({key}) -- {duplicates} keys repeat",
    )

leftover = [
    row[0]
    for row in rows(
        "SELECT table_name FROM "
        f"{publish.DATABASE}.INFORMATION_SCHEMA.TABLES "
        f"WHERE table_schema = '{publish.STAGING_SCHEMA}'"
    )
]
check(
    not leftover,
    f"{publish.DATABASE}.{publish.STAGING_SCHEMA} is empty"
    + (f" -- found {sorted(leftover)}, from a run that did not finish" if leftover else ""),
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 3 — a failed run reaches a human
# MAGIC
# MAGIC The alert cannot be the notebook's: a run that dies before reaching any
# MAGIC line of `snowflake_publish.py` still has to wake somebody, and that is
# MAGIC the job's `email_notifications.on_failure`. Read back off the Jobs API
# MAGIC for the same reason the schedule is.
# MAGIC
# MAGIC This checks the notification is declared and addressed. It does not check
# MAGIC that mail is delivered, which no job can — `docs/nightly-publish.md` has
# MAGIC the one-line way to prove that end to end.

# COMMAND ----------

notifications = settings.email_notifications if settings is not None else None
on_failure = list(notifications.on_failure or []) if notifications is not None else []
check(
    bool(on_failure),
    "the publish job emails somebody when a run fails"
    + (f": {', '.join(on_failure)}" if on_failure else ""),
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 4 — derived_at is there, and it is gold's
# MAGIC
# MAGIC Non-null on every mart row is the readable half. The half that is easy to
# MAGIC lose is that the value must be the one the *gold pipeline* computed: a
# MAGIC publish that stamped `current_timestamp()` would make a mart republished
# MAGIC from an unchanged gold table look like it was recomputed tonight, which
# MAGIC is stale data presented as fresh.
# MAGIC
# MAGIC So the two are compared. `max(derived_at)` on each side, to the second —
# MAGIC the column is `TIMESTAMP_NTZ` in Snowflake and a Spark timestamp in the
# MAGIC lakehouse, and a sub-second disagreement would be the string transport
# MAGIC losing precision rather than the publish restamping anything, which is a
# MAGIC different bug and one the transport tests cover.

# COMMAND ----------

for target in publish.targets_in("MARTS"):
    source = catalog.table(target.layer, target.stream, target.table)
    nulls = scalar(
        f"SELECT COUNT(*) FROM {target.qualified} WHERE derived_at IS NULL"
    )
    check(nulls == 0, f"{target.qualified} carries derived_at on every row")

    published_at = scalar(
        f"SELECT TO_CHAR(MAX(derived_at), 'YYYY-MM-DD HH24:MI:SS') "
        f"FROM {target.qualified}"
    )
    computed_at = spark.sql(
        f"SELECT date_format(max(derived_at), 'yyyy-MM-dd HH:mm:ss') FROM {source}"
    ).first()[0]
    check(
        published_at == computed_at,
        f"{target.qualified}.derived_at is the gold mart's own timestamp "
        f"(published {published_at}, computed {computed_at})",
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The verdict

# COMMAND ----------

print(f"\n{len(failures)} failures")
for failure in failures:
    print(f"  {failure}")

if failures:
    raise AssertionError(
        f"{len(failures)} of issue #39's acceptance criteria do not hold against "
        "the live serving layer:\n  " + "\n  ".join(failures)
    )

dbutils.notebook.exit(json.dumps({"checked": len(counts), "rows": counts}, sort_keys=True))
