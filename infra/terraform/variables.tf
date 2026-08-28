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

variable "search_location" {
  description = <<-EOT
    Azure region for the AI Search service ONLY. This is the one resource in the
    estate that does not live in var.location, and the difference is deliberate:
    do not "fix" it back to eastus2 or retrieval breaks again.

    East US 2 is out of AI Search capacity at every tier — free, basic and
    standard alike — and has been since 2026-08-25 (cc-3wo). Paying was tried and
    returns the identical InsufficientResourcesAvailable (cc-6wz): money is not
    the lever, capacity is. East US, next door, has capacity — a free-tier
    service was created there successfully on 2026-08-26 (cc-okc).

    Moving ONE service is not moving the region. var.location stays eastus2
    because Snowflake Cortex Analyst is Azure-native in East US 2 and West Europe
    only and a Snowflake account's region is fixed at signup
    (docs/service-inventory.md item 7). That constraint binds Cortex Analyst, and
    through it the rest of the estate; it does not bind AI Search, which talks to
    nothing region-pinned. Nothing else here should follow this variable out.

    The cost of the split is a cross-region hop on every retrieval call: the
    agent runs in eastus2 Container Apps and queries an index in eastus. Measured
    at provisioning time, see search.tf for the number.
  EOT
  type        = string
  default     = "eastus"
}

