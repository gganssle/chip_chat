# Compute: the chat app on Container Apps, the ops API on Functions.

resource "azurerm_container_app_environment" "main" {
  name                = "cae-${local.base}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location

  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  logs_destination           = "log-analytics"

  tags = local.tags
}

resource "azurerm_container_app" "web" {
  name                         = "ca-${local.base}-web"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  ingress {
    external_enabled = true
    target_port      = var.web_target_port
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    # Zero. An idle replica still bills — roughly an eighth of the active vCPU
    # rate — and this app is idle almost all of the time. Scale to one only
    # while actively sharing the link.
    min_replicas = var.web_min_replicas
    max_replicas = var.web_max_replicas

    # Scale-to-zero has two documented ways to strand an app, and this is the
    # guard against both. An app with no scale rule and no minimum replica can
    # scale to zero and have no way to wake up; and the KEDA CPU and memory
    # scalers cannot scale to zero by design. An HTTP rule is the one that
    # works, so it is not optional here even though it looks like tuning.
    http_scale_rule {
      name                = "http"
      concurrent_requests = "20"
    }

    container {
      name   = "web"
      image  = var.web_image
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }

      env {
        name  = "AZURE_KEY_VAULT_URI"
        value = azurerm_key_vault.main.vault_uri
      }

      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }

      env {
        name  = "AZURE_STORAGE_ACCOUNT"
        value = azurerm_storage_account.data.name
      }

      env {
        name  = "AZURE_UPLOADS_CONTAINER"
        value = azurerm_storage_container.uploads.name
      }

      env {
        name  = "AZURE_SEARCH_ENDPOINT"
        value = one(azurerm_search_service.main[*].name) == null ? "" : "https://${one(azurerm_search_service.main[*].name)}.search.windows.net"
      }

      env {
        name  = "AZURE_FOUNDRY_ENDPOINT"
        value = azurerm_cognitive_account.foundry.endpoint
      }

      env {
        name  = "AZURE_CONTENT_SAFETY_ENDPOINT"
        value = azurerm_cognitive_account.content_safety.endpoint
      }

      env {
        name  = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
        value = azurerm_cognitive_account.document_intelligence.endpoint
      }
    }
  }

  tags = local.tags

  lifecycle {
    # Phase 0 stands up the environment; it does not ship the app. Once a real
    # image is deployed, Terraform must not drag it back to the quickstart
    # placeholder on the next apply.
    ignore_changes = [
      template[0].container[0].image,
      template[0].revision_suffix,
    ]
  }
}

# --- Ops API ----------------------------------------------------------------
#
# Flex Consumption rather than the older Y1 Consumption plan: it scales to zero
# the same way, and Y1 is on its way out.

resource "azurerm_service_plan" "ops" {
  name                = "plan-${local.base}-ops"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  os_type             = "Linux"
  sku_name            = "FC1"

  tags = local.tags
}

resource "azurerm_function_app_flex_consumption" "ops" {
  name                = "func-${local.base}-ops-${random_string.unique.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  service_plan_id     = azurerm_service_plan.ops.id

  # Identity, not a connection string — which is why the deployment storage
  # account above has shared keys disabled entirely. The grants that make this
  # work are in storage.tf.
  storage_container_type            = "blobContainer"
  storage_container_endpoint        = "${azurerm_storage_account.functions.primary_blob_endpoint}${azurerm_storage_container.function_deployments.name}"
  storage_authentication_type       = "UserAssignedIdentity"
  storage_user_assigned_identity_id = azurerm_user_assigned_identity.app.id

  runtime_name    = "python"
  runtime_version = "3.12"

  instance_memory_in_mb  = 2048
  maximum_instance_count = 40

  https_only = true

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  site_config {
    application_insights_connection_string = azurerm_application_insights.main.connection_string
  }

  app_settings = {
    # The Functions host's own state, also over identity rather than a
    # connection string.
    "AzureWebJobsStorage__accountName" = azurerm_storage_account.functions.name
    "AzureWebJobsStorage__credential"  = "managedidentity"
    "AzureWebJobsStorage__clientId"    = azurerm_user_assigned_identity.app.client_id

    "AZURE_CLIENT_ID"     = azurerm_user_assigned_identity.app.client_id
    "AZURE_KEY_VAULT_URI" = azurerm_key_vault.main.vault_uri
  }

  tags = local.tags

  depends_on = [
    azurerm_role_assignment.app_functions_blob_owner,
    azurerm_role_assignment.app_functions_queue_contributor,
    azurerm_role_assignment.app_functions_table_contributor,
  ]
}
