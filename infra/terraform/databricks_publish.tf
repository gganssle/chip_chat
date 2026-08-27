# The nightly publish (gh-39): the job that carries the lakehouse into Snowflake,
# the secret scope holding the credential it does it with, and the job that
# asserts the acceptance criteria against the live serving layer.
#
# This is the seam between the two clocks. `databricks_gold.tf` made the pipeline
# that decides what is served and `databricks_recommender.tf` the thing that
# decides what is suggested; both of those end when the table is correct in Unity
# Catalog, and each says so in its own header. This file is where "correct in
# Unity Catalog" becomes "queryable in Snowflake on the conversational path".
#
# --- WHY A JOB AND NOT A PIPELINE -------------------------------------------
#
# `databricks_recommender.tf`'s reason, and one this issue adds. A Lakeflow
# declarative pipeline declares tables inside Unity Catalog; there is nowhere in
# that model to put "and then write it into another account". The publish is also
# imperative in a way a mart is not: stage, check the count, swap, check again,
# drop the staging table -- with the order of those five being the whole
# atomicity argument.
#
# One task, not two. The recommender splits train and publish because they fail
# for different reasons and are worth retrying separately. Here every table fails
# for the same reason -- the far side is unreachable, or a projection is wrong --
# and a partial retry would republish tables that already landed.
#
# --- THE SCHEDULE ------------------------------------------------------------
#
# Declared with a cron a person can read, and PAUSED unless
# `var.databricks_publish_schedule_enabled` says otherwise. The same arrangement
# `databricks_recommender.tf` argues at length: the criterion is about the
# publish being scheduled infrastructure, and the guardrail is about this
# Terraform not starting a cluster nobody asked for. `publish_verify.py` reads
# the schedule back off the Jobs API and fails if there is none, so "nightly"
# cannot quietly become "manual".
#
# 07:00 UTC. After #38's weekly re-harvest (07:00 Monday) has had an hour, and
# before the recommender's 09:00 Monday retrain -- so a Monday runs harvest,
# publish, retrain in that order rather than publishing a catalogue the marts
# were not computed against.
#
# --- THE ALERT, WHICH IS PART OF THIS TICKET AND NOT A FOLLOW-UP -------------
#
# RFC-001 §10 is specific: when the Databricks job fails, serve stale gold marts
# with their `derived_at`, ALERT, and do not silently serve stale data as fresh.
# The issue says to implement the alert here rather than later.
#
# It is `email_notifications.on_failure` on the job and not a line in the
# notebook, and the difference matters: a run that dies before reaching any line
# of `snowflake_publish.py` -- a cluster that would not start, a workspace file
# that is not there, a task killed at its timeout -- is exactly the run nobody
# hears about otherwise. The notebook's job is to fail loudly enough that the
# mail says something useful.
#
# The stale half needs no infrastructure at all, and that is the design working:
# the swap is one `INSERT OVERWRITE` per table, so a failed run leaves the
# previous generation in place, and the publish carries `derived_at` across
# unchanged rather than restamping it. Yesterday's mart therefore reports the
# night it was computed. `databricks/src/chip_chat/databricks/publish.py` carries
# both arguments in full.
#
# One retry, because a JDBC connection that was refused once is worth trying
# again before it wakes somebody, and one is where that stops being true.
#
# --- THE CREDENTIAL ----------------------------------------------------------
#
# A Databricks-backed secret scope, created EMPTY here and filled by an operator
# with the CLI. No private key enters Terraform state, which is the same
# argument `snowflake/sql/04_users.sql` makes about `RSA_PUBLIC_KEY` never
# appearing in the checked-in SQL: a credential in a state file is a credential
# in the storage account, in every plan output, and in whoever has read access.
#
#   databricks secrets put-secret chip-chat-snowflake publisher-private-key \
#     --string-value "$(cat ~/.snowflake/keys/chip_chat_publisher.p8)"
#
# docs/nightly-publish.md §3 has the whole sequence, key pair included.
#
# --- WHAT IS NOT HERE -------------------------------------------------------
#
# No reverse path. Nothing is read out of Snowflake into the lakehouse, and the
# marts are computed from silver rather than from what was published -- so a
# publish that landed wrong cannot become an input to the next one.
#
# No `demo_visitors`. #47 owns returning the sandbox to its generated state, and
# the three columns a visitor edits live in a table this job cannot write. The
# publish carries orders, order_items and loyalty_ledger, which is
# `schema.MART_INPUTS`, and `snowflake/sql/03_grants.sql` grants exactly those
# three by name.

