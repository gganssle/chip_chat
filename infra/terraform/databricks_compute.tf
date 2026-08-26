# Everything inside the workspace: the compute policies that make the cost
# guardrail structural, the service principal jobs run as, and one job that
# proves the whole path works end to end.
#
# THE GUARDRAIL, STATED PLAINLY. Leaving an all-purpose cluster running is the
# most common way a demo subscription quietly burns a month of credits, and it
# is a pricing problem before it is a discipline problem: all-purpose compute is
# $0.55/DBU-hour against $0.30 for jobs compute in East US 2, before the VM
# underneath it. So the shape is configured in rather than agreed to.
#
# There are three policies and not one, because Databricks will not accept one,
# and because the ten-minute number in the design only means something in one of
# the three.
#
# `autotermination_minutes` is rejected outright by both job compute
# ("Automated clusters do not support autotermination") and pipeline compute.
# Both tear their cluster down when the run finishes, so termination there is
# structural: there is no idle cluster to time out, because there is no cluster
# once the work stops. The service inventory records this trap for pipelines
# only; it is true of jobs as well, found the hard way on 2026-08-26.
#
# So the ten minutes belongs to exactly one policy — the interactive one, which
# is the only place a cluster can sit idle and bill. That is also the only place
# the design's cost trap can actually happen.
# See docs/service-inventory.md#3-databricks-on-azure.

locals {
  # Small, current-generation, cheap. An allowlist rather than a fixed value so
  # a genuinely bigger job can be run deliberately, and so the list is the thing
  # someone has to edit when they want to spend more.
  databricks_node_types = var.databricks_node_type_allowlist

  # Shared by every policy below. Single node is the whole point: `num_workers`
  # fixed at zero means the driver is also the worker, there is no second VM to
  # forget about, and no autoscale ceiling to get wrong.
  databricks_single_node_rules = {
    "num_workers"           = { type = "fixed", value = 0 }
    "autoscale.min_workers" = { type = "forbidden", hidden = true }
    "autoscale.max_workers" = { type = "forbidden", hidden = true }

    # Databricks recognises a single-node cluster by this trio, not by
    # num_workers alone. Without them the driver still tries to schedule work on
    # executors that do not exist.
    "spark_conf.spark.databricks.cluster.profile" = { type = "fixed", value = "singleNode", hidden = true }
    "spark_conf.spark.master"                     = { type = "fixed", value = "local[*]", hidden = true }
    "custom_tags.ResourceClass"                   = { type = "fixed", value = "SingleNode", hidden = true }

    "node_type_id"        = { type = "allowlist", values = local.databricks_node_types, defaultValue = var.databricks_node_type }
    "driver_node_type_id" = { type = "allowlist", values = local.databricks_node_types, defaultValue = var.databricks_node_type, isOptional = true }

    # Pools keep VMs warm, which is the opposite of what this project wants.
    "instance_pool_id"        = { type = "forbidden", hidden = true }
    "driver_instance_pool_id" = { type = "forbidden", hidden = true }
  }
}

# --- Jobs ------------------------------------------------------------------

resource "databricks_cluster_policy" "job_single_node" {
  name = "${local.base}-job-single-node"

  definition = jsonencode(merge(local.databricks_single_node_rules, {
    # The load-bearing line. A cluster created under this policy cannot be an
    # all-purpose cluster, so it cannot outlive the job that created it.
    "cluster_type" = { type = "fixed", value = "job" }

    # NO autotermination_minutes, for the same reason as the pipeline policy
    # below and not merely by analogy with it: job compute rejects the attribute
    # too. Setting it here fails job creation with "Automated clusters do not
    # support autotermination" (observed 2026-08-26, which is how this comment
    # came to exist). The attribute would have been meaningless anyway — a job
    # cluster is torn down when the run ends, so termination is structural here
    # rather than a timeout.
  }))
}

# --- Interactive -----------------------------------------------------------
#
# The issue asks for no all-purpose cluster that can be left running, "or one
# with an aggressive termination policy if interactive work is genuinely
# needed". Interactive work is genuinely needed — someone has to look at the
# bronze tables — so this exists, and it is aggressive: one single-node cluster
# per person, dead ten minutes after the last command.