variable "search_sku" {
  description = <<-EOT
    Azure AI Search tier. Back to "free" as of 2026-08-26, after the region moved
    to East US (var.search_location).

    Basic was authorised by the account owner on the theory that the exhausted
    pool was the free one and paying would step around it. It would not have:
    eastus2 was out of capacity at every tier. Once the constraint turned out to
    be the region rather than the tier, and East US free-tier capacity was
    confirmed to exist, Basic bought nothing that Free does not — so this stays
    Free and the ~$73.73/month stays unspent. This restores the recorded decision
    in docs/service-inventory.md#the-reranker-decision-issue-10, which was right
    on the merits all along.

    Free comes with two consequences the rest of the estate has to absorb, and
    they are the reason Basic in East US remains pre-authorised if either turns
    out to be unworkable:

      * Semantic ranking still EXISTS — the "free" semantic plan is accepted on a
        Free SKU and search.tf now sets it explicitly, because leaving it unset
        disables reranking rather than capping it. What Free changes is the shape
        of the ceiling: 1,000 semantic requests a month, then a billing error
        rather than a charge. That is a hard stop, not an overage, so the
        degrade-to-hybrid-without-reranking path in #49 is load-bearing code and
        not a fallback nobody will hit. Keep the counter in the retrieval eval
        harness (#10).
      * No managed identity ON the search service, which rules out an indexer
        reaching a data source under its own identity. It does NOT rule out RBAC
        INTO the service: the app's user-assigned identity holds data-plane roles
        and local authentication is off, on Free, verified. See search.tf.

    Free is also one service per subscription, which is a quota, not capacity —
    a second one fails with ServiceQuotaExceeded and that error means the estate
    already has its free service, not that the region is full again.
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

variable "embedding_deployment" {
  description = <<-EOT
    Which entry of var.model_deployments answers the knowledge lane (issue #48).

    Changing this is not the same kind of change as changing chat_deployment.
    The chat lane swaps on a restart; a different embedding model is a different
    vector space, so every document in the index has to be re-embedded before a
    query can be. That is a `make search-build`, which is a rebuild and an alias
    swap rather than a restart -- and it is one of the things the alias makes
    cheap rather than frightening.
  EOT
  type        = string
  default     = "text-embedding-3-small"
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
    Where agent-observability spans go, as an override.

    Empty is not "nowhere" any more. Empty means "the Phoenix container app in
    this environment", which observability.tf computes from the resource itself
    so it cannot go stale. Setting this to a value points the app somewhere else
    instead — a hosted Arize AX endpoint, a collector, a laptop through a tunnel
    — which is the whole of the app tier's half of decision D6, unchanged.
  EOT
  type        = string
  default     = ""
}

# --- The agent-observability backend ----------------------------------------
#
# See docs/decisions/hosted-phoenix.md. Free tier only was the owner's
# instruction, and self-hosting the open-source backend of the same vendor is
# the version of "free" that does not require signing anybody up for anything.

variable "phoenix_enabled" {
  description = <<-EOT
    Whether to run the agent-observability backend in this environment.

    True by default, and turning it off is a deliberate act with a consequence
    worth knowing: the app then exports only to Application Insights, and every
    monitor in chip_chat.eval.online loses its trace source. PRD section 12
    makes online evals a launch criterion, so false is a pre-launch state and
    not a saving.
  EOT
  type        = bool
  default     = true
}

variable "phoenix_image" {
  description = <<-EOT
    The Phoenix image, pinned, and pinned to the same version compose.yaml uses.

    A dev loop and a deployment that disagree about the backend's version are
    worse than either alone: every difference between a local span tree and a
    production one then has two candidate causes instead of one.
    infra/tests/test_local_stack.py fails if this and compose.yaml drift apart,
    so bumping one means bumping both in the same commit.
  EOT
  type        = string
  default     = "docker.io/arizephoenix/phoenix:version-20.3.0"
}

variable "monitors_image_tag" {
  description = <<-EOT
    The tag of the monitors image the scheduled job is CREATED with.

    Built by `make monitors-image-push`, which is `az acr build` against
    eval/Dockerfile — the chip-chat-eval package rather than chip-chat-api, so
    the job carries chip_chat.eval.online and the app image does not have to.

    "latest" is a moving tag and this repository is otherwise strict that a
    deployed thing points at an immutable one, so the exception needs its
    reason. It is the same one azurerm_container_app.web has: Terraform creates
    the resource and then stops owning the image (`ignore_changes`), and
    `make monitors-deploy` immediately moves it to a commit-tagged digest, the
    way `make deploy` does for the app. What this default buys is that a fresh
    `terraform apply` produces a job that runs rather than a job that cannot
    start, without anybody having to remember a -var.
  EOT
  type        = string
  default     = "latest"
}

variable "monitors_cron" {
  description = <<-EOT
    How often the monitors run, as a cron expression in UTC.

    Every fifteen minutes. The number is a trade between two costs that pull in
    opposite directions: a longer interval means a disclosure signal sits
    undetected for longer, and a shorter one means more job starts and more
    overlap between windows. Fifteen minutes is short enough that the worst-case
    detection delay is under a coffee break and long enough that a run costs
    seconds of vCPU rather than minutes.
  EOT
  type        = string
  default     = "*/15 * * * *"
}

variable "monitors_args" {
  description = <<-EOT
    Arguments appended to the scheduled monitor run, after --phoenix.

    The default asks for the last twenty minutes against a fifteen-minute
    schedule, which overlaps on purpose: a turn that lands between the end of
    one window and the start of the next would otherwise be seen by nobody, and
    a monitor firing twice on one trace is a duplicate alert while a monitor
    firing on none is a missed one.

    --judge is on. Four of the six monitors need no model and run on every turn
    regardless; the two that do need one are the groundedness and over-refusal
    judges, and eval/online/README.md measures their share of the daily ceiling
    at under five percent even on the pessimistic figure. Removing it here makes
    the loop free and blind to exactly the two failures a demo is judged on.

    --fail-on page is the routing, and it is the only routing this system has.
    chip_chat.eval.online deliberately delivers no alerts — the route is
    somebody's action group and an eval package that held a delivery mechanism
    would be untestable and the delivery unowned — so the CALLER routes, and for
    a scheduled job the exit status is the route. A disclosure signal fails the
    execution, which is visible in `az containerapp job execution list` and is
    something Azure Monitor can alert on without this repository knowing
    anything about Azure Monitor. Everything below `page` is reported and does
    not fail the run, which is right: a dashboard-severity finding means
    something as a rate and nothing as an instance.
  EOT
  type        = list(string)
  default     = ["--lookback-minutes", "20", "--judge", "--fail-on", "page"]
}

# --- Container registry -----------------------------------------------------

variable "container_registry_enabled" {
  description = <<-EOT
    Whether to create the container registry. True by default: decision D8 made
    the agent a hosted agent, so an image and a registry are part of the estate
    rather than an optional extra.

    False is for a disposable stack that only needs the data and model tiers --
    it saves the Basic tier's fixed daily charge, and nothing in Phases 0-6
    depends on the registry.
  EOT
  type        = bool
  default     = true
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

    # The knowledge lane's embeddings (issue #48), at both ends of integrated
    # vectorization: the index build calls it for document vectors, and the
    # index's own vectorizer calls it for query vectors.
    #
    # The subscription chose this, the same way it chose the two above. Read on
    # 2026-08-27 with `az cognitiveservices usage list -l eastus2`:
    # text-embedding-3-LARGE reports a limit of 0 on GlobalStandard,
    # DataZoneStandard and Standard alike, and -small reports 1,000. ada-002 has
    # Standard quota and is the previous generation at a higher price.
    #
    # Its own quota pool again, for the blast-radius reason the chat and vision
    # split has: a corpus rebuild embeds every chunk in the corpus in a few
    # minutes, and it must not be able to starve a conversation while it does.
    "text-embedding-3-small" = {
      model_name    = "text-embedding-3-small"
      model_version = "1"
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

variable "search_alias" {
  description = <<-EOT
    The one search index name the application ever knows (issue #48).

    It is an ALIAS, not an index. RFC-001 section 08: the index is rebuilt,
    never patched -- each weekly re-harvest builds a new index named after the
    corpus release it holds, and one alias write makes it live. The application
    is given this name and never learns the other one, which is what makes the
    swap invisible to it.

    Terraform does not create the alias. It is a data-plane object, it is
    created by the first `make search-build`, and a Terraform resource for it
    would fight the build for ownership of the one write the whole design turns
    on. What Terraform owns is the *name*, so that the app and the build agree
    on it without either hardcoding it.
  EOT
  type        = string
  default     = "corpus"
}

variable "search_enabled" {
  description = <<-EOT
    Escape hatch for a regional capacity outage, not a design choice. On
    2026-08-25 East US 2 returned InsufficientResourcesAvailable for every
    attempt to create a search service — subscription quota was fine (0 of 1
    used at Free, 0 of 16 at Basic); the region itself was out of AI Search
    capacity at every tier. Setting this false applied the rest of the estate
    while that lasted. Tracked as cc-3wo.

    RESOLVED 2026-08-26 by moving the service to East US (var.search_location)
    rather than by waiting or by paying, so this hatch is closed and stays
    `true`. It is kept, rather than deleted, because the failure it exists for
    is Azure-side and can recur in any region: if East US fills up too, false
    unblocks the rest of an apply. Do not commit it false — a committed false
    goes on silently skipping the service long after the outage clears, whereas
    a loud failure self-heals.
  EOT
  type        = bool
  default     = true
}

# --- Databricks -------------------------------------------------------------

variable "databricks_sku" {
  description = <<-EOT
    Workspace tier. Premium, and the precondition in databricks.tf enforces it.
    Standard retires 2026-10-01 and Unity Catalog requires Premium, so a Standard
    workspace created now cannot survive this project. "trial" is Premium for 14
    days on up to $400 of credits and then bills normally — it changes the
    invoice, not the feature set.
  EOT
  type        = string
  default     = "premium"

  validation {
    condition     = contains(["premium", "trial"], var.databricks_sku)
    error_message = "databricks_sku must be premium or trial. Standard tier retires 2026-10-01 and cannot run Unity Catalog."
  }
}

variable "databricks_autotermination_minutes" {
  description = <<-EOT
    Minutes of idleness before compute shuts itself off, fixed by the job and
    interactive cluster policies. This is the number in the design's cost
    guardrail and it is fixed rather than a maximum, because a maximum still
    lets someone type 4320. Not applied to the pipeline policy: pipeline compute
    rejects the attribute outright.
  EOT
  type        = number
  default     = 10

  validation {
    condition     = var.databricks_autotermination_minutes >= 10 && var.databricks_autotermination_minutes <= 120
    error_message = "Databricks will not accept an autotermination below 10 minutes, and above 120 this stops being a guardrail."
  }
}

variable "databricks_node_type" {
  description = <<-EOT
    Default VM for single-node compute. Small on purpose: the whole dataset is a
    restaurant menu. The VM is billed beside the DBUs, so this number is half the
    cost of a run and the DBU rate is the other half. $0.343/hour in East US 2 as
    of 2026-08-26, 4 vCPU and 16 GB.

    It is not the cheapest 4-vCPU type that fits, and the reason is worth reading
    before someone "optimises" it. East US 2 will not start most of them:

      * `Standard_D4ds_v5` -- Databricks' OWN default node type -- has a quota of
        **zero cores** on this subscription (`standardDDSv5Family`).
      * `Standard_D4ds_v4`, `Standard_DS3_v2`, `Standard_F4s_v2`,
        `Standard_D4s_v3` and `Standard_E4ds_v4` are **not offered in East US 2
        at all**, however plausible they look in a pricing page.
      * `Standard_D4ds_v4` additionally failed live with
        `CLOUD_PROVIDER_RESOURCE_STOCKOUT` / "Capacity Restrictions" -- the same
        regional-capacity shortage that is currently blocking AI Search.

    `Standard_F4ads_v7` is the one 4-vCPU type with quota and **no restrictions
    of any kind** on this subscription. `Standard_D4ads_v6` is $0.228/hour and
    would do, at the cost of two of the three availability zones.

    Two ways this fails, and neither is at plan time: a zero-quota family fails
    minutes into the run with `QuotaExceeded`, and a capacity-restricted one
    hangs on "Finding instances for new nodes" until the job's timeout. Verify
    before changing:

      az vm list-usage -l eastus2 -o table
      az vm list-skus -l eastus2 --resource-type virtualMachines \
        --query "[?name=='<sku>'].{n:name,f:family,r:restrictions}"

    The whole-region ceiling is 10 cores, so one 4-vCPU single-node cluster fits
    and two do not.
  EOT
  type        = string
  default     = "Standard_F4ads_v7"
}

variable "databricks_node_type_allowlist" {
  description = <<-EOT
    VMs the cluster policies will accept. An allowlist rather than a fixed value
    so that a genuinely larger job can be run deliberately — editing this list is
    the moment someone decides to spend more, which is where that decision
    belongs.
  EOT
  type        = list(string)
  # Every entry is 4 vCPU, because the region's total quota is 10 cores; every
  # entry is from a family this subscription has quota in; and every entry is
  # actually offered in East US 2. All three were checked on 2026-08-26 against
  # `az vm list-usage` and `az vm list-skus`, because each one fails differently
  # and none of them fail at plan time. Prices are East US 2 Linux
  # pay-as-you-go on the same date.
  default = [
    "Standard_F4ads_v7", # 16 GB, $0.343/hr - the default; no restrictions at all
    "Standard_D4ads_v6", # 16 GB, $0.228/hr - cheapest, but only zones 1 and 3
    "Standard_F4as_v7",  # 16 GB, $0.273/hr - zones 1 and 2
    "Standard_E4ads_v6", # 32 GB, $0.290/hr - memory headroom, zones 1 and 3
    "Standard_L4aos_v4", # 32 GB, $0.452/hr - unrestricted fallback
  ]

  validation {
    condition     = length(var.databricks_node_type_allowlist) > 0
    error_message = "The allowlist cannot be empty: a policy with no permitted node type cannot create any cluster at all."
  }
}

variable "databricks_spark_version" {
  description = <<-EOT
    Databricks Runtime for the smoke job. Pinned to an LTS release rather than
    resolved with a data source, so that a first apply against a workspace that
    does not exist yet does not have to read from it. Check what the workspace
    actually offers with:

      databricks clusters spark-versions --profile <profile>
  EOT
  type        = string
  default     = "17.3.x-scala2.13"
}

variable "databricks_restrict_cluster_create" {
  description = <<-EOT
    Whether to take cluster and instance-pool creation away from the `users`
    group, so that everyone who is not a workspace admin must go through a
    policy. True is the guardrail. Set false only if you are debugging an
    entitlement problem and know you are widening the blast radius.
  EOT
  type        = bool
  default     = true
}

variable "databricks_unity_catalog_enabled" {
  description = <<-EOT
    Whether to create the storage credential and external locations. These are
    metastore-level objects and need the workspace to have a metastore attached.
    Workspaces created after 2023-11-09 get one assigned automatically, and from
    2026-09-30 every new workspace is UC-only — so this is on by default. Set
    false if the account has no auto-assign metastore in this region yet, apply
    the rest, set an auto-assign metastore in the account console, and set it
    back.
  EOT
  type        = bool
  default     = true
}

variable "databricks_account_id" {
  description = <<-EOT
    Databricks account id. Not a secret and not derivable from the Azure
    subscription: an Azure Databricks account is created per Entra tenant by
    Databricks, with its own GUID. Needed because binding a job's `run_as` to a
    service principal is an account-level permission.

    Read it from https://accounts.azuredatabricks.net (top right), or provoke it
    out of the API, which names the account in its error:

      databricks api get "/api/2.0/preview/accounts/access-control/rule-sets\
        ?name=accounts/00000000-0000-0000-0000-000000000000/servicePrincipals/x/ruleSets/default&etag="
  EOT
  type        = string
  default     = "43b2ef58-9ed2-4bb9-9962-8b20eb5cd8b4"

  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.databricks_account_id))
    error_message = "databricks_account_id must be a GUID."
  }
}

variable "databricks_service_principal_token_enabled" {
  description = <<-EOT
    Whether to mint a Databricks PAT for the jobs service principal and store it
    in Key Vault. OFF, and the default is the recommendation rather than a
    limitation: the app tier authenticates to Databricks with the
    `id-chip-chat-app` managed identity over Entra, so the normal path has no
    secret to store, rotate or leak.

    It is also currently impossible without a human. On-behalf-of token creation
    is disabled for new Databricks accounts and there is no workspace API for it;
    an account admin must turn it on at
    https://accounts.azuredatabricks.net -> Settings -> Feature enablement ->
    "Personal access tokens for service principals". Verified against this
    account on 2026-08-26. Turn that on first, then set this true.
  EOT
  type        = bool
  default     = false
}

variable "databricks_token_lifetime_days" {
  description = <<-EOT
    Lifetime of the service principal's PAT. Rotate before it lapses with

      terraform apply -replace=databricks_obo_token.jobs

    which reissues the token and rewrites the Key Vault secret in one step.
  EOT
  type        = number
  default     = 90

  validation {
    condition     = var.databricks_token_lifetime_days > 0 && var.databricks_token_lifetime_days <= 365
    error_message = "Token lifetime must be between 1 and 365 days. A token that never expires is not a credential, it is a liability."
  }
}

variable "databricks_smoke_timeout_seconds" {
  description = "Ceiling on the smoke job. A run that has not finished by now is not going to, and the useful thing is for it to stop billing."
  type        = number
  default     = 900
}

variable "databricks_uc_probe_timeout_seconds" {
  description = <<-EOT
    Ceiling on the two Unity Catalog verification jobs (gh-32). Longer than the
    ADLS smoke job because the lineage probe polls: Unity Catalog records lineage
    asynchronously, so the notebook waits for the graph to resolve rather than
    reading it once and calling an early answer a failure.
  EOT
  type        = number
  default     = 1800
}

variable "databricks_catalog_owner" {
  description = <<-EOT
    Unity Catalog owner of the catalog and its six schemas. Empty means the
    identity running Terraform.

    It must be an ACCOUNT-level principal — an account user, an account group or
    a service principal. The workspace-local `admins` and `users` groups are not
    ones, and Unity Catalog rejects them with "Could not find principal with
    name admins", which reads like a typo and is not (verified 2026-08-26).

    An account group is the right answer and cannot be created from here: it
    needs a provider pointed at accounts.azuredatabricks.net and an account
    admin to run it. Set this to that group's name once it exists, and the
    catalog changes hands in one apply.
  EOT
  type        = string
  default     = ""
}

variable "databricks_recommender_schedule_enabled" {
  description = <<-EOT
    Whether the weekly retraining job's schedule is RUNNING. False leaves the
    schedule declared and PAUSED, which is the shipped default.

    Issue #37's fourth acceptance criterion is that retraining is a scheduled
    job rather than a notebook somebody remembers to run, and the rest of this
    directory says nothing in the workspace should be able to start spending on
    its own. Both hold: the cron is declared in Terraform where a person can
    read and review it, and it does not fire until this is set. Turning
    retraining on is one variable and one apply.

    Turn it on when the lakehouse is loaded and the marts are current -- a
    training run against an empty silver layer registers a model fitted on
    nothing, which is worse than no model, because it has a version number.
  EOT
  type        = bool
  default     = false
}

variable "databricks_recommender_timeout_seconds" {
  description = <<-EOT
    Ceiling on a retraining run: both tasks, one cluster start. Longer than the
    verification jobs because it fits twice -- once over the training window for
    the holdout, once over the whole history for the version that gets
    registered -- and then batch-scores every visitor.

    A run that has not finished by now is not going to, and the useful thing is
    for it to stop billing.
  EOT
  type        = number
  default     = 3600
}

# ---------------------------------------------------------------------------
# The nightly publish (gh-39). Databricks to Snowflake.
#
# Snowflake is not managed by this Terraform at all -- `snowflake/sql/` builds
# the account and `make snowflake-apply` runs it, for the reason
# `snowflake/README.md` gives. What is here is the two facts the Databricks job
# needs in order to find it, and neither of them is a secret: an account URL and
# a user name. The private key lives in a Databricks secret scope that this
# Terraform creates empty and never writes to.
# ---------------------------------------------------------------------------

variable "snowflake_account_url" {
  description = <<-EOT
    The Snowflake account host the nightly publish connects to, e.g.
    hq72718.us-east-2.aws.snowflakecomputing.com. No scheme and no trailing
    slash -- the connector wants a host.

    Empty means the publish job is not created at all, which is the shipped
    default: a job pointed at no account would fail on its first scheduled run
    and email somebody about a system that was never stood up.

    AWS us-east-2, not Azure East US 2. The region was fixed when the trial was
    created and cannot be changed; GitHub #104 has the decision and its costs.
  EOT
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# The chat app's read connection (cc-lpy4). Container App to Snowflake.
#
# The same rule as the block above: Snowflake is not managed here, no key
# material enters Terraform state, and what is declared is the handful of names
# the app needs in order to find an account it did not create. The private key
# is a Container Apps secret whose value is a Key Vault *reference* -- the
# platform resolves it with the app's managed identity, so the value is never in
# a plan, a state file or a container image.
# ---------------------------------------------------------------------------

variable "snowflake_account" {
  description = <<-EOT
    The account locator the chat app opens read connections against, e.g.
    hq72718.us-east-2.aws. This is the locator form `snow connection add` wants
    and `CURRENT_ACCOUNT()` returns, not the organisation form -- .env.example
    has both and says which is which.

    Empty is a supported deployment and is what shipped before cc-lpy4: the app
    comes up with no pool, assigns personas from the roster committed in its own
    image, and answers `get_points_balance` and `get_usual_order` from the
    hardcoded fixture. `docs/decisions/shipped-persona-roster.md` is that path
    and it still works; `GET /healthz/lanes` reports it as `not_wired` rather
    than as an outage.

    Setting it is not enough on its own. The app also needs a private key, which
    is `snowflake_app_key_secret` below, and it will say which of the two is
    missing in its first log lines.

    THE DEFAULT IS THE LIVE ACCOUNT, AND THAT IS DELIBERATE. There is no
    terraform.tfvars in this repository -- the defaults in this file *are* the
    deployment -- so an empty default would mean one `terraform apply` by
    somebody who did not pass `-var` silently un-wired the account lane and
    restored the contradiction docs/public-demo.md §9 records. A locator is not a
    credential: it is committed in .env.example already, and on its own it grants
    nothing.

    The cost of this default is a second stack. `terraform apply -var
    environment=scratch` builds its own Key Vault, and a Container App secret
    that references a secret which is not in it produces a revision that will not
    start. That failure is loud and names the secret, and the fix is one flag --
    `-var snowflake_account=""` for a stack that does not need the read lanes, or
    put the key in the new vault first for one that does.
  EOT
  type        = string
  default     = "hq72718.us-east-2.aws"
}

variable "snowflake_app_key_secret" {
  description = <<-EOT
    The Key Vault secret holding CHIP_CHAT_APP's private key, as unencrypted
    PKCS#8 PEM. `snowflake/sql/04_users.sql` creates the user with TYPE =
    SERVICE, and a SERVICE user cannot authenticate with a password -- key-pair
    is the only option, which is why this is a key and not a credential pair.

    Terraform never reads the value. The Container App gets a secret whose
    source is a versionless Key Vault reference resolved by the app's own
    managed identity, which already holds Key Vault Secrets User on the vault
    (foundation.tf). An operator puts the key there once:

      az keyvault secret set --vault-name <vault> \
        --name snowflake-app-private-key \
        --file ~/.snowflake/keys/chip_chat_app.p8

    `snowflake-ops-private-key` is the other one and belongs to the write tier.
    They are deliberately two secrets rather than one: the chat app holding a
    key it has no route to use is a credential nobody can explain the presence
    of during an incident.
  EOT
  type        = string
  default     = "snowflake-app-private-key"
}

variable "snowflake_host" {
  description = <<-EOT
    The REST host `ask_account_question` posts Cortex Analyst requests to, e.g.
    llmpcwe-gs74649.snowflakecomputing.com. No scheme.

    Empty is fine and is the shipped default: `chip_chat.api.app._analyst_host`
    derives `<snowflake_account>.snowflakecomputing.com` from the locator above
    and logs that it did. Both forms were checked against the live account on 27
    August 2026 and both answer 200. Set it where the organisation form is
    preferred, or where an account is reached through a URL neither form builds.
  EOT
  type        = string
  default     = ""
}

variable "snowflake_publisher_user" {
  description = <<-EOT
    The Snowflake user the publish authenticates as. `snowflake/sql/04_users.sql`
    creates it with TYPE = SERVICE and grants it CHIP_CHAT_PUBLISH and nothing
    else, so this is a name rather than a choice -- it is here so that a second
    account, or a rebuild after the trial expires, needs no code change.
  EOT
  type        = string
  default     = "CHIP_CHAT_PUBLISHER"
}

variable "databricks_publish_secret_scope" {
  description = <<-EOT
    The Databricks secret scope holding the publisher's private key. Created
    empty by this Terraform and filled by an operator:

      databricks secrets put-secret <scope> publisher-private-key \
        --string-value "$(cat ~/.snowflake/keys/chip_chat_publisher.p8)"

    No private key enters Terraform state, which is the same argument
    `snowflake/sql/04_users.sql` makes about RSA_PUBLIC_KEY never appearing in
    the checked-in SQL. `chip_chat.databricks.publish.SECRET_SCOPE` is the
    matching default on the notebook side.
  EOT
  type        = string
  default     = "chip-chat-snowflake"
}

variable "databricks_publish_schedule_enabled" {
  description = <<-EOT
    Whether the nightly publish's schedule is RUNNING. False leaves the schedule
    declared and PAUSED, which is the shipped default.

    Issue #39 asks for a nightly job, and the rest of this directory says nothing
    in the workspace should be able to start spending on its own. Both hold: the
    cron is declared in Terraform where a person can read and review it, and it
    does not fire until this is set.

    Turn it on once the medallion is loaded and the marts are current. A publish
    against an empty silver layer refuses rather than emptying the serving layer
    -- `snowflake_publish.py` checks each source table for rows before it writes
    anything -- but a job that fails every night at seven is an alert that stops
    meaning anything.
  EOT
  type        = bool
  default     = false
}

variable "databricks_publish_cron" {
  description = <<-EOT
    When the publish runs, as a Quartz expression in UTC. Seconds first, and a
    `?` in whichever of day-of-month and day-of-week is not being used.

    07:00 daily by default: an hour after #38's weekly re-harvest starts and two
    before the recommender's Monday retrain, so a Monday runs harvest, publish,
    retrain in that order rather than publishing a catalogue the marts were not
    computed against.
  EOT
  type        = string
  default     = "0 0 7 * * ?"
}

variable "databricks_publish_alert_email" {
  description = <<-EOT
    Who hears about a failed publish. RFC-001 §10 requires the alert and issue
    #39 requires it in that ticket rather than as a follow-up.

    Defaults to the same address the budget alerts go to, because the failure
    this is about -- personalization quietly going stale -- is one nobody
    watching a dashboard would notice, and a second address to configure is a
    second address to leave empty.
  EOT
  type        = string
  default     = "grahamganssle@gmail.com"
}

variable "databricks_publish_timeout_seconds" {
  description = <<-EOT
    Ceiling on one publish. Eleven tables, the largest of them around fifty
    thousand order lines, each staged and swapped over a JDBC connection.

    Shorter than the recommender's, which fits two model refits, and longer than
    the smoke job: most of this run is a cluster start and the rest is network.
    A run that has not finished by now is not going to, and the useful thing is
    for it to stop billing and send the mail.
  EOT
  type        = number
  default     = 1800
}
