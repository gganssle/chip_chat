variable "subscription_id" {
  description = "Azure subscription the stack is deployed into."
  type        = string
  default     = "c8b63a71-218d-4d4c-991c-b963ed2fd1f0"

  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.subscription_id))
    error_message = "subscription_id must be a GUID."
  }
}

variable "location" {
  description = <<-EOT
    Azure region. Forced to East US 2, not preferred: Snowflake Cortex Analyst is
    natively available on Azure in East US 2 and West Europe only, and a Snowflake
    account's region is fixed at signup. See
    docs/service-inventory.md#region-recommendation-east-us-2.
  EOT
  type        = string
  default     = "eastus2"
}

variable "environment" {
  description = <<-EOT
    Stack name. "demo" is the live stack and produces the unsuffixed resource
    names that Phase 0 created by hand; anything else produces a parallel,
    disposable stack in its own resource group.
  EOT
  type        = string
  default     = "demo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,10}$", var.environment))
    error_message = "environment must be 2-11 lowercase alphanumerics starting with a letter (it feeds storage account names, which cap at 24)."
  }
}

variable "adopt_existing_foundation" {
  description = <<-EOT
    True when this stack adopts the resource group, Key Vault, managed identity,
    cost action group and budget that issue #3 created imperatively with the az
    CLI, rather than creating them. Set false for a disposable stack that builds
    its own foundation from nothing. See infra/README.md, "Adopting Phase 0".
  EOT
  type        = bool
  default     = true
}

# --- Cost guardrails --------------------------------------------------------
#
# These are defaults, not documentation. Every one of them is the cheap option
# and changing it should feel like a decision.

variable "monthly_budget_usd" {
  description = "Subscription budget ceiling in USD. Alerts only — a budget prevents nothing."
  type        = number
  default     = 150
}

variable "budget_start_date" {
  description = "First day of the budget's first month, RFC3339. Must be the first of a month and cannot be changed after creation."
  type        = string
  default     = "2026-08-01T00:00:00Z"
}

variable "budget_end_date" {
  description = "Budget expiry, RFC3339."
  type        = string
  default     = "2029-08-01T00:00:00Z"
}

variable "cost_alert_email" {
  description = "Address that receives budget notifications."
  type        = string
  default     = "grahamganssle@gmail.com"
}

variable "search_sku" {
  description = <<-EOT
    Azure AI Search tier. "free" is the decision recorded in
    docs/service-inventory.md#the-reranker-decision-issue-10: the semantic
    reranker runs on Free, capped at 1,000 semantic queries a month, and Basic is
    ~$74/month. Free is limited to one service per subscription and has no
    managed identity, which is why local_authentication_enabled stays on below.
  EOT
  type        = string
  default     = "free"

  validation {
    condition     = contains(["free", "basic", "standard"], var.search_sku)
    error_message = "search_sku must be one of: free, basic, standard."
  }
}

variable "content_safety_sku" {
  description = "Content Safety tier. F0 is free: 5,000 text records and 5,000 images a month, one free account per subscription."
  type        = string
  default     = "F0"
}

variable "document_intelligence_sku" {
  description = "Document Intelligence tier. F0 is free, one free account per subscription."
  type        = string
  default     = "F0"
}

variable "foundry_sku" {
  description = "Microsoft Foundry (AIServices) account tier. S0 is pay-per-token; there is no free tier for model inference."
  type        = string
  default     = "S0"
}

variable "uploads_retention_days" {
  description = <<-EOT
    Days after creation before an uploaded photo is deleted by the lifecycle
    rule. 1 is the tightest a lifecycle rule can express — the engine has day
    granularity and takes up to 24 hours to first run, so real behaviour is
    deletion 24-48 hours after upload. Say "within 48 hours" in user-facing copy.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.uploads_retention_days >= 1
    error_message = "Lifecycle rules have day granularity; the minimum is 1."
  }
}

variable "log_retention_days" {
  description = "Log Analytics retention. 30 is the free floor; longer costs money."
  type        = number
  default     = 30
}

variable "log_daily_quota_gb" {
  description = <<-EOT
    Hard daily ingestion cap on the Log Analytics workspace, in GB. This is a
    real spend control: ingestion is billed per GB and a crash-looping container
    can generate a lot of it overnight. Ingestion stops when the cap is hit.
  EOT
  type        = number
  default     = 1
}

# --- Container App ----------------------------------------------------------