resource "databricks_cluster_policy" "interactive_single_node" {
  name = "${local.base}-interactive-single-node"

  # One per person. Not a cost ceiling on its own, but it removes the failure
  # where a stale cluster is invisible because a fresh one was started beside it.
  max_clusters_per_user = 1

  definition = jsonencode(merge(local.databricks_single_node_rules, {
    "cluster_type" = { type = "fixed", value = "all-purpose" }

    # Fixed, not a maximum. A maximum would still let someone type 4320.
    "autotermination_minutes" = { type = "fixed", value = var.databricks_autotermination_minutes }
  }))
}

# --- Pipelines -------------------------------------------------------------

resource "databricks_cluster_policy" "pipeline_single_node" {
  name        = "${local.base}-pipeline-single-node"
  description = "Single-node compute for Lakeflow Spark Declarative Pipelines. Deliberately has no autotermination_minutes — pipeline compute rejects it."

  definition = jsonencode(merge(local.databricks_single_node_rules, {
    "cluster_type" = { type = "fixed", value = "dlt" }
    # NO autotermination_minutes. See the header of this file: pipeline compute
    # stops itself, and a policy that sets the attribute fails at pipeline start.
    # The SKU names and event-log schemas still say DLT even though the product
    # is now Lakeflow Spark Declarative Pipelines; "dlt" here is the API value,
    # not a stale reference.
  }))
}

# --- Who may create compute, and under what ---------------------------------
#
# A policy only binds someone who has to use one. Workspace admins can create
# unrestricted compute and no policy changes that — which is worth saying out
# loud, because "a policy exists" reads like a wall and is really a default.
# Taking cluster-create off the `users` group is what turns the policies above
# from a suggestion into the only route for everyone who is not an admin.

# `depends_on` is doing real work here, not documenting an ordering that would
# have happened anyway. Terraform reads data sources during plan, and on a first
# apply the workspace this provider authenticates against does not exist yet —
# the read fails with "cannot configure default credentials", which reads like a
# broken `az login` and is not. Depending on the workspace defers the read to
# apply time, which is the only time the answer exists.
data "databricks_group" "users" {
  display_name = "users"

  depends_on = [azurerm_databricks_workspace.main]
}

resource "databricks_entitlements" "users" {
  count = var.databricks_restrict_cluster_create ? 1 : 0

  group_id = data.databricks_group.users.id

  # The two that cost money without going through a policy.
  allow_cluster_create       = false
  allow_instance_pool_create = false

  # Left on: without it a member of `users` cannot open the workspace at all,
  # which is a different thing from not being able to spend in it. This resource
  # is authoritative for the group, so it has to be restated rather than merely
  # not mentioned.
  workspace_access = true

  # Off, and this one is a narrowing: the group had it. SQL warehouses are the
  # single compute path in this workspace that cluster policies do not govern —
  # a policy binds clusters, and a warehouse is not a cluster. The workspace
  # ships with a Serverless Starter Warehouse (Small, PRO, auto-stop 10 minutes)
  # that nothing in this design uses; the natural-language-to-SQL lane is
  # Snowflake Cortex Analyst, not Databricks SQL. Serverless SQL is $0.70/DBU-hr
  # and a Small warehouse draws about 12 DBU/hr, so an idle-but-started warehouse
  # is roughly $8/hour — the most expensive way to spend money in this workspace
  # and the one the rest of this file cannot reach.
  #
  # Today this changes nothing: the only member of `users` is a workspace admin,
  # and admins are not bound by entitlements. It is here so that the second
  # person added to this workspace does not arrive with the one capability the
  # guardrails do not cover.
  databricks_sql_access = false
}

# --- The identity jobs run as -----------------------------------------------
#
# Automated work runs as a service principal rather than as a person, so that a
# job does not stop working the day someone's account is disabled, and so that
# the audit log distinguishes the pipeline from the human who wrote it.

resource "databricks_service_principal" "jobs" {
  display_name = "${local.base}-jobs"

  # Deliberately false: the SP creates compute through the policy below, which
  # is the whole point of having the policy.
  allow_cluster_create       = false
  allow_instance_pool_create = false
  workspace_access           = true
}

