# Compute: the chat app on Container Apps, the ops API on Functions.

resource "azurerm_container_app_environment" "main" {
  name                = "cae-${local.base}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location

  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  logs_destination           = "log-analytics"

  tags = local.tags
}

# What the chat app is handed. Everything here is a name, an endpoint or a
# ceiling; there is not a secret among them, which is why they are plain `env`
# entries rather than Container Apps secrets.
#
# The CHIP_CHAT_* names are the ones the code actually reads -- see
# chip_chat.agent.foundry, chip_chat.api.limits and chip_chat.otel.config. A
# setting the application does not read is worse than an absent one, because it
# looks like configuration.
locals {
  web_env = merge(
    {
      AZURE_CLIENT_ID                       = azurerm_user_assigned_identity.app.client_id
      AZURE_KEY_VAULT_URI                   = azurerm_key_vault.main.vault_uri
      APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.main.connection_string
      AZURE_STORAGE_ACCOUNT                 = azurerm_storage_account.data.name
      AZURE_UPLOADS_CONTAINER               = azurerm_storage_container.uploads.name

      # Photo lane and retrieval. The retrieval lane reads the ALIAS and never
      # an index name: the index is rebuilt weekly under a new name and alias-
      # swapped into place (RFC-001 section 08), so an index name in this map
      # would be stale the first time the corpus was re-harvested.
      AZURE_SEARCH_ENDPOINT                = one(azurerm_search_service.main[*].name) == null ? "" : "https://${one(azurerm_search_service.main[*].name)}.search.windows.net"
      AZURE_SEARCH_INDEX_ALIAS             = var.search_alias
      AZURE_CONTENT_SAFETY_ENDPOINT        = azurerm_cognitive_account.content_safety.endpoint
      AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = azurerm_cognitive_account.document_intelligence.endpoint

      # Which deployment answers for which lane. The eval swap point.
      CHIP_CHAT_FOUNDRY_ENDPOINT          = azurerm_cognitive_account.foundry.endpoint
      CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT   = var.chat_deployment
      CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT = var.vision_deployment

      # The knowledge lane's. Read by chip_chat.search at index-build time; the
      # index's own vectorizer carries the same name, put there by the build.
      CHIP_CHAT_FOUNDRY_EMBEDDING_DEPLOYMENT = var.embedding_deployment

      # deployment.environment on every span, so the deployed app's traces are
      # distinguishable from a laptop's in the same backend.
      CHIP_CHAT_ENVIRONMENT = var.environment

      # The inline spend cap. These are the numbers standing between a URL with
      # no authentication and an invoice.
      CHIP_CHAT_DAILY_TOKEN_CEILING        = tostring(var.spend_caps.daily_token_ceiling)
      CHIP_CHAT_SESSION_TURN_CAP           = tostring(var.spend_caps.session_turn_cap)
      CHIP_CHAT_SESSION_TOKEN_CAP          = tostring(var.spend_caps.session_token_cap)
      CHIP_CHAT_SOURCE_REQUESTS_PER_WINDOW = tostring(var.spend_caps.source_requests_per_window)
      CHIP_CHAT_SOURCE_WINDOW_SECONDS      = tostring(var.spend_caps.source_window_seconds)
      CHIP_CHAT_TURN_TOKEN_RESERVATION     = tostring(var.spend_caps.turn_token_reservation)
      CHIP_CHAT_BUDGET_RESET_TIMEZONE      = var.spend_caps.budget_reset_timezone

      # The circuit breaker, in the run position. One portal edit throws it.
      CHIP_CHAT_KILL_SWITCH = var.kill_switch
    },
    var.otlp_endpoint == "" ? {} : { OTEL_EXPORTER_OTLP_ENDPOINT = var.otlp_endpoint },
  )
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

  # Pull by identity. No admin user, no password, nothing to rotate — see
  # registry.tf.
  registry {
    server   = one(azurerm_container_registry.main[*].login_server)
    identity = azurerm_user_assigned_identity.app.id
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

      dynamic "env" {
        for_each = local.web_env
        content {
          name  = env.key
          value = env.value
        }
      }

      # /healthz is outside the spend cap and outside the rate limit on purpose
      # (chip_chat.api.app): a probe that could be refused for spending money it
      # never spends would take the app down every time the ceiling was reached.
      liveness_probe {
        transport = "HTTP"
        port      = var.web_target_port
        path      = "/healthz"
      }

      readiness_probe {
        transport = "HTTP"
        port      = var.web_target_port
        path      = "/healthz"
      }
    }
  }

  tags = local.tags

  depends_on = [azurerm_role_assignment.app_registry_pull]

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