locals {
  publish_lib_path = local.bronze_lib_path
}

# --- The declarations, as a workspace file ----------------------------------
#
# `publish.py` is stdlib-only and joins `bronze.py`, `catalog.py`, `silver.py`,
# `gold.py` and `recommender.py` in the shared lib directory, for the reason
# `gold.py` joined them: modules that always travel together do not need a
# `sys.path` entry each.

resource "databricks_workspace_file" "publish_module" {
  path   = "${local.publish_lib_path}/publish.py"
  source = "${path.module}/../../databricks/src/chip_chat/databricks/publish.py"
}

resource "databricks_notebook" "snowflake_publish" {
  path     = "/Shared/${local.base}/snowflake_publish"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/snowflake_publish.py"
}

resource "databricks_notebook" "publish_verify" {
  path     = "/Shared/${local.base}/publish_verify"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/publish_verify.py"
}

# --- Where the credential goes ----------------------------------------------
#
# `initial_manage_principal` is not set, so the scope is manageable by its
# creator -- the identity running Terraform -- and by nobody else. The jobs
# service principal is given READ below and never MANAGE: a job that could
# rewrite the secret it authenticates with is a job that can lock itself out,
# and there is no reason for it to be able to.

resource "databricks_secret_scope" "snowflake" {
  name = var.databricks_publish_secret_scope
}

resource "databricks_secret_acl" "snowflake_jobs" {
  scope      = databricks_secret_scope.snowflake.name
  principal  = databricks_service_principal.jobs.application_id
  permission = "READ"
}

# --- The job -----------------------------------------------------------------

