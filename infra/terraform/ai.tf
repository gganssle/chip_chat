# Microsoft Foundry, Content Safety and Document Intelligence.
#
# On naming: what the planning documents call "Azure AI Foundry" is now
# "Microsoft Foundry", and the umbrella that Content Safety and Document
# Intelligence bill under is now "Foundry Tools" on the invoice — worth knowing
# when the Phase 9 cost dashboard greps the cost export for a service name. The
# individual services kept their own names. See
# docs/service-inventory.md#1-the-headline-azure-ai-foundry-is-now-microsoft-foundry.
#
# On shape: this uses the current Foundry account-and-project model — a
# Cognitive Services account of kind AIServices with project management enabled,
# plus a project on it — rather than the older hub-and-workspace form built on
# Azure Machine Learning (`azurerm_ai_foundry`). The hub form drags in a
# workspace, its own storage account and an Application Insights component, none
# of which this design uses.

resource "azurerm_cognitive_account" "foundry" {
  name                = "aif-${local.base}-${random_string.unique.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  kind                = "AIServices"
  sku_name            = var.foundry_sku

  # Required for Entra token auth against the data plane, which is what the app
  # identity uses.
  custom_subdomain_name = "aif-${local.base}-${random_string.unique.result}"

  project_management_enabled = true

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  # Keys stay available for local development against the deployed models; the
  # app itself authenticates with the identity above.
  local_auth_enabled = true

  tags = local.tags
}

resource "azurerm_cognitive_account_project" "main" {
  name                 = "proj-${local.base}"
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  location             = var.location
  display_name         = "Cilantro (${var.environment})"
  description          = "Agent project for the Cilantro demo. Managed by infra/terraform (gh-5)."

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  tags = local.tags
}

# Model deployments are driven by var.model_deployments and default to none.
# Issue #5 stands up the environment; issue #8 picks the chat and vision models
# and confirms in-region quota before turning them on. Deployment names are
# configuration rather than literals precisely so they can be swapped for eval
# experiments later.
resource "azurerm_cognitive_deployment" "models" {
  for_each = var.model_deployments

  name                 = each.key
  cognitive_account_id = azurerm_cognitive_account.foundry.id

  model {
    format  = each.value.model_format
    name    = each.value.model_name
    version = each.value.model_version
  }

  sku {
    name = each.value.sku_name
    # Thousands of tokens per minute. Low is deliberate: TPM quota is a spend
    # control as much as a performance setting.
    capacity = each.value.capacity
  }
}

# --- Content Safety ---------------------------------------------------------
#
# F0 gives 5,000 text records and 5,000 images a month at 5 RPS, which is more
# than a demo will use. One free account of this kind per subscription.
#
# East US 2 supports image analysis, Prompt Shields, groundedness and protected
# material. It does NOT support multimodal (text+image in one call) or custom
# categories (standard) — those need East US or West Europe. The design calls
# image moderation on the upload and text moderation on the prompt as separate
# calls, which East US 2 does support. If multimodal is ever wanted it can be a
# second resource in East US without moving anything else.

resource "azurerm_cognitive_account" "content_safety" {
  name                  = "cs-${local.base}-${random_string.unique.result}"
  resource_group_name   = azurerm_resource_group.main.name
  location              = var.location
  kind                  = "ContentSafety"
  sku_name              = var.content_safety_sku
  custom_subdomain_name = "cs-${local.base}-${random_string.unique.result}"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  tags = local.tags
}

# --- Document Intelligence --------------------------------------------------
#
# Reads the nutrition PDFs in Phase 1. F0 is free and one per subscription; the
# rate limit is the only thing to watch. `FormRecognizer` is the API kind that
# backs what the portal now calls Document Intelligence.

resource "azurerm_cognitive_account" "document_intelligence" {
  name                  = "di-${local.base}-${random_string.unique.result}"
  resource_group_name   = azurerm_resource_group.main.name
  location              = var.location
  kind                  = "FormRecognizer"
  sku_name              = var.document_intelligence_sku
  custom_subdomain_name = "di-${local.base}-${random_string.unique.result}"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  tags = local.tags
}

# --- Grants -----------------------------------------------------------------

resource "azurerm_role_assignment" "app_foundry_user" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# OpenAI-shaped inference on the Foundry account is a separate role from the
# generic data-plane read.
resource "azurerm_role_assignment" "app_foundry_openai_user" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_content_safety_user" {
  scope                = azurerm_cognitive_account.content_safety.id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_document_intelligence_user" {
  scope                = azurerm_cognitive_account.document_intelligence.id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# The developer's own data-plane grant on the Foundry account (issue #8).
#
# Subscription Owner does not imply this. Owner carries `*` in `actions` and
# nothing in `dataActions`, and inference is a data action — so without the two
# grants below, `az login` credentials get a 401 from the model endpoint while
# every management call keeps working, which is a confusing way to spend an
# afternoon. This is the "configure managed identity access" half of #8's
# acceptance criteria for a human rather than for the app.
resource "azurerm_role_assignment" "developer_foundry_user" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services User"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "developer_foundry_openai_user" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = data.azurerm_client_config.current.object_id
}
