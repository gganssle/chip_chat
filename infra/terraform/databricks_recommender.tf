# The item-affinity recommender (gh-37): an MLflow experiment, a Unity Catalog
# registered model, the scheduled job that fills both, and the job that asserts
# the acceptance criteria.
#
# `databricks_gold.tf` made the pipeline that decides what is served. This one
# makes the thing that decides what is *suggested*, and it is the first object in
# this workspace that is a model rather than a table. The point of the issue is
# not the model -- a co-occurrence count is deliberately modest -- it is that the
# MLflow tracking and Unity Catalog registry path is exercised for real, and that
# what reaches a visitor is grounded in their own ordering behaviour.
#
# --- WHY A JOB AND NOT A PIPELINE -------------------------------------------
#
# The three medallion lanes are Lakeflow declarative pipelines because each of
# them is "declare a table, let the engine work out how to keep it right". This
# is not that shape. Training is imperative -- fit, evaluate, compare against a
# baseline, decide whether to move an alias -- and a declarative pipeline has
# nowhere to put a decision. It also has to talk to two systems that are not the
# table store: the MLflow tracking server and the model registry.
#
# So: a two-task job. `train` fits and registers; `publish` loads the alias and
# writes the table. Two tasks rather than one notebook because they fail for
# different reasons and are worth retrying separately -- a registry timeout is
# not a reason to refit -- and because the task boundary is where "the model that
# is deployed" is read back rather than passed along.
#
# --- THE SCHEDULE, AND THE RULE IT SITS AGAINST ------------------------------
#
# Issue #37's fourth acceptance criterion is that retraining is a scheduled job
# rather than a notebook someone remembers to run. Every other file in this
# directory says the opposite thing: "no schedule -- nothing in this workspace
# should be able to start spending on its own", and #38 moved the weekly
# re-harvest to a GitHub Actions runner rather than a job cluster for the same
# reason.
#
# Both are right, and they are about different halves of the sentence. The
# criterion is about the *retraining being scheduled infrastructure* -- declared,
# reviewable, with a cron expression a person can read -- and the guardrail is
# about *this Terraform not starting a cluster nobody asked for*. So the schedule
# is declared here, with its cron, and `pause_status` is driven by
# `var.databricks_recommender_schedule_enabled`, which defaults to false.
#
# What ships is a job with a schedule that is paused. Turning retraining on is
# one variable and one apply, not a job somebody builds later; and
# `recommender_verify.py` reads the schedule back off the Jobs API and fails if
# there is none, so "scheduled" cannot quietly become "manual".
#
# Unlike the re-harvest, this genuinely belongs on a job cluster: it is a
# self-join over every order in the population followed by an MLflow log, which
# is Spark work against Unity Catalog tables and cannot run on a free runner
# holding no credentials.
#
# --- WHAT IS NOT HERE -------------------------------------------------------
#
# No model serving endpoint. The issue is explicit that the serving path should
# read a table rather than call a model on the conversational path, so the model
# is batch-scored into `gold_synthetic.recommendations` and nothing subscribes to
# an endpoint. An endpoint would also be an always-on cost, which is the trap
# `databricks_compute.tf` exists to close.
#
# No publish to Snowflake. #39 owns the nightly hand-off, and this job's work
# ends when the table is correct in Unity Catalog.
#
# No row access policy. #43 applies them, and `recommendations` carries `demo_id`
# so that it can.

locals {
  recommender_lib_path = local.bronze_lib_path
}

# --- The declarations, as workspace files -----------------------------------
#
# `recommender.py` is stdlib-only and joins `bronze.py`, `catalog.py`, `silver.py`
# and `gold.py` in the shared lib directory, for the reason `gold.py` joined
# them: modules that always travel together do not need a `sys.path` entry each.
#
# `recommender_model.py` is the one file in the tree that imports MLflow, and it
# is uploaded rather than pip-installed because it is *also* what MLflow logs
# into every model version as `code_paths`. A version that loaded whatever the
# module says next week would not be the model whose metrics were logged beside
# it, which is the failure a registry exists to prevent.

resource "databricks_workspace_file" "recommender_module" {
  path   = "${local.recommender_lib_path}/recommender.py"
  source = "${path.module}/../../databricks/src/chip_chat/databricks/recommender.py"
}

resource "databricks_workspace_file" "recommender_model_module" {
  path   = "${local.recommender_lib_path}/recommender_model.py"
  source = "${path.module}/../../databricks/src/chip_chat/databricks/recommender_model.py"
}

resource "databricks_notebook" "recommender_train" {
  path     = "/Shared/${local.base}/recommender_train"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/recommender_train.py"
}

resource "databricks_notebook" "recommender_publish" {
  path     = "/Shared/${local.base}/recommender_publish"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/recommender_publish.py"
}

resource "databricks_notebook" "recommender_verify" {
  path     = "/Shared/${local.base}/recommender_verify"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/recommender_verify.py"
}

