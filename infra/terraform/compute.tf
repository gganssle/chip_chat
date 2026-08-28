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
    # The agent-observability backend. `local.otlp_endpoint` is defined in
    # observability.tf and resolves to the Phoenix container app in this
    # environment unless `var.otlp_endpoint` overrides it. This is the entire
    # app-tier diff of the backend switch — one env entry, no instrumentation.
    #
    # OTEL_EXPORTER_OTLP_HEADERS is deliberately absent: a self-hosted backend
    # on an internal address needs no API key, and this map still carries no
    # secrets. OTEL_EXPORTER_OTLP_PROTOCOL is absent for a different reason —
    # chip_chat.otel.config does not read it (only the agent tier's runtime
    # does), and a setting the application does not read is worse than an
    # absent one, because it looks like configuration. Both are in
    # docs/decisions/hosted-phoenix.md.
    local.otlp_endpoint == "" ? {} : { OTEL_EXPORTER_OTLP_ENDPOINT = local.otlp_endpoint },
    local.snowflake_env,
  )

  # The read connection (cc-lpy4). Names only -- the private key is a Container
  # Apps secret below, not an entry here, which is why this merge is a separate
  # block rather than five more lines in the map above.
  #
  # All of it is conditional on var.snowflake_account, and that is the switch
  # rather than a feature flag in code: an app told the name of an account it has
  # no key for would open a pool that fails every checkout, which reads as an
  # outage instead of as a deployment nobody finished. Empty means the app takes
  # `connect is None` and runs on the roster shipped in its image.
  snowflake_env = var.snowflake_account == "" ? {} : {
    SNOWFLAKE_ACCOUNT = var.snowflake_account

    # CHIP_CHAT_APP on CHIP_CHAT_READ, which is refused an INSERT by the account
    # itself. Both are defaulted in `chip_chat.api.connect` off
    # `chip_chat.snowflake.account.USERS`, and both are spelled here anyway: what
    # a deployed tier runs as should be readable in the deployment and not only
    # in the code it deploys.
    SNOWFLAKE_APP_USER  = "CHIP_CHAT_APP"
    SNOWFLAKE_READ_ROLE = "CHIP_CHAT_READ"

    # X-Small, suspends after sixty seconds. CHIP_CHAT_PUBLISH_WH is the other
    # one and only the nightly publish may name it.
    SNOWFLAKE_WAREHOUSE = "CHIP_CHAT_SERVING_WH"
    SNOWFLAKE_DATABASE  = "CHIP_CHAT"

    # ACCOUNTS, because the entry roster reads `FROM persona_fixtures`
    # unqualified -- the one read in the system that happens before there is a
    # visitor, and the one #43's `entry_roster` policy was written for.
    SNOWFLAKE_SCHEMA = "ACCOUNTS"

    # Where the account lane posts. Derived from the locator when unset; see
    # var.snowflake_host.
    SNOWFLAKE_HOST = (
      var.snowflake_host != ""
      ? var.snowflake_host
      : "${var.snowflake_account}.snowflakecomputing.com"
    )
  }
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

  # The one secret this app holds: CHIP_CHAT_APP's private key.
  #
  # A Key Vault *reference* and not a value. The id is deliberately versionless,
  # so rotating the secret in the vault is one `az keyvault secret set` and a
  # revision restart rather than a Terraform change; the platform resolves it
  # with the user-assigned identity below, which already holds Key Vault Secrets
  # User on the vault (foundation.tf). Nothing about the key reaches a plan, the
  # state file or the image.
  #
  # This is also why `chip_chat.api.connect` prefers the environment over its own
  # Key Vault client. The read happens before the process exists, so the app pays
  # nothing for it on a start-up path that a liveness probe is waiting behind —
  # which is the trap docs/deployment.md §3.11 is a write-up of.
  dynamic "secret" {
    for_each = var.snowflake_account == "" ? [] : ["snowflake"]
    content {
      name                = "snowflake-private-key"
      key_vault_secret_id = "${azurerm_key_vault.main.vault_uri}secrets/${var.snowflake_app_key_secret}"
      identity            = azurerm_user_assigned_identity.app.id
    }
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
      name  = "web"
      image = var.web_image

      # Half a vCPU rather than a quarter, and the memory that has to accompany
      # it — Container Apps only allows fixed CPU/memory pairs, so 0.5 vCPU
      # means 1 GiB whether or not the app wants it.
      #
      # This is bought for exactly one number: the cold start a visitor sees.
      # `min_replicas = 0` means every link handed to somebody who has not
      # clicked one recently pays for a Python process starting from nothing,
      # and that start is CPU-bound. Idle still costs nothing, because idle is
      # still zero replicas; what doubled is the rate while a replica is
      # actually serving, which is a few minutes a day. docs/deployment.md §6
      # has the measurement this is justified by.
      cpu    = 0.5
      memory = "1.0Gi"

      dynamic "env" {
        for_each = local.web_env
        content {
          name  = env.key
          value = env.value
        }
      }

      # The private key, by reference to the secret above rather than by value.
      # `secret_name` and `value` are mutually exclusive, which is why this is a
      # second block and not another entry in local.web_env — a map cannot carry
      # the distinction and a key that ended up in the value column would be a
      # key in `az containerapp show`.
      dynamic "env" {
        for_each = var.snowflake_account == "" ? [] : ["snowflake"]
        content {
          name        = "SNOWFLAKE_PRIVATE_KEY"
          secret_name = "snowflake-private-key"
        }
      }

      # /healthz is outside the spend cap and outside the rate limit on purpose
      # (chip_chat.api.app): a probe that could be refused for spending money it
      # never spends would take the app down every time the ceiling was reached.
      #
      # Every timing below was a default until a deploy showed what the defaults
      # cost. Container Apps' own are a one-second timeout with no initial
      # delay, and a cold Python process on a fraction of a vCPU cannot answer
      # anything in the first second of its life — so the platform opened a
      # restart loop against an application that was merely starting, and the
      # revision never reached "ready". docs/deployment.md §3.11.
      #
      # The numbers are chosen so that the probe distinguishes the two things it
      # is for: a container that is slow to come up (wait) and a container that
      # has stopped answering (restart). Ten seconds of grace, then a five
      # second timeout — five times the default, because a GIL held by a batch
      # export is a real thing on one worker — and three consecutive failures
      # before the platform concludes the process is gone. Forty-five seconds of
      # evidence before a restart, rather than three.
      liveness_probe {
        transport               = "HTTP"
        port                    = var.web_target_port
        path                    = "/healthz"
        initial_delay           = 10
        interval_seconds        = 15
        timeout                 = 5
        failure_count_threshold = 3
      }

      # Readiness is the one that decides whether a new revision takes traffic,
      # so it is deliberately more patient than liveness: a revision that is
      # still importing should be *not ready*, which is the correct answer, and
      # should not also be *restarted*, which is not.
      readiness_probe {
        transport               = "HTTP"
        port                    = var.web_target_port
        path                    = "/healthz"
        initial_delay           = 5
        interval_seconds        = 10
        timeout                 = 5
        failure_count_threshold = 6
        success_count_threshold = 1
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
#
# This app is RFC-001 §03's *only path that writes*, and the settings below are
# where that sentence is made true of a running system rather than of a design.
# Nothing else in the estate is given the Snowflake write role: the container app
# gets `snowflake-app-private-key` and this one gets `snowflake-ops-private-key`,
# and the two users those keys belong to are granted different roles by
# `snowflake/sql/00_roles.sql`. Terraform stands the app up; `make ops-deploy`
# puts code on it, for the same reason `make deploy` and not `terraform apply`
# owns the container image.

locals {
  # The account identifier the ops API authenticates against — `hq72718.us-east-2.aws`,
  # not a URL and not a secret. Derived from the publish job's account URL when
  # that is configured, because it is the same Snowflake account: two settings
  # that must agree are one setting that can disagree, and the failure mode is a
  # write path pointed at nothing that answers 503 while looking configured.
  snowflake_ops_account = (
    var.snowflake_ops_account != ""
    ? var.snowflake_ops_account
    : trimsuffix(var.snowflake_account_url, ".snowflakecomputing.com")
  )

  # Two Key Vault secrets this app reads, neither of them created here.
  #
  # The same argument `databricks_publish_secret_scope` makes: no key material
  # enters Terraform state. `snowflake-ops-private-key` is the PKCS#8 private
  # key of the `CHIP_CHAT_OPS` service user, put there by whoever ran
  # `ALTER USER ... SET RSA_PUBLIC_KEY`, and `ops-api-key` is the shared secret
  # the chat app presents on `x-cilantro-ops-key`. `make ops-key` mints the
  # second if it is absent.
  #
  # Both are referenced rather than read, so what lands in the app's environment
  # is resolved by the platform at start-up and neither value is ever in a plan,
  # a state file or a `terraform output`. An absent secret leaves the setting
  # unresolved, which the host reads as no credential — and
  # `function_app.py::_authentic` refuses every request rather than allowing
  # them all, which is the direction this has to fail in.
  ops_private_key_secret = "snowflake-ops-private-key"
  ops_api_key_secret     = "ops-api-key"

  key_vault_reference = {
    for name in [local.ops_private_key_secret, local.ops_api_key_secret] :
    name => "@Microsoft.KeyVault(VaultName=${azurerm_key_vault.main.name};SecretName=${name})"
  }
}

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

  runtime_name = "python"
  # 3.13, matching `requires-python = ">=3.13"` in every workspace package. The
  # host installs those packages as wheels built out of this repository
  # (`api/functions/requirements.txt`), and pip refuses a wheel whose
  # `Requires-Python` the interpreter does not satisfy — so a 3.12 worker does
  # not run the ops API slightly differently, it fails to install it at all.
  runtime_version = "3.13"

  instance_memory_in_mb  = 2048
  maximum_instance_count = 40

  https_only = true

  # Two identities, and they do different jobs.
  #
  # The user-assigned one is the app tier's: it reads the published catalogue out
  # of the `raw` container and it authenticates this app's deployment storage.
  # The system-assigned one exists for exactly one reason — App Service resolves
  # `@Microsoft.KeyVault(...)` references with the *system-assigned* identity
  # unless `keyVaultReferenceIdentity` names another, and that property is not on
  # this resource in the AzureRM provider. Adding the system identity is
  # therefore how the two references above resolve at all; it is granted Key
  # Vault Secrets User in foundation.tf and nothing else anywhere.
  identity {
    type         = "SystemAssigned, UserAssigned"
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

    # The catalogue the draft store prices against. `chip_chat.api.menu` reads
    # it with the user-assigned identity above, from the container #24's build
    # publishes to — the same two settings the container app is given, because
    # the ops API and the app must price a draft identically or the card the
    # visitor read and the row that gets written are two different orders.
    "AZURE_STORAGE_ACCOUNT"   = azurerm_storage_account.data.name
    "AZURE_CATALOG_CONTAINER" = azurerm_storage_container.raw.name

    # The shared secret the chat app presents. Its absence refuses every write,
    # which is the whole reason it is checked before anything else in the
    # request — see `function_app.py::_authentic`.
    "CHIP_CHAT_OPS_KEY" = local.key_vault_reference[local.ops_api_key_secret]

    # The only credentials in the system with the Snowflake write role. The user
    # and the role are named rather than defaulted so that what this app runs as
    # is a fact in a file somebody reads, and not a property of an account
    # somebody may edit; `snowflake/sql/04_users.sql` and `00_roles.sql` create
    # both. The warehouse is the X-Small serving one — only the nightly publish
    # may name `CHIP_CHAT_PUBLISH_WH`.
    "SNOWFLAKE_ACCOUNT"     = local.snowflake_ops_account
    "SNOWFLAKE_OPS_USER"    = var.snowflake_ops_user
    "SNOWFLAKE_PRIVATE_KEY" = local.key_vault_reference[local.ops_private_key_secret]
    "SNOWFLAKE_WRITE_ROLE"  = "CHIP_CHAT_WRITE"
    "SNOWFLAKE_WAREHOUSE"   = "CHIP_CHAT_SERVING_WH"
    "SNOWFLAKE_DATABASE"    = "CHIP_CHAT"
    "SNOWFLAKE_SCHEMA"      = "ACCOUNTS"

    # deployment.environment on every `ops.<action>` span, so a write made from
    # the deployed app is distinguishable in the backend from one made by a
    # laptop pointed at the same account. Issue #63's last acceptance criterion
    # is read off these spans, so where they land is part of the criterion.
    "CHIP_CHAT_ENVIRONMENT" = var.environment
  }

  tags = local.tags

  depends_on = [
    azurerm_role_assignment.app_functions_blob_owner,
    azurerm_role_assignment.app_functions_queue_contributor,
    azurerm_role_assignment.app_functions_table_contributor,
  ]
}
