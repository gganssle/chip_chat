# Observability — both halves, and they are different questions.
#
# Application Insights answers *is the service healthy*: request rate, container
# latency, dependency failures, exceptions. The agent-observability backend
# answers *is the agent behaving*: the span tree for one turn, which tool it
# reached for, tokens per span, which passages the retriever returned. The two
# are a fan-out and never a migration — every span goes to both, out of one
# tracer provider, and losing either one loses a question nothing else answers.
#
# The second half used to live outside this stack, on the assumption that it
# would be bought from Arize. It is not bought; it is hosted here, in the same
# Container Apps environment as the app it watches, and that decision — what was
# planned, what the owner chose instead, and what is genuinely lost by choosing
# it — is written down in `docs/decisions/hosted-phoenix.md`. Read that before
# changing anything below the Application Insights block.

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.base}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location

  sku               = "PerGB2018"
  retention_in_days = var.log_retention_days

  # A real spend control, not a tidiness setting. Ingestion is billed per GB and
  # a crash-looping container can produce a great deal of it overnight.
  # Ingestion stops when the cap is hit; logs resume the next day.
  daily_quota_gb = var.log_daily_quota_gb

  tags = local.tags
}

resource "azurerm_application_insights" "main" {
  name                = "appi-${local.base}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  application_type    = "web"

  # Workspace-based. Classic Application Insights is retired.
  workspace_id = azurerm_log_analytics_workspace.main.id

  # Second layer of the same cap, because App Insights bills its own ingestion
  # through the workspace and has its own daily ceiling.
  daily_data_cap_in_gb                 = var.log_daily_quota_gb
  daily_data_cap_notifications_enabled = true

  # Strip client IPs. A public demo has no reason to retain them.
  ip_masking_enabled = true

  tags = local.tags
}

# --- The agent-observability backend ----------------------------------------
#
# Phoenix, self-hosted, in the same Container Apps environment as the app whose
# spans it collects. The image is the one `compose.yaml` pins, and it is pinned
# here from the same value for a reason worth stating: a development loop and a
# deployment that disagree about the backend's version are worse than either
# alone, because every difference you then see between a local span tree and a
# production one has two possible causes instead of one.
# `infra/tests/test_local_stack.py` fails if the two ever drift apart.
#
# The alternative that was planned was Arize AX, and `docs/arize-switch.md`
# still records the diff that switch would be. It is not a purchase this session
# was authorised to make, and the repository owner's instruction is free tier
# only, never converting to paid. The full argument — what was planned, what was
# decided instead, and what is genuinely lost by deciding it — is in
# `docs/decisions/hosted-phoenix.md`.

# TRACES HERE ARE EPHEMERAL, DELIBERATELY, AND THIS IS THE MEASUREMENT THAT
# DECIDED IT. Do not add a volume back without reading this paragraph.
#
# Phoenix keeps a SQLite database under PHOENIX_WORKING_DIR, and the obvious
# thing to do is mount an Azure Files share there so a container restart does not
# empty the backend. That was built, deployed and tested on 28 August 2026, and
# it does not work. What happens is precise enough to be worth recording:
#
#   1. The first container starts, migrates, serves, and takes 34 real spans.
#      It creates phoenix.db, phoenix.db-wal and phoenix.db-shm on the share.
#   2. Container Apps rolls a new replica BEFORE terminating the old one — there
#      is no "recreate" strategy for an app — so for about a minute two Phoenix
#      processes have the same SQLite file. `max_replicas = 1` does not prevent
#      this; it bounds the steady state, not the transition.
#   3. The second process dies with `sqlean.dbapi2.OperationalError: unable to
#      open database file` and crash-loops. SQLite's WAL mode needs shared
#      memory through the -shm file, and CIFS cannot provide the mmap semantics
#      that requires.
#   4. It does not recover. A clean scale-to-zero-and-back into a brand new
#      revision failed the same way, because the -shm left on the share is now
#      unopenable and undeletable — the Azure Files API refuses with
#      `DeletePending`, "marked for deletion by an SMB client".
#
# So the share bought exactly one process lifetime of persistence and a backend
# that cannot be restarted, which is worse than no persistence at all. The
# options from there were a PostgreSQL Flexible Server, which is Phoenix's
# supported production database and costs about $16 a month and would need a
# public endpoint with a password because this environment has no VNet; or
# accepting ephemeral traces. The second was chosen and the reason is that the
# durable copy already exists:
#
#   **Application Insights holds every one of these spans**, out of the same
#   tracer provider, with the same span ids, under the same trace ids, for 30
#   days. That was verified rather than assumed — see the evidence section of
#   docs/decisions/hosted-phoenix.md. Phoenix is the agent-shaped view and the
#   monitors' rolling window; App Insights is the archive. Paying $16 a month
#   and opening a database to the internet to have two archives, on an estate
#   whose budget alert is already going to fire, is the wrong trade.
#
# What is genuinely lost: a restart empties the span tree UI, and the monitor
# job's next run sees only traffic since the restart. The monitors resume within
# fifteen minutes and nothing about "online evals are live" stops being true; a
# human wanting to look at last week's trace has to open App Insights and read
# it as spans rather than as a tree. That is the cost, in full.