variable "web_min_replicas" {
  description = <<-EOT
    Minimum replicas for the chat app. ZERO is the default and the point: an idle
    replica still bills, at roughly an eighth of the active vCPU rate. Scale to
    one only while actively sharing the link.
  EOT
  type        = number
  default     = 0
}

variable "web_max_replicas" {
  description = <<-EOT
    Ceiling on replicas. ONE, and not for cost.

    The spend cap's counters are process-local (api/README.md, "What is not here
    yet"). Two replicas are two ledgers, so the daily token ceiling would mean
    twice what it says and the per-session cap would apply to whichever replica
    happened to answer. A ceiling that silently doubles under load is not a
    ceiling.

    Raise this only once BudgetLedger and SourceRateLimiter are behind a shared
    store. The container also runs a single uvicorn worker for the same reason.
  EOT
  type        = number
  default     = 1
}

variable "web_image" {
  description = <<-EOT
    Container image for the chat app. The default is Microsoft's quickstart
    image: Phase 0 stands up the environment, it does not ship the app. The real
    image is pushed by deployment, and Terraform deliberately ignores changes to
    this field afterwards so a deploy is not reverted by the next apply.
  EOT
  type        = string
  default     = "mcr.microsoft.com/k8se/quickstart:latest"
}

variable "web_target_port" {
  description = <<-EOT
    Port the chat app container listens on. 8000, not 80: the container runs as
    an unprivileged user and ports below 1024 need a capability it deliberately
    does not have. Ingress is 443 either way — this is the port behind it.
  EOT
  type        = number
  default     = 8000
}

variable "container_registry_sku" {
  description = <<-EOT
    Container registry tier. Basic is the cheapest ACR there is, at roughly
    $5/month; there is no free tier. It is a variable rather than a literal
    because it is the only standing charge this stack adds that is not
    pay-per-use, and a standing charge should be visible.
  EOT
  type        = string
  default     = "Basic"

  validation {
    condition     = contains(["Basic", "Standard", "Premium"], var.container_registry_sku)
    error_message = "container_registry_sku must be one of: Basic, Standard, Premium."
  }
}

# --- The spend cap ----------------------------------------------------------
#
# api/README.md: "Issue #85 trips the ceiling against the real deployment.
# SpendLimits.from_env is how that is done without a code change." These are
# that. Every one is optional in code and defaulted small there too; they are
# repeated here because the numbers guarding a URL with no authentication in
# front of it should be reviewable in the same diff as the URL.