resource "databricks_job" "publish" {
  count = var.databricks_unity_catalog_enabled && var.snowflake_account_url != "" ? 1 : 0

  name        = "${local.base}-publish"
  description = "Publishes the harvested catalogue, the three synthetic account tables the marts are computed from, and the four gold marts, out of Unity Catalog and into Snowflake. Each table lands in CHIP_CHAT.STAGING and is made live by one INSERT OVERWRITE, so a conversation querying mid-publish sees last night's generation or tonight's and never half of either. derived_at is carried across unchanged, never restamped. Nightly. gh-39."

  timeout_seconds     = var.databricks_publish_timeout_seconds
  max_concurrent_runs = 1

  run_as {
    service_principal_name = databricks_service_principal.jobs.application_id
  }

  # Quartz, because that is what the Jobs API takes: seconds first, and a `?` in
  # whichever of day-of-month and day-of-week is not being used.
  schedule {
    quartz_cron_expression = var.databricks_publish_cron
    timezone_id            = "UTC"
    pause_status           = var.databricks_publish_schedule_enabled ? "UNPAUSED" : "PAUSED"
  }

  # The alert. RFC-001 §10 requires it and #39 requires it in this ticket rather
  # than a follow-up. on_failure covers a run that never reached the notebook,
  # which is the run that would otherwise be silent.
  email_notifications {
    on_failure = [var.databricks_publish_alert_email]
  }

  # A skipped run is a run that did not happen and a cancelled one is a person
  # deciding, so neither is a failure -- but both mean the marts are a day
  # older, and RFC-001 §10 is about nobody being surprised by that. Left at the
  # default (alerting) rather than turned off.
  notification_settings {
    no_alert_for_skipped_runs  = false
    no_alert_for_canceled_runs = false
  }

  job_cluster {
    job_cluster_key = "single-node"

    new_cluster {
      policy_id     = databricks_cluster_policy.job_single_node.id
      spark_version = var.databricks_spark_version
      node_type_id  = var.databricks_node_type
      num_workers   = 0

      data_security_mode = "SINGLE_USER"
      single_user_name   = databricks_service_principal.jobs.application_id

      spark_conf = {
        "spark.databricks.cluster.profile" = "singleNode"
        "spark.master"                     = "local[*]"
      }

      custom_tags = {
        "ResourceClass" = "SingleNode"
        "project"       = "chip_chat"
        "issue"         = "gh-39"
      }
    }
  }

  task {
    task_key        = "publish"
    job_cluster_key = "single-node"

    # One retry and no more. A refused JDBC connection is worth trying again
    # before it wakes somebody; a projection that does not compile is not, and
    # retrying it only delays the mail.
    max_retries               = 1
    min_retry_interval_millis = 60000
    retry_on_timeout          = false

    notebook_task {
      notebook_path = databricks_notebook.snowflake_publish.path
      base_parameters = {
        catalog        = databricks_catalog.main[0].name
        lib_path       = "/Workspace${local.publish_lib_path}"
        snowflake_url  = var.snowflake_account_url
        snowflake_user = var.snowflake_publisher_user
        secret_scope   = databricks_secret_scope.snowflake.name
      }
    }
  }

  depends_on = [
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.publish_module,
    databricks_secret_acl.snowflake_jobs,
    databricks_permissions.job_policy_usage,
    databricks_grants.medallion,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "publish_job" {
  count = length(databricks_job.publish) > 0 ? 1 : 0

  job_id = databricks_job.publish[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }

  # The app tier may start a publish with its managed identity and no stored
  # credential -- the same grant the three pipelines and the recommender have,
  # so that a rebuild after a re-harvest can run the whole chain in sequence.
  # `CAN_MANAGE_RUN` because this is a job and not a pipeline; see the note on
  # `databricks_permissions.recommender_job` for why the two spellings differ.
  access_control {
    service_principal_name = databricks_service_principal.app.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }
}

# --- The proof ---------------------------------------------------------------
#
# #39's acceptance criteria are claims about two live systems at once, so they
# are a job rather than a screenshot -- the same shape as `gold_verify` and
# `recommender_verify`, and separate from the publish for their reason: a check
# that runs only as part of the thing it checks cannot be run to ask whether the
# thing is still true.
#
# It is read-only on both sides and safe at any time. Two of its assertions are
# about the publish job itself -- its schedule and its failure notification --
# which it reads off the Jobs API rather than off this file.
#
# Run them in order:
#
#   databricks jobs run-now $(terraform output -raw databricks_publish_job_id)
#   databricks jobs run-now $(terraform output -raw databricks_publish_verify_job_id)

resource "databricks_job" "publish_verify" {
  count = var.databricks_unity_catalog_enabled && var.snowflake_account_url != "" ? 1 : 0

  name        = "${local.base}-publish-verify"
  description = "Asserts issue #39's acceptance criteria against the live serving layer: every published table holds what the lakehouse produced, no primary key is duplicated and CHIP_CHAT.STAGING is empty, the publish job carries a cron schedule and a failure notification addressed to a human, and derived_at on every mart row is the gold pipeline's own timestamp rather than the publish's. Read-only. Manual trigger only."

  timeout_seconds     = var.databricks_uc_probe_timeout_seconds
  max_concurrent_runs = 1

  run_as {
    service_principal_name = databricks_service_principal.jobs.application_id
  }

  job_cluster {
    job_cluster_key = "single-node"

    new_cluster {
      policy_id     = databricks_cluster_policy.job_single_node.id
      spark_version = var.databricks_spark_version
      node_type_id  = var.databricks_node_type
      num_workers   = 0

      data_security_mode = "SINGLE_USER"
      single_user_name   = databricks_service_principal.jobs.application_id

      spark_conf = {
        "spark.databricks.cluster.profile" = "singleNode"
        "spark.master"                     = "local[*]"
      }

      custom_tags = {
        "ResourceClass" = "SingleNode"
        "project"       = "chip_chat"
        "issue"         = "gh-39"
      }
    }
  }

  task {
    task_key        = "verify"
    job_cluster_key = "single-node"

    notebook_task {
      notebook_path = databricks_notebook.publish_verify.path
      base_parameters = {
        catalog        = databricks_catalog.main[0].name
        lib_path       = "/Workspace${local.publish_lib_path}"
        snowflake_url  = var.snowflake_account_url
        snowflake_user = var.snowflake_publisher_user
        secret_scope   = databricks_secret_scope.snowflake.name
        job_name       = databricks_job.publish[0].name
      }
    }
  }

  depends_on = [
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.publish_module,
    databricks_secret_acl.snowflake_jobs,
    databricks_permissions.job_policy_usage,
    databricks_grants.medallion,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "publish_verify_job" {
  count = length(databricks_job.publish_verify) > 0 ? 1 : 0

  job_id = databricks_job.publish_verify[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }
}
