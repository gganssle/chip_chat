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
  description = "Ceiling on replicas. Low on purpose — this is a demo, and scale-out is spend."
  type        = number
  default     = 2
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
  description = "Port the chat app container listens on."
  type        = number
  default     = 80
}

# --- Model deployments ------------------------------------------------------

variable "model_deployments" {
  description = <<-EOT
    Model deployments on the Foundry account, keyed by deployment name. Empty by
    default: issue #5 builds the environment, issue #8 chooses the models and
    confirms quota. Deployment names are configuration precisely so they can be
    swapped for eval experiments later — see #8's acceptance criteria.

    Capacity is thousands of tokens per minute and is a spend control as much as
    a performance setting. See terraform.tfvars.example for the intended shape.
  EOT
  type = map(object({
    model_name    = string
    model_version = optional(string)
    model_format  = optional(string, "OpenAI")
    sku_name      = optional(string, "GlobalStandard")
    capacity      = optional(number, 10)
  }))
  default = {}
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