variable "chat_deployment" {
  description = <<-EOT
    Which entry of var.model_deployments answers the agent's conversational
    lane. This is the eval swap point (issue #8): change it, restart, and the
    agent runs on a different model with no code change.
  EOT
  type        = string
  default     = "gpt-5-mini"
}

variable "vision_deployment" {
  description = "Which entry of var.model_deployments answers the photo lane."
  type        = string
  default     = "gpt-4.1-mini"
}

variable "spend_caps" {
  description = <<-EOT
    The inline spend cap's ceilings, passed to the app as CHIP_CHAT_* settings.
    Defaults match the ones in api/src/chip_chat/api/limits.py.

    turn_token_reservation is the one to think hardest about: it is what a turn
    is charged against the daily ceiling *before* the model answers, and setting
    it below the worst turn the agent can produce is the one way concurrent
    turns can collectively overshoot.
  EOT
  type = object({
    daily_token_ceiling        = optional(number, 2000000)
    session_turn_cap           = optional(number, 40)
    session_token_cap          = optional(number, 120000)
    source_requests_per_window = optional(number, 20)
    source_window_seconds      = optional(number, 60)
    turn_token_reservation     = optional(number, 8000)
    budget_reset_timezone      = optional(string, "UTC")
  })
  default = {}
}

variable "kill_switch" {
  description = <<-EOT
    The circuit breaker, as an application setting. Anything not recognisably
    "off" ("", 0, false, no, off, run) stops the app on its next check.

    It is set explicitly, to "off", rather than left absent on purpose: the
    runbook in api/README.md promises a stop that takes a minute from a phone,
    and a setting that is already in the portal is one edit away from thrown.
    An absent one has to be created first, by somebody who remembers its name.
  EOT
  type        = string
  default     = "off"
}

variable "otlp_endpoint" {
  description = <<-EOT
    Where agent-observability spans go. Empty while the deployed app exports
    only to Application Insights; issue #78 points it at Arize AX, which
    decision D6 says must be an endpoint and a header and nothing else.
  EOT
  type        = string
  default     = ""
}

# --- Model deployments ------------------------------------------------------

variable "model_deployments" {
  description = <<-EOT
    Model deployments on the Foundry account, keyed by deployment name.

    The defaults are issue #8's choices. Two things constrained them, and both
    are worth knowing before editing this map.

    First, quota. Most model families report a limit of **zero** TPM in East US 2
    on this subscription — gpt-5.4, gpt-5.1, gpt-5, gpt-4o and gpt-4.1 all do —
    so "the newest model" was never on the menu. Read the real numbers with

      az cognitiveservices usage list -l eastus2 -o table

    and treat a family missing from that list, or present with a limit of 0, as
    unavailable rather than as something to raise a support ticket about mid-demo.

    Second, blast radius. The chat and vision lanes are deliberately on
    *different* models so that they draw on different quota pools: a burst of
    photo uploads cannot starve the agent's conversational TPM, and vice versa.
    That is the main reason they are not one deployment used twice.

    Capacity is thousands of tokens per minute and is a spend control as much as
    a performance setting. Both GlobalStandard deployments are pay-per-token and
    carry no standing hourly charge — only a provisioned SKU would. See
    docs/phase-0-verification.md for the measured verification and the prices.

    Deployment names are model names, and the *role* mapping lives in
    configuration (CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT and
    ...VISION_DEPLOYMENT), not here. That is what makes an eval experiment a new
    entry in this map plus an environment variable, rather than a rename that
    breaks whatever was pointing at the old name.
  EOT
  type = map(object({
    model_name    = string
    model_version = optional(string)
    model_format  = optional(string, "OpenAI")
    sku_name      = optional(string, "GlobalStandard")
    capacity      = optional(number, 10)
  }))

  default = {
    # The agent's chat and tool-calling model. `agentsV2` capable, which is what
    # the hosted agent runtime (docs/decisions/foundry-agent-shape.md) needs.
    # Cheapest input token of anything with quota here, and input is where an
    # agent loop spends: every tool result is replayed into the next request.
    "gpt-5-mini" = {
      model_name    = "gpt-5-mini"
      model_version = "2025-08-07"
      sku_name      = "GlobalStandard"
      capacity      = 10
    }

    # The photo lane's model. Non-reasoning on purpose: describing a burrito bowl
    # is a single-shot perception call, and a reasoning model would bill thinking
    # tokens for it. Cheaper output than gpt-5-mini, and its own quota pool.
    "gpt-4.1-mini" = {
      model_name    = "gpt-4.1-mini"
      model_version = "2025-04-14"
      sku_name      = "GlobalStandard"
      capacity      = 10
    }
  }

  validation {
    condition = alltrue([
      for d in values(var.model_deployments) : !can(regex("Provisioned", d.sku_name))
    ])
    error_message = "Provisioned SKUs bill by the hour whether or not a token is spent. Nothing in this stack prevents that spend — see docs/phase-0-verification.md. Use a Standard SKU."
  }
}

# --- Adoption of the Phase 0 role assignments -------------------------------
#
# Role assignment names are server-generated GUIDs. Everything else about the
# Phase 0 estate can be derived from its name; these cannot, so they are
# supplied. See imports.tf for how to read them back, and set either to "" to
# have Terraform create the grant instead of adopting one.

variable "adopted_key_vault_admin_assignment_id" {
  description = "GUID of the existing 'Key Vault Administrator' role assignment for the developer on the Key Vault."
  type        = string
  default     = "09de9d45-ba8b-4028-8d14-7dc298c8c74d"
}

variable "adopted_key_vault_secrets_user_assignment_id" {
  description = "GUID of the existing 'Key Vault Secrets User' role assignment for the app identity on the Key Vault."
  type        = string
  default     = "735743f9-99b0-4b32-8abf-e5ce744edf4b"
}

variable "search_enabled" {
  description = <<-EOT
    Escape hatch for a regional capacity outage, not a design choice. On
    2026-08-25 East US 2 returned InsufficientResourcesAvailable for every
    attempt to create a Free-tier search service — subscription quota was fine
    (0 of 1 used); Azure's shared free pool in the region was full. The region
    is fixed by Snowflake Cortex Analyst, so waiting is the only option that
    keeps the rest of the design intact.

    Set false to apply the rest of the estate while the pool is full, then set it
    back to true and re-apply. Retrieval is Phase 5, so this blocks nothing
    earlier. Tracked as cc-3wo.
  EOT
  type        = bool
  default     = true
}