# --- Where the runs go -------------------------------------------------------
#
# Named here rather than left to default. A notebook with no experiment set logs
# into a workspace path named after itself, which works and is invisible: nobody
# comparing two versions of a model goes looking under `/Shared/.../train`. One
# named experiment is where "which run produced the champion" is answerable.
#
# What lands in it, per run: every hyperparameter, each carrying a `why.<name>`
# tag that says what the number does -- MLflow records a parameter as a string
# with nothing attached, and a run whose reader has to open the source to find
# out what `shrinkage=40` meant is tracked but not documented. Then four hit
# rates (the model's and a popularity baseline's, plain and novel), catalogue
# coverage, and `item_affinity_agreement`, which is 1.0 when the full-history
# refit reproduces the published gold mart.
#
# The `description` argument is deprecated in the provider and no longer written
# anywhere, so this is a comment and the tags below are what survives into the
# workspace.

# The experiments folder has to be declared, which is not true of anything else
# this file writes into the workspace. `databricks_notebook` and
# `databricks_workspace_file` create the directories on the way to their path;
# the MLflow experiment API does not, and the first apply of this file failed
# with `Parent directory does not exist: /Shared/chip-chat/experiments`. Nothing
# else in this repository puts an object under `experiments/`, so no other
# resource brings it into being as a side effect, and relying on one that
# happened to would be a dependency nobody wrote down.
#
# The experiment names itself from this resource's own path rather than
# rebuilding the string, so the ordering is a real dependency in the graph
# instead of a hope about the order Terraform picked.

resource "databricks_directory" "recommender_experiments" {
  path = "/Shared/${local.base}/experiments"
}

resource "databricks_mlflow_experiment" "recommender" {
  name = "${databricks_directory.recommender_experiments.path}/item-affinity-recommender"

  tags {
    key   = "project"
    value = "chip_chat"
  }

  tags {
    key   = "issue"
    value = "gh-37"
  }

  tags {
    key   = "promotes_on"
    value = "novel_hit_rate_at_k over a popularity baseline (PRD P2)"
  }
}

resource "databricks_permissions" "recommender_experiment" {
  experiment_id = databricks_mlflow_experiment.recommender.id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_EDIT"
  }

  access_control {
    service_principal_name = databricks_service_principal.readonly.application_id
    permission_level       = "CAN_READ"
  }
}

# --- The registered model ----------------------------------------------------
#
# Created here and not by the training notebook, for the reason
# `databricks_catalog.tf` gives about schemas: ownership and grants are cheap to
# set on an empty object and tedious to retrofit onto one that already has
# versions. The notebook creates *versions* of this model and moves its alias; it
# never creates the model itself.
#
# Three-level, in `gold_synthetic`, so it is governed by the grants that already
# cover the tables it was fitted from -- a principal who may not read the
# synthetic population may not load the model fitted on it either. That is the
# whole difference between this registry and the workspace one, and it is the
# reason the issue asks for Unity Catalog by name.

resource "databricks_registered_model" "recommender" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name         = "item_affinity_recommender"
  catalog_name = databricks_catalog.main[0].name
  schema_name  = databricks_schema.medallion["gold_synthetic"].name
  owner        = local.uc_owner

  comment = "Item-affinity co-occurrence recommender (gh-37). Scores a visitor's unordered items by the shrunk lift of their strongest ordered item against each candidate, excludes everything they have ever ordered, and returns the top few with the seed that earned each one. Fitted on silver_synthetic.orders and order_items; the full-history refit reproduces gold_synthetic.item_affinity. The @champion alias only moves when a run beats a popularity baseline on NOVEL hit rate, because PRD P2 is explicit that a top-sellers list does not satisfy the requirement even when it scores well. Batch-scored into gold_synthetic.recommendations; nothing calls this model on the conversational path."

  depends_on = [databricks_grants.medallion]
}

# EXECUTE, and only EXECUTE -- which is the privilege to *load* a model and
# score with it, and is what `recommender_publish.py` and a reviewer need.
#
# This comment used to say that MODIFY on the schema is what creating a version
# and setting an alias needs. It is not, and the first live training run is how
# that was found out: MLflow's `log_model(registered_model_name=...)` calls
# `create_registered_model` before it logs anything, idempotently, whether or
# not the model already exists -- and that call wants CREATE_MODEL on the
# schema, which `databricks_catalog.tf` now grants and argues for.
#
# The read-only principal gets EXECUTE so that a reviewer can load a version and
# reproduce a recommendation without being able to replace it.
resource "databricks_grants" "recommender_model" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  model = databricks_registered_model.recommender[0].id

  grant {
    principal  = databricks_service_principal.jobs.application_id
    privileges = ["EXECUTE"]
  }

  grant {
    principal  = databricks_service_principal.readonly.application_id
    privileges = ["EXECUTE"]
  }
}

# --- The job -----------------------------------------------------------------