# Binding a job's `run_as` to a service principal is an ACCOUNT-level permission,
# not a workspace one, and it is not implied by being an account admin. Without
# it, creating the job fails with "the user creating or updating the job must
# have 'servicePrincipal.user' role on the service principal" — which reads like
# a workspace ACL problem and is not one.
#
# The rule set is authoritative for the principal it names, so the `manager` role
# Databricks grants the creator automatically has to be restated here or applying
# this would silently revoke it.
data "databricks_current_user" "me" {
  depends_on = [azurerm_databricks_workspace.main]
}

resource "databricks_access_control_rule_set" "jobs_service_principal" {
  name = "accounts/${var.databricks_account_id}/servicePrincipals/${databricks_service_principal.jobs.application_id}/ruleSets/default"

  # Restated, not added: Databricks grants this to whoever created the principal.
  grant_rules {
    principals = [data.databricks_current_user.me.acl_principal_id]
    role       = "roles/servicePrincipal.manager"
  }

  # The one that matters here. "Manager" is about administering the principal;
  # "user" is about acting as it, which is what `run_as` needs.
  grant_rules {
    principals = [data.databricks_current_user.me.acl_principal_id]
    role       = "roles/servicePrincipal.user"
  }
}

resource "databricks_permissions" "job_policy_usage" {
  cluster_policy_id = databricks_cluster_policy.job_single_node.id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_USE"
  }

  # The read-only principal (gh-32) runs one job: the one that proves it cannot
  # write. It needs the policy for the same reason the jobs principal does --
  # neither may create compute any other way -- and it is listed here rather than
  # in its own resource because this ACL is authoritative for the policy and a
  # second `databricks_permissions` on the same object would overwrite this one
  # on every alternate apply.
  access_control {
    service_principal_name = databricks_service_principal.readonly.application_id
    permission_level       = "CAN_USE"
  }
}

# The same grant on the pipeline policy, and it is a separate resource because a
# cluster policy's ACL is authoritative for the policy it names -- one resource
# covering both would be one resource replacing both.
#
# ⚠️ A POLICY IS NOT USABLE BY THE PRINCIPAL A PIPELINE RUNS AS UNTIL THIS
# EXISTS, and the failure does not name the principal. Creating the pipeline
# succeeds, `terraform apply` reports no drift, and the *update* fails two
# seconds in with
#
#     Failed to create a pipeline cluster: PERMISSION_DENIED: You are not
#     authorized to access this cluster policy.
#
# which reads like the policy is broken rather than like a grant is missing.
# The jobs principal held CAN_USE on the job policy since gh-31 and nothing
# implied it here. Found by hitting it on `dbw-chip-chat`, 2026-08-26 (gh-33).
resource "databricks_permissions" "pipeline_policy_usage" {
  cluster_policy_id = databricks_cluster_policy.pipeline_single_node.id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_USE"
  }
}

# --- How the app tier authenticates, and why there is no secret ---------------
#
# Issue #31 asks for "a service principal for automated jobs; PAT stored in Key
# Vault". It gets the service principal. It does not get the PAT, and the reason
# is worth reading before someone adds one back.
#
# The identity above never needs a credential: `run_as` is resolved inside
# Databricks, so a job running as it authenticates without anything being issued,
# stored or rotated. The credential in the original scope was for the other
# direction — something outside Databricks calling in. And for that, the app
# already has an identity: `id-chip-chat-app`, the user-assigned managed identity
# every other Azure service in this stack is reached with. Registering it here as
# a Databricks service principal lets the app tier present an Entra token for the
# Azure Databricks resource and be recognised, with no PAT minted, no secret in
# Key Vault, and nothing to expire on a Tuesday.
#
# That is the same rule storage.tf already applies with
# `shared_access_key_enabled = false`: the strongest version of "the key did not
# leak" is that there is no key. A PAT is still available if something genuinely
# cannot do Entra — see var.databricks_service_principal_token_enabled, which is
# off, and which documents the one-time account-console step it needs.
resource "databricks_service_principal" "app" {
  application_id = azurerm_user_assigned_identity.app.client_id
  display_name   = "${local.base}-app (id-${local.base}-app managed identity)"

  # Read and trigger, never create compute. The app tier's job is to start the
  # pipeline, not to decide what it runs on.
  allow_cluster_create       = false
  allow_instance_pool_create = false
  workspace_access           = true

  # This identity exists in Entra and is owned by foundation.tf. Databricks is
  # being told about it, not being made its owner, so removing this resource must
  # not attempt to delete the underlying principal.
  force = true
}

