# Gold marts (gh-36): one Lakeflow Spark Declarative Pipeline that computes the
# four personalization marts of RFC-001 §04 out of silver.
#
# `databricks_bronze.tf` made the pipeline that lands what arrived and
# `databricks_silver.tf` the one that decides what is true. This one makes the
# pipeline that decides what is *served* -- and it is the lane that earns
# Databricks its place in this architecture. Snowflake is the governed
# low-latency store the agent hits every turn; Databricks is the batch engine
# that computes overnight what would be far too slow to compute mid-conversation.
# `item_affinity` is a self-join over every order in the population, and nobody
# is waiting on it inside a chat turn.
#
# --- WHY THIS IS A THIRD PIPELINE -------------------------------------------
#
# The same argument `databricks_silver.tf` makes, one layer up. A pipeline
# named for one layer would own two; its `schema` default, its table properties
# and its event log would all name silver; and re-deriving a mart -- which is
# the thing that changes here, because a confidence definition or an affinity
# threshold is a product decision -- would mean re-conforming the corpus first.
#
# The cost is one extra single-node cluster start on a MANUAL trigger.
# `continuous = false` closes the always-on trap exactly as it does in the two
# pipelines above.
#
# --- WHAT IS NOT HERE -------------------------------------------------------
#
# No schedule. #38 argues the weekly re-harvest and nothing in this workspace
# should be able to start spending on its own. No checkpoint, for silver's
# reason: these are materialized views and every update recomputes them in
# full, which is what lets a mart re-derive a row a late-arriving order changed.
# And no publish to Snowflake -- #39 owns the nightly hand-off, and this
# pipeline's job ends when the mart is correct in Unity Catalog.

locals {
  gold_lib_path = local.bronze_lib_path
}

# --- The declarations, as a workspace file ----------------------------------
#
# `bronze.py`, `catalog.py` and `silver.py` are already uploaded into this
# directory by the two files above, and this pipeline imports `catalog` and
# `gold`. `gold.py` joins them there rather than in a directory of its own, for
# the reason `silver.py` did: two `sys.path` entries to hold four files that
# always travel together is one more moving part than the arrangement needs.
#
# It is stdlib-only, and that is load-bearing. `gold.py` holds the SQL for all
# four marts, and a module that needed a cluster library to be read would make
# this upload insufficient.

resource "databricks_workspace_file" "gold_module" {
  path   = "${local.gold_lib_path}/gold.py"
  source = "${path.module}/../../databricks/src/chip_chat/databricks/gold.py"
}

resource "databricks_notebook" "gold_marts" {
  path     = "/Shared/${local.base}/gold_marts"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/gold_marts.py"
}

# --- The pipeline ------------------------------------------------------------

resource "databricks_pipeline" "gold" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name = "${local.base}-gold-marts"

  # All four marts are visitor-scoped or population-scoped facts about the
  # generated accounts, so unlike bronze and silver this pipeline publishes into
  # exactly one schema. The notebook still qualifies every name, because a
  # default that is correct today is a default somebody relies on tomorrow.
  catalog = databricks_catalog.main[0].name
  schema  = "gold_synthetic"

  continuous  = false
  development = false
  photon      = false

  channel = "CURRENT"

  run_as {
    service_principal_name = databricks_service_principal.jobs.application_id
  }

  cluster {
    label = "default"

    policy_id    = databricks_cluster_policy.pipeline_single_node.id
    node_type_id = var.databricks_node_type
    num_workers  = 0

    spark_conf = {
      "spark.databricks.cluster.profile" = "singleNode"
      "spark.master"                     = "local[*]"
    }

    custom_tags = {
      "ResourceClass" = "SingleNode"
      "project"       = "chip_chat"
      "issue"         = "gh-36"
    }
  }

  configuration = {
    "chip_chat.catalog"  = databricks_catalog.main[0].name
    "chip_chat.lib_path" = "/Workspace${local.gold_lib_path}"
  }

  library {
    notebook {
      path = databricks_notebook.gold_marts.path
    }
  }

  depends_on = [
    databricks_permissions.pipeline_policy_usage,
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.gold_module,
    databricks_grants.medallion,
    databricks_grants.catalog,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "gold_pipeline" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  pipeline_id = databricks_pipeline.gold[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_RUN"
  }

  # The app tier can start an update and read its state, with its managed
  # identity and no stored credential -- the same grant bronze and silver have,
  # so that #38's weekly re-harvest can run the three in sequence.
  access_control {
    service_principal_name = databricks_service_principal.app.application_id
    permission_level       = "CAN_RUN"
  }
}

# --- The proof ---------------------------------------------------------------
#
# #36's five acceptance criteria are claims about a live system, so they are a
# job rather than a screenshot -- the same shape as `bronze_verify` and
# `silver_verify`.
#
# It runs on job compute rather than inside the pipeline for those two jobs'
# reason and for one this issue adds: the fifth criterion is that the marts
# rebuild deterministically from the same silver input, which means running the
# pipeline's own query again and comparing it to what the pipeline published. A
# dataset inside the pipeline cannot assert about the table it *is*.
#
# Run it after the pipeline:
#
#   databricks pipelines start-update $(terraform output -raw databricks_gold_pipeline_id)
#   databricks jobs run-now $(terraform output -raw databricks_gold_verify_job_id)

resource "databricks_notebook" "gold_verify" {
  path     = "/Shared/${local.base}/gold_verify"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/gold_verify.py"
}

resource "databricks_job" "gold_verify" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name        = "${local.base}-gold-verify"
  description = "Asserts issue #36's acceptance criteria against the gold marts: all four built and matching RFC-001 section 04's schema exactly, a known customer's usual order returned correctly against issue #26's independent measurement, the confidence calibrated so the Regular is stated and the Explorer is not, derived_at on every row, and a rebuild from the same silver input reproducing every column but the timestamp. Read-only. Manual trigger only."

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
        "issue"         = "gh-36"
      }
    }
  }

  task {
    task_key        = "verify"
    job_cluster_key = "single-node"

    notebook_task {
      notebook_path = databricks_notebook.gold_verify.path
      base_parameters = {
        catalog  = databricks_catalog.main[0].name
        lib_path = "/Workspace${local.gold_lib_path}"
      }
    }
  }

  depends_on = [
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.gold_module,
    databricks_permissions.job_policy_usage,
    databricks_grants.medallion,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "gold_verify_job" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  job_id = databricks_job.gold_verify[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }
}
