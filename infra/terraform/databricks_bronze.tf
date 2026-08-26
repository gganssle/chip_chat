# Bronze ingestion (gh-33): one Lakeflow Spark Declarative Pipeline carrying
# both streams out of the ADLS landing zone and into bronze.
#
# `databricks_catalog.tf` made the six schemas and the grants. This file makes
# the first thing that writes into them.
#
# --- WHY THERE IS A LIBRARY DIRECTORY HERE -----------------------------------
#
# A pipeline runs a NOTEBOOK on the driver. There is no wheel on the cluster, so
# `import chip_chat.databricks.bronze` would fail, and the alternative -- pasting
# the source list into the notebook -- would put the declarations somewhere no
# test can read them. So the two modules the notebook needs are uploaded as
# workspace files beside it and the notebook puts that directory on `sys.path`.
#
# That works only because both modules import nothing but the standard library,
# which is stated in the header of each and asserted by
# `databricks/tests/test_bronze.py`. The files uploaded are the very files pytest
# imports -- there is no second copy to drift.
#
# --- COST --------------------------------------------------------------------
#
# `continuous = false` and `development = false`. A continuous pipeline holds a
# cluster open indefinitely, which is the cost trap #31 exists to close, and
# development mode deliberately keeps the cluster alive after an update so the
# next one starts faster. Both are the wrong default for a project with a
# $150/month ceiling. The cluster is single-node under the pipeline policy, and
# it tears itself down when the update finishes -- pipeline compute has no
# `autotermination_minutes` and rejects one, which is recorded at length in the
# header of `databricks_compute.tf`.
#
# No schedule. Nothing in this workspace should be able to start spending on its
# own; #38 is where the weekly trigger is argued.

locals {
  # Where Auto Loader keeps each table's inferred schema and, more importantly,
  # its record of which files it has already consumed. That ledger is what makes
  # a re-run idempotent, so it lives in the lakehouse container -- durable,
  # backed up with everything else, and covered by no lifecycle rule -- rather
  # than beside the data it is reading.
  bronze_checkpoint_uri = "abfss://${azurerm_storage_container.lakehouse.name}@${azurerm_storage_account.data.name}.dfs.core.windows.net/_autoloader"

  bronze_lib_path = "/Shared/${local.base}/lib"
}

# --- The declarations, as workspace files -----------------------------------

resource "databricks_workspace_file" "bronze_module" {
  path   = "${local.bronze_lib_path}/bronze.py"
  source = "${path.module}/../../databricks/src/chip_chat/databricks/bronze.py"
}

resource "databricks_workspace_file" "catalog_module" {
  path   = "${local.bronze_lib_path}/catalog.py"
  source = "${path.module}/../../databricks/src/chip_chat/databricks/catalog.py"
}

resource "databricks_notebook" "bronze_ingest" {
  path     = "/Shared/${local.base}/bronze_ingest"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/bronze_ingest.py"
}

# --- The pipeline ------------------------------------------------------------

resource "databricks_pipeline" "bronze" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name = "${local.base}-bronze-ingest"

  # Unity Catalog publishing. `schema` is the default target for a name that is
  # not qualified; the notebook qualifies every one of them, because a single
  # pipeline writes into both `bronze_harvested` and `bronze_synthetic` and a
  # default that is right for half the tables is worse than no default at all.
  catalog = databricks_catalog.main[0].name
  schema  = "bronze_harvested"

  continuous  = false
  development = false
  photon      = false

  # CURRENT rather than PREVIEW: this pipeline is the only thing standing
  # between the landing zone and every downstream issue, and a channel that
  # moves under it is not worth the newer feature.
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
      "issue"         = "gh-33"
    }
  }

  # Read by the notebook through `spark.conf.get`. Everything the pipeline needs
  # to know about where it is running is here, so the notebook hardcodes no URI
  # and a teardown that regenerates the storage account's random suffix does not
  # leave a stale path in a source file.
  configuration = {
    "chip_chat.raw_uri"        = local.uc_probe_raw_uri
    "chip_chat.catalog"        = databricks_catalog.main[0].name
    "chip_chat.checkpoint_uri" = local.bronze_checkpoint_uri
    "chip_chat.lib_path"       = "/Workspace${local.bronze_lib_path}"
  }

  library {
    notebook {
      path = databricks_notebook.bronze_ingest.path
    }
  }

  depends_on = [
    databricks_permissions.pipeline_policy_usage,
    databricks_workspace_file.bronze_module,
    databricks_workspace_file.catalog_module,
    databricks_grants.medallion,
    databricks_grants.catalog,
    databricks_grants.raw_external_location,
    databricks_grants.lakehouse_external_location,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "bronze_pipeline" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  pipeline_id = databricks_pipeline.bronze[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_RUN"
  }

  # The app tier can start an update and read its state, with its managed
  # identity and no stored credential. It cannot edit what the pipeline does.
  # #38 makes the weekly re-harvest use this.
  access_control {
    service_principal_name = databricks_service_principal.app.application_id
    permission_level       = "CAN_RUN"
  }
}

# --- The proof ---------------------------------------------------------------
#
# The issue's four acceptance criteria are all claims about a live system, so
# they are a job rather than a screenshot -- the same shape as the two Unity
# Catalog probes gh-32 left behind, and for the same reason.
#
# It runs on job compute rather than inside the pipeline because a pipeline that
# asserts things about itself is a pipeline that fails the update it is
# verifying, and the third criterion needs the update to have *completed* with
# malformed input in the landing zone.
#
# Run it after the pipeline:
#
#   databricks pipelines start-update $(terraform output -raw databricks_bronze_pipeline_id)
#   databricks jobs run-now $(terraform output -raw databricks_bronze_verify_job_id)

resource "databricks_notebook" "bronze_verify" {
  path     = "/Shared/${local.base}/bronze_verify"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/bronze_verify.py"
}

resource "databricks_job" "bronze_verify" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name        = "${local.base}-bronze-verify"
  description = "Asserts issue #33's acceptance criteria against the bronze tables: both streams landed, no identity appears twice, and the quarantine resolves. Read-only. Manual trigger only."

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
        "issue"         = "gh-33"
      }
    }
  }

  task {
    task_key        = "verify"
    job_cluster_key = "single-node"

    notebook_task {
      notebook_path = databricks_notebook.bronze_verify.path
      base_parameters = {
        catalog  = databricks_catalog.main[0].name
        lib_path = "/Workspace${local.bronze_lib_path}"

        # TRUE, because the landing zone permanently holds two deliberately
        # malformed documents under `raw/index/zz/` -- a truncated pointer and
        # one whose `status_code` is a word. `zz` is not a hex digest shard, so
        # the pair can never collide with a harvested document, and leaving them
        # there is what makes the third acceptance criterion something you run
        # rather than something you set up first. Same reasoning as the
        # `lineage_probe` tables gh-32 deliberately left in place.
        #
        # An empty quarantine here therefore means the mechanism stopped
        # working, not that the corpus is clean.
        expect_quarantined = "true"
      }
    }
  }

  depends_on = [
    databricks_workspace_file.bronze_module,
    databricks_workspace_file.catalog_module,
    databricks_permissions.job_policy_usage,
    databricks_grants.medallion,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "bronze_verify_job" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  job_id = databricks_job.bronze_verify[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }
}