# A PAT for the jobs service principal. OFF by default, because on-behalf-of
# token creation is disabled for new Databricks accounts and can only be enabled
# by an account admin in the account console — a portal action Terraform cannot
# take. Verified on this workspace 2026-08-26: the API answers "On-behalf-of
# token creation for service principals is not enabled for this workspace", and
# there is no workspace-conf key for it (`enableTokensConfig` is already true and
# is a different setting).
#
# When it is on, the token value ends up in Terraform state. That is the same
# trade the AI Search admin key already makes: the state container is Entra-only
# with shared-key access disabled, so it sits behind the same identity boundary
# as the vault. Treat state as sensitive regardless. See infra/README.md.
resource "databricks_obo_token" "jobs" {
  count = var.databricks_service_principal_token_enabled ? 1 : 0

  application_id   = databricks_service_principal.jobs.application_id
  comment          = "chip_chat automated jobs (gh-31). Rotate with: terraform apply -replace=databricks_obo_token.jobs[0]"
  lifetime_seconds = var.databricks_token_lifetime_days * 24 * 60 * 60
}

# --- The proof ---------------------------------------------------------------
#
# One job, run by hand, that writes to ADLS through Unity Catalog and reads it
# back. It exists so that "the storage credential works and the cluster stops
# afterwards" is something you run rather than something you remember being told.
# No schedule: nothing here should be able to start spending on its own.

resource "databricks_notebook" "adls_smoke" {
  path     = "/Shared/${local.base}/adls_smoke"
  language = "PYTHON"
  source   = "${path.module}/../../databricks/notebooks/adls_smoke.py"
}

resource "databricks_job" "adls_smoke" {
  name        = "${local.base}-adls-smoke"
  description = "Round-trips a small Delta table through the ADLS external location on single-node job compute. Manual trigger only."

  # A run that has not finished in fifteen minutes is not going to, and the
  # useful thing at that point is for it to stop costing money.
  timeout_seconds     = var.databricks_smoke_timeout_seconds
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

      # Unity Catalog access needs a security mode that carries an identity, and
      # single-node compute only supports the single-user one. The identity is
      # the service principal the job runs as, which is what makes the grants on
      # the external location apply to this cluster.
      data_security_mode = "SINGLE_USER"
      single_user_name   = databricks_service_principal.jobs.application_id

      spark_conf = {
        "spark.databricks.cluster.profile" = "singleNode"
        "spark.master"                     = "local[*]"
      }

      custom_tags = {
        "ResourceClass" = "SingleNode"
        "project"       = "chip_chat"
        "issue"         = "gh-31"
      }
    }
  }

  task {
    task_key        = "roundtrip"
    job_cluster_key = "single-node"

    notebook_task {
      notebook_path = databricks_notebook.adls_smoke.path
      base_parameters = {
        raw_uri = "abfss://${azurerm_storage_container.raw.name}@${azurerm_storage_account.data.name}.dfs.core.windows.net"
        # A Databricks dynamic value, resolved per run. It gives the notebook a
        # path of its own so two runs cannot pass by reading each other's output.
        run_id = "{{job.run_id}}"
      }
    }
  }

  depends_on = [
    databricks_permissions.job_policy_usage,
    databricks_grants.raw_external_location,
    databricks_access_control_rule_set.jobs_service_principal,
  ]
}

resource "databricks_permissions" "adls_smoke_job" {
  job_id = databricks_job.adls_smoke.id

  access_control {
    service_principal_name = databricks_service_principal.jobs.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }

  # The app tier can start the job and read its runs, using its managed identity
  # and no stored credential. It cannot edit what the job does.
  access_control {
    service_principal_name = databricks_service_principal.app.application_id
    permission_level       = "CAN_MANAGE_RUN"
  }
}