resource "azurerm_container_app" "phoenix" {
  count = var.phoenix_enabled ? 1 : 0

  name                         = "ca-${local.base}-phoenix"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"

  # Internal. This holds production traces, and a production trace carries what
  # a stranger typed, what the model said back, and the passages the retriever
  # returned — so a publicly readable Phoenix is a public transcript of every
  # conversation the demo has ever had. Internal ingress means the FQDN resolves
  # only inside `cae-chip-chat`, which is exactly the set of things that have
  # business with it: the chat app, which writes, and the monitor job below,
  # which reads.
  #
  # The alternative considered was external ingress with Phoenix's own
  # authentication (PHOENIX_ENABLE_AUTH and a secret), which would let the owner
  # open the UI in a browser from anywhere. It was rejected because it trades a
  # network boundary for a password on an internet-facing service holding
  # visitors' messages, and because the thing it buys is available without it —
  # see `docs/decisions/hosted-phoenix.md`, "Reading the traces".
  ingress {
    external_enabled = false
    target_port      = 6006
    transport        = "auto"

    # Plain HTTP inside the environment. Nothing on this hop leaves the managed
    # environment's own network, and the exporter is an OTLP/HTTP client that
    # would otherwise have to trust a certificate for an `.internal.` name.
    allow_insecure_connections = true

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    # One replica, always. This is the setting that would otherwise be wrong by
    # default: everything else in this estate scales to zero because an idle
    # replica bills for nothing served, and every other app here can afford to
    # be asleep because a visitor's request wakes it. A span exporter's POST is
    # not a request anybody is waiting on — a batch of spans arriving at a
    # backend that is scaled to zero is dropped, quietly, and the first symptom
    # is a monitor that has not fired in a week because it saw no traffic rather
    # than because there was none.
    #
    # A ceiling of one as well. Phoenix's SQLite database is container-local
    # (see the block above), so a second replica would be a second, separate
    # database behind one ingress — half the traces in each, and a monitor
    # reading whichever one the load balancer chose.
    min_replicas = 1
    max_replicas = 1

    container {
      name  = "phoenix"
      image = var.phoenix_image

      # Phoenix holds recent spans in memory as well as on disk and builds trace
      # trees in-process for the UI. Half a vCPU and a gigabyte is the smallest
      # pair Container Apps sells that is comfortable.
      cpu    = 0.5
      memory = "1.0Gi"

      # Container-local, and the same path `compose.yaml` uses so that the two
      # deployments differ in as few places as possible. See the block above for
      # why there is no volume behind it.
      env {
        name  = "PHOENIX_WORKING_DIR"
        value = "/tmp/phoenix"
      }

      # Phoenix serves the UI, its REST API and OTLP-over-HTTP on one port, and
      # Container Apps ingress publishes one port. 6006 is that port, and it is
      # the same one `compose.yaml` publishes locally.
      env {
        name  = "PHOENIX_PORT"
        value = "6006"
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = 6006
        path                    = "/healthz"
        initial_delay           = 30
        interval_seconds        = 30
        timeout                 = 5
        failure_count_threshold = 5
      }

      readiness_probe {
        transport               = "HTTP"
        port                    = 6006
        path                    = "/healthz"
        initial_delay           = 10
        interval_seconds        = 10
        timeout                 = 5
        failure_count_threshold = 12
        success_count_threshold = 1
      }
    }
  }

  tags = local.tags
}

