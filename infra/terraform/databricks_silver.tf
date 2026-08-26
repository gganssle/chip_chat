# Silver conformance (gh-34): one Lakeflow Spark Declarative Pipeline that
# cleans, deduplicates and conforms both streams out of bronze.
#
# `databricks_bronze.tf` made the pipeline that lands what arrived. This one
# makes the pipeline that decides what is true. It reads bronze and never the
# landing zone, which is what keeps "bronze is what arrived" a property of the
# whole layer rather than of most of it.
#
# --- WHY THIS IS A SECOND PIPELINE ------------------------------------------
#
# Bolting silver onto `databricks_pipeline.bronze` would save one cluster start
# per full run, and this project counts cluster starts. It is still the wrong
# shape, for two reasons that outlast the saving:
#
#   * You could not re-conform without re-listing the landing zone. Silver is
#     the layer whose logic changes -- a boilerplate rule, a new expectation --
#     and iterating on it should not involve Auto Loader at all.
#   * A pipeline named for one layer would own two, and its `schema` default,
#     its table properties and its event log would all name bronze.
#
# The cost this actually adds is one extra single-node cluster start on a
# MANUAL trigger. The trap #31 exists to close is an always-on cluster, and
# `continuous = false` closes it here exactly as it does there.
#
# --- NO CHECKPOINT ----------------------------------------------------------
#
# There is no `chip_chat.checkpoint_uri` in the configuration below and that is
# not an omission. Silver's tables are materialized views: every update
# recomputes them from bronze in full. That is the correct semantics for a layer
# whose job is deduplication -- a duplicate arriving in a later update has to be
# able to displace the row already written, and an append-only stream cannot do
# that. Auto Loader's file ledger belongs to the layer that reads files.

locals {
  silver_lib_path = local.bronze_lib_path
}

# --- The declarations, as a workspace file ----------------------------------
#
# `bronze.py` and `catalog.py` are already uploaded by `databricks_bronze.tf`
# into the same directory, and this pipeline imports all three. `silver.py`
# joins them there rather than in a directory of its own, because two `sys.path`
# entries to hold three files that always travel together is one more moving
# part than the arrangement needs.

resource "databricks_workspace_file" "silver_module" {
  path   = "${local.silver_lib_path}/silver.py"
  source = "${path.module}/../../databricks/src/chip_chat/databricks/silver.py"
}

resource "databricks_notebook" "silver_conform" {
  path     = "/Shared/${local.base}/silver_conform"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/silver_conform.py"
}

# --- The pipeline ------------------------------------------------------------

resource "databricks_pipeline" "silver" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name = "${local.base}-silver-conform"

  # As in bronze: `schema` is the default target for an unqualified name, and
  # the notebook qualifies every one of them because one pipeline publishes into
  # both `silver_harvested` and `silver_synthetic`.
  catalog = databricks_catalog.main[0].name
  schema  = "silver_harvested"

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
      "issue"         = "gh-34"
    }
  }

  configuration = {
    "chip_chat.catalog"  = databricks_catalog.main[0].name
    "chip_chat.lib_path" = "/Workspace${local.silver_lib_path}"
  }

  library {
    notebook {
      path = databricks_notebook.silver_conform.path
    }
  }

  depends_on = [
    databricks_permissions.pipeline_policy_usage,
    databricks_workspace_file.bronze_module,
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.silver_module,
    databricks_grants.medallion,
    databricks_grants.catalog,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "silver_pipeline" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  pipeline_id = databricks_pipeline.silver[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_RUN"
  }

  # The app tier can start an update and read its state, with its managed
  # identity and no stored credential -- the same grant bronze has, so that #38's
  # weekly re-harvest can run the two in sequence.
  access_control {
    service_principal_name = databricks_service_principal.app.application_id
    permission_level       = "CAN_RUN"
  }
}

# --- The proof ---------------------------------------------------------------
#
# #34's three acceptance criteria are claims about a live system, so they are a
# job rather than a screenshot -- the same shape as `bronze_verify`.
#
# It runs on job compute rather than inside the pipeline for the reason bronze's
# does, and for one more: two of the three criteria are comparisons BETWEEN
# bronze and silver ("deduplication measurably reduces the corpus"), and a
# dataset inside the pipeline cannot assert about the layer it was built from
# without becoming part of it.
#
# Run it after the pipeline:
#
#   databricks pipelines start-update $(terraform output -raw databricks_silver_pipeline_id)
#   databricks jobs run-now $(terraform output -raw databricks_silver_verify_job_id)

resource "databricks_notebook" "silver_verify" {
  path     = "/Shared/${local.base}/silver_verify"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/silver_verify.py"
}

resource "databricks_job" "silver_verify" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name        = "${local.base}-silver-verify"
  description = "Asserts issue #34's acceptance criteria against the silver tables: both streams conformed with fatal expectations, deduplication that reduces the corpus and conserves every citation, and no surviving block of boilerplate. Read-only. Manual trigger only."

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
        "issue"         = "gh-34"
      }
    }
  }

  task {
    task_key        = "verify"
    job_cluster_key = "single-node"

    notebook_task {
      notebook_path = databricks_notebook.silver_verify.path
      base_parameters = {
        catalog  = databricks_catalog.main[0].name
        lib_path = "/Workspace${local.silver_lib_path}"
      }
    }
  }

  depends_on = [
    databricks_workspace_file.bronze_module,
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.silver_module,
    databricks_permissions.job_policy_usage,
    databricks_grants.medallion,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "silver_verify_job" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  job_id = databricks_job.silver_verify[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }
}