resource "databricks_job" "recommender" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name        = "${local.base}-recommender"
  description = "Fits the item-affinity recommender over silver_synthetic, evaluates it on a temporal holdout against a popularity baseline, registers a version in Unity Catalog, moves the @champion alias only if the run beat the baseline on novel hit rate -- then batch-scores the champion into gold_synthetic.recommendations. Weekly. Retraining is a scheduled job rather than a notebook somebody remembers to run (gh-37)."

  timeout_seconds     = var.databricks_recommender_timeout_seconds
  max_concurrent_runs = 1

  run_as {
    service_principal_name = databricks_service_principal.jobs.application_id
  }

  # 09:00 UTC on Mondays, two hours after #38's weekly re-harvest, so that a
  # week in which the catalogue changed is a week this model is refitted after
  # the change rather than before it. Quartz, because that is what the Jobs API
  # takes: seconds first, and a `?` in whichever of day-of-month and day-of-week
  # is not being used.
  schedule {
    quartz_cron_expression = "0 0 9 ? * MON"
    timezone_id            = "UTC"
    pause_status           = var.databricks_recommender_schedule_enabled ? "UNPAUSED" : "PAUSED"
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
        "issue"         = "gh-37"
      }
    }
  }

  task {
    task_key        = "train"
    job_cluster_key = "single-node"

    notebook_task {
      notebook_path = databricks_notebook.recommender_train.path
      base_parameters = {
        catalog    = databricks_catalog.main[0].name
        lib_path   = "/Workspace${local.recommender_lib_path}"
        experiment = databricks_mlflow_experiment.recommender.name
      }
    }
  }

  # `publish` runs whatever `train` decided, including "the alias did not move".
  # That is deliberate: republishing from an unchanged champion refreshes the
  # table against a week of new orders, which is a real change even when the
  # model is not. What it never does is publish from a version that lost -- it
  # loads the alias, and a losing run did not take it.
  task {
    task_key        = "publish"
    job_cluster_key = "single-node"

    depends_on {
      task_key = "train"
    }

    notebook_task {
      notebook_path = databricks_notebook.recommender_publish.path
      base_parameters = {
        catalog  = databricks_catalog.main[0].name
        lib_path = "/Workspace${local.recommender_lib_path}"
      }
    }
  }

  depends_on = [
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.recommender_module,
    databricks_workspace_file.recommender_model_module,
    databricks_permissions.job_policy_usage,
    databricks_grants.medallion,
    databricks_grants.recommender_model,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "recommender_job" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  job_id = databricks_job.recommender[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }

  # The app tier may start a retraining run with its managed identity and no
  # stored credential -- the same grant the three pipelines have, so that a
  # rebuild after a re-harvest can run the whole chain in sequence.
  #
  # `CAN_MANAGE_RUN` and not `CAN_RUN`, which is what the pipelines above take.
  # The two object types do not share a permission vocabulary: a job accepts
  # only `CAN_MANAGE`, `CAN_MANAGE_RUN`, `CAN_VIEW` and `IS_OWNER`, and the
  # first apply of this file was rejected outright for asking for the pipeline
  # word on a job. `CAN_MANAGE_RUN` is the job-shaped spelling of the same
  # intent -- start and cancel runs, change nothing about the job itself.
  access_control {
    service_principal_name = databricks_service_principal.app.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }
}

# --- The proof ---------------------------------------------------------------
#
# #37's four acceptance criteria are claims about a live system, so they are a
# job rather than a screenshot -- the same shape as `gold_verify`.
#
# It is a separate job rather than a third task on the one above for the reason
# `gold_verify` is separate from the gold pipeline: a check that runs only as
# part of the thing it checks cannot be run to ask whether the thing is still
# true. This one is read-only and safe at any time, and one of its assertions is
# about the retraining job's own schedule, which it reads off the Jobs API.
#
# Run them in order:
#
#   databricks jobs run-now $(terraform output -raw databricks_recommender_job_id)
#   databricks jobs run-now $(terraform output -raw databricks_recommender_verify_job_id)

resource "databricks_job" "recommender_verify" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name        = "${local.base}-recommender-verify"
  description = "Asserts issue #37's acceptance criteria against the live recommender: the model registered in Unity Catalog with a version behind a run carrying every parameter and metric, no visitor recommended anything they have ever ordered, a short well-formed rationale on every row, and the retraining job carrying a cron schedule. Also checks what none of the four say and PRD P2 does -- that these are not a top-sellers list. Read-only. Manual trigger only."

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
        "issue"         = "gh-37"
      }
    }
  }

  task {
    task_key        = "verify"
    job_cluster_key = "single-node"

    notebook_task {
      notebook_path = databricks_notebook.recommender_verify.path
      base_parameters = {
        catalog  = databricks_catalog.main[0].name
        lib_path = "/Workspace${local.recommender_lib_path}"
        job_name = databricks_job.recommender[0].name
      }
    }
  }

  depends_on = [
    databricks_workspace_file.catalog_module,
    databricks_workspace_file.recommender_module,
    databricks_permissions.job_policy_usage,
    databricks_grants.medallion,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "recommender_verify_job" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  job_id = databricks_job.recommender_verify[0].id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }
}