# Where the app's spans go, and the whole of the "switching backends is
# configuration" claim on the app tier.
#
# `var.otlp_endpoint` stays the manual override — set it and the app exports
# there instead, which is how the Arize AX switch in `docs/arize-switch.md`
# would still be made, without deleting anything above. When it is empty and
# Phoenix is enabled, the endpoint is the Phoenix app's own internal address,
# computed from the resource rather than written down, so it cannot go stale.
locals {
  phoenix_endpoint = (
    var.phoenix_enabled
    ? "http://${azurerm_container_app.phoenix[0].ingress[0].fqdn}"
    : ""
  )
  otlp_endpoint = var.otlp_endpoint != "" ? var.otlp_endpoint : local.phoenix_endpoint

  # The monitors image, which exists only where a registry does. Terraform owns
  # this exactly once, at creation; see var.monitors_image_tag for why the tag
  # moves and why that is the same exception the web app already takes.
  monitors_registry = one(azurerm_container_registry.main[*].login_server)
  monitors_image = (
    local.monitors_registry == null
    ? ""
    : "${local.monitors_registry}/chip-chat-monitors:${var.monitors_image_tag}"
  )
}

# --- The monitors, on a schedule --------------------------------------------
#
# `chip_chat.eval.online` has had six monitors, a deterministic sampling policy
# and a budget line since #76, and has never had a live trace source: it read a
# capture file somebody produced by hand. This is the source, and the schedule
# is the half that makes it a monitor rather than a command — a check somebody
# has to remember to run has already failed on the day it matters.
#
# A Container Apps *job* rather than a second always-on app, because the work is
# a few seconds of reading spans every quarter of an hour and a replica idling
# between cron ticks would cost more than the work does. It runs inside
# `cae-chip-chat`, which is the only place the internal Phoenix address
# resolves.
resource "azurerm_container_app_job" "monitors" {
  count = var.phoenix_enabled && local.monitors_image != "" ? 1 : 0

  name                         = "caj-${local.base}-monitors"
  resource_group_name          = azurerm_resource_group.main.name
  location                     = var.location
  container_app_environment_id = azurerm_container_app_environment.main.id

  # Five minutes is generous for reading a quarter of an hour of spans and
  # judging a fifth of them, and a run that has not finished in five minutes has
  # something wrong with it that a longer timeout would hide rather than fix.
  replica_timeout_in_seconds = 300
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = one(azurerm_container_registry.main[*].login_server)
    identity = azurerm_user_assigned_identity.app.id
  }

  schedule_trigger_config {
    cron_expression          = var.monitors_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "monitors"
      image  = local.monitors_image
      cpu    = 0.5
      memory = "1.0Gi"

      command = ["python", "-m", "chip_chat.eval.online"]
      args    = concat(["--phoenix", local.phoenix_endpoint], var.monitors_args)

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }

      # The judged monitors are model calls, and #76's last acceptance criterion
      # is that they are accounted inside the same daily ceiling the request
      # path enforces rather than beside it. `python -m chip_chat.eval.online`
      # exits non-zero when this is unset, on purpose, so a job that lost it
      # fails loudly instead of running an unaccounted judge.
      env {
        name  = "CHIP_CHAT_DAILY_TOKEN_CEILING"
        value = tostring(var.spend_caps.daily_token_ceiling)
      }

      # Judging needs a model, and it is deliberately the same deployment the
      # product answers with. A judge on a different model produces a number
      # that cannot be compared to the offline baselines in `eval/*/BASELINE.md`.
      env {
        name  = "CHIP_CHAT_FOUNDRY_ENDPOINT"
        value = azurerm_cognitive_account.foundry.endpoint
      }

      env {
        name  = "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT"
        value = var.chat_deployment
      }

      env {
        name  = "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT"
        value = var.vision_deployment
      }

      env {
        name  = "CHIP_CHAT_ENVIRONMENT"
        value = var.environment
      }
    }
  }

  tags = local.tags

  lifecycle {
    # The same reason `azurerm_container_app.web` ignores its image: the tag is
    # a commit, it moves on every deploy, and Terraform must not drag a deployed
    # job back to whatever tag was last written into a tfvars file.
    ignore_changes = [template[0].container[0].image]
  }
}
