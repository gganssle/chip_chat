# Gold chunking (gh-35): one Lakeflow Spark Declarative Pipeline that turns the
# conformed corpus into the retrievable units gh-48 indexes.
#
# `databricks_bronze.tf` made the pipeline that lands what arrived and
# `databricks_silver.tf` made the one that decides what is true. This one makes
# the pipeline that decides where a fact ENDS -- which RFC-001 §08 treats as the
# decision that determines whether allergen answers are trustworthy, and settles
# it as "at the boundary the publisher drew, never at a length".
#
# --- WHY THIS IS A THIRD PIPELINE -------------------------------------------
#
# The argument silver's header makes about bronze applies again, one layer up,
# and the second half of it applies harder:
#
#   * You could not re-chunk without re-conforming. Gold is now the layer whose
#     logic changes -- a renderer's wording, a new chunk kind, a metadata field
#     gh-48 turns out to need -- and iterating on it should not involve
#     recomputing twenty-four silver tables to find out whether a sentence reads
#     better.
#   * A pipeline named for one layer would own three, and its `schema` default,
#     its table properties and its event log would all name bronze.
#
# The cost is one more single-node cluster start on a MANUAL trigger. The trap
# #31 exists to close is an always-on cluster, and `continuous = false` closes it
# here exactly as it does in the other two.
#
# --- ONE TABLE, AND ONE SCHEMA IT LIVES IN ----------------------------------
#
# `gold_synthetic` exists -- `databricks_catalog.tf` creates all six schemas --
# and this pipeline writes nothing into it. That is not an oversight to be
# tidied up later. RFC-001 §04 holds the real catalogue and the invented account
# data apart, and the retrieval index is where blurring them would cost the
# most: a generated order that reached the index would be a fabricated fact with
# a real-looking citation on it. The way to keep that from happening is for
# there to be no code path that could take it there, which is why
# `chip_chat.databricks.gold.STREAM` is a constant rather than a loop variable.
#
# The gold marts the system design lists -- customer_360, usual_order,
# item_affinity, spend_summary -- are the synthetic stream's half of this layer
# and are a different issue's pipeline.

locals {
  gold_lib_path = local.bronze_lib_path
}

# --- The declarations, as a workspace file ----------------------------------
#
# `bronze.py`, `catalog.py` and `silver.py` are already uploaded into the same
# directory by the two pipelines below this one, and this pipeline imports
# `catalog`, `gold` and `silver`. `gold.py` joins them there for the reason
# `silver.py` did: three `sys.path` entries to hold four files that always
# travel together is three more moving parts than the arrangement needs.

resource "databricks_workspace_file" "gold_module" {
  path   = "${local.gold_lib_path}/gold.py"
  source = "${path.module}/../../databricks/src/chip_chat/databricks/gold.py"
}

resource "databricks_notebook" "gold_chunk" {
  path     = "/Shared/${local.base}/gold_chunk"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/gold_chunk.py"
}

# --- The pipeline ------------------------------------------------------------

resource "databricks_pipeline" "gold" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name = "${local.base}-gold-chunk"

  # `schema` is the default target for an unqualified name. The notebook
  # qualifies its one table anyway, as the other two pipelines do, so that the
  # fully qualified name in the source is the name in the catalogue.
  catalog = databricks_catalog.main[0].name
  schema  = "gold_harvested"

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
      "issue"         = "gh-35"
    }
  }

  configuration = {
    "chip_chat.catalog"  = databricks_catalog.main[0].name
    "chip_chat.lib_path" = "/Workspace${local.gold_lib_path}"
  }

  library {
    notebook {
      path = databricks_notebook.gold_chunk.path
    }
  }

  depends_on = [
    databricks_permissions.pipeline_policy_usage,
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.silver_module,
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
  # so that #38's weekly re-harvest can run the three in sequence and #48's index
  # rebuild can follow them.
  access_control {
    service_principal_name = databricks_service_principal.app.application_id
    permission_level       = "CAN_RUN"
  }
}

# --- The proof ---------------------------------------------------------------
#
# Two of #35's three acceptance criteria are already tests: `make ci` runs the
# chunker over the recorded nutrition sheet and the recorded catalogue, and
# `test_gold.py` runs the same assertions over a fixed-window chunker and
# requires them to FAIL. What CI cannot check is the live table, so this job is
# the other half -- and the third criterion, a person reading twenty chunks, is
# a thing this job puts in front of them rather than a thing it decides.
#
# It runs on job compute rather than inside the pipeline for the reason bronze's
# and silver's do, and for one more: the row-count comparisons are BETWEEN
# silver and gold ("eight extracted rows became eight chunks"), and a dataset
# inside the pipeline cannot assert about the layer it was built from without
# becoming part of it.
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
  description = "Asserts issue #35's acceptance criteria against the gold chunk table: the metadata schema as published, no nutrition table split across a chunk boundary, every chunk citable, and the deterministic sample of twenty the hand review reads. Read-only. Manual trigger only."

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
        "issue"         = "gh-35"
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
    databricks_workspace_file.silver_module,
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
