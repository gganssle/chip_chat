# Storage.
#
# Two accounts, because they want opposite things.
#
# `data` is ADLS Gen2 with a hierarchical namespace: the raw landing zone for the
# harvest and the photo-upload container. `functions` is a plain account holding
# the ops API's deployment package and its runtime state — Azure Functions does
# not support a hierarchical namespace for its own storage, so mixing the two
# would break the Function App rather than save a resource.

resource "azurerm_storage_account" "data" {
  name                = substr("st${local.compact}${random_string.unique.result}", 0, 24)
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location

  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"

  # The hierarchical namespace is what makes this ADLS Gen2 rather than plain
  # blob: directory semantics, atomic renames, and the /dfs endpoint Databricks
  # and Lakeflow expect.
  is_hns_enabled = true

  # Identity, not keys. Nothing in this stack runs on a connection string that
  # could have been a managed identity, so shared keys are off outright — which
  # also means an accidentally leaked key is not a credential.
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true

  https_traffic_only_enabled      = true
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  public_network_access_enabled   = true

  blob_properties {
    # BLOB SOFT DELETE MUST STAY OFF ON THIS ACCOUNT.
    #
    # This is not a cost setting, it is the data-hygiene property. With soft
    # delete on, the 24-hour lifecycle rule below only *soft*-deletes an uploaded
    # photo and the image is then retained for the full soft-delete window —
    # precisely the thing the design says it does not do.
    #
    # Soft delete is disabled by *omitting* the retention blocks: the provider's
    # minimum for `days` is 1, so there is no way to write "zero days" and mean
    # it. Omission is therefore load-bearing rather than an oversight, which is
    # why the postcondition below asserts the result against what Azure actually
    # reports instead of trusting the absence of two blocks.
    versioning_enabled  = false
    change_feed_enabled = false
  }

  tags = local.tags

  lifecycle {
    postcondition {
      condition     = length(self.blob_properties[0].delete_retention_policy) == 0
      error_message = "Blob soft delete must be OFF on the uploads account: with it on, the 24-hour lifecycle rule only soft-deletes, and strangers' photographs are retained for the full soft-delete window."
    }
    postcondition {
      condition     = length(self.blob_properties[0].container_delete_retention_policy) == 0
      error_message = "Container soft delete must be OFF on the uploads account, for the same reason as blob soft delete."
    }
  }
}

# The raw landing zone: harvested menu and nutrition source documents, before
# anything has been done to them.
resource "azurerm_storage_container" "raw" {
  name               = "raw"
  storage_account_id = azurerm_storage_account.data.id
}

# Visitor photo uploads. Everything written here is deleted by the lifecycle
# rule below.
resource "azurerm_storage_container" "uploads" {
  name               = "uploads"
  storage_account_id = azurerm_storage_account.data.id
}

resource "azurerm_storage_management_policy" "data" {
  storage_account_id = azurerm_storage_account.data.id

  # Delete uploaded photos after a day.
  #
  # An honest reading of what this does: lifecycle rules have *day* granularity,
  # and the engine takes up to 24 hours to begin executing after a policy change
  # and then runs periodically. So the real behaviour is deletion 24-48 hours
  # after upload, not exactly 24. User-facing copy should say "within 48 hours".
  rule {
    name    = "expire-uploads"
    enabled = true

    filters {
      prefix_match = ["${azurerm_storage_container.uploads.name}/"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = var.uploads_retention_days
      }
    }
  }
}

# --- Function app storage ---------------------------------------------------

resource "azurerm_storage_account" "functions" {
  name                = substr("stfn${local.compact}${random_string.unique.result}", 0, 24)
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location

  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"

  # Same rule as above: the Function App authenticates to this account with the
  # app's managed identity, so there is no key for anything to leak.
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true

  https_traffic_only_enabled      = true
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false

  tags = local.tags
}

# Flex Consumption deploys from a blob container rather than from the file
# share the older plans used.
resource "azurerm_storage_container" "function_deployments" {
  name               = "deployments"
  storage_account_id = azurerm_storage_account.functions.id
}

# --- Data-plane grants ------------------------------------------------------

resource "azurerm_role_assignment" "app_data_blob_contributor" {
  scope                = azurerm_storage_account.data.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# The developer's own grant on the same account (issue #8). Shared keys are off,
# so there is no connection string to fall back on: without this, `az login`
# credentials cannot read the photo the vision verification uploads, and the
# harvest cannot write to the raw landing zone from a laptop either.
resource "azurerm_role_assignment" "developer_data_blob_contributor" {
  scope                = azurerm_storage_account.data.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

# The Functions runtime needs blob, queue and table on its own storage account
# when it is running without a connection string. Owner rather than Contributor
# on blob because the host manages leases and container ACLs for itself.
resource "azurerm_role_assignment" "app_functions_blob_owner" {
  scope                = azurerm_storage_account.functions.id
  role_definition_name = "Storage Blob Data Owner"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_functions_queue_contributor" {
  scope                = azurerm_storage_account.functions.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_functions_table_contributor" {
  scope                = azurerm_storage_account.functions.id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}
