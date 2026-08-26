# Databricks: the workspace, and the identity Unity Catalog reaches ADLS with.
#
# Two providers are involved and they are split across two files. This one is
# the Azure side — the workspace resource, the access connector, the role
# assignment that makes the connector useful, and the Key Vault secrets the app
# tier reads. `databricks_compute.tf` is everything *inside* the workspace,
# which the `databricks` provider manages over the workspace API.
#
# PREMIUM IS NOT A PREFERENCE. Two Databricks deadlines land inside this
# project's window and both are answered by the same choice:
#
#   * Standard tier retires 2026-10-01.
#   * From 2026-09-30 every new workspace is provisioned Unity-Catalog-only.
#
# Unity Catalog requires Premium anyway, so creating this workspace Premium with
# an auto-assigned metastore costs nothing extra and sidesteps both dates. The
# way this issue can quietly fail weeks after it looks finished is a Standard
# workspace created now and "upgraded later" — a workspace created this week has
# to survive into October, and a Standard one will not.
# See docs/service-inventory.md#3-databricks-on-azure.

resource "azurerm_databricks_workspace" "main" {
  name                = "dbw-${local.base}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  sku                 = var.databricks_sku

  # The Databricks RP creates this group itself and owns everything in it — the
  # VNet, the NSG, the managed storage account. It is named explicitly so that
  # it is obviously ours and obviously disposable; `terraform destroy` removes
  # the workspace and the RP takes the group with it.
  managed_resource_group_name = "rg-${local.base}-databricks-managed"

  public_network_access_enabled = true

  tags = local.tags

  lifecycle {
    precondition {
      condition     = var.databricks_sku == "premium"
      error_message = "Databricks must be Premium: Standard tier retires 2026-10-01 and Unity Catalog requires Premium. A Standard workspace created now will not survive this project."
    }
  }
}

# --- How Unity Catalog reaches ADLS -----------------------------------------
#
# An access connector is a managed identity that lives outside the workspace and
# that the Databricks control plane is allowed to assume. It is the supported
# way for Unity Catalog to authenticate to ADLS, and it is the reason there is
# no storage key, no SAS token and no service-principal secret anywhere in this
# path — which matters more than usual here, because the data account has
# `shared_access_key_enabled = false` and could not mint a key even if someone
# wanted one.

resource "azurerm_databricks_access_connector" "unity" {
  name                = "dbac-${local.base}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location

  identity {
    type = "SystemAssigned"
  }

  tags = local.tags
}

# Contributor rather than Reader because the lakehouse writes: bronze/silver/gold
# land in ADLS, not in the workspace's own managed storage. Scoped to the data
# account, not the subscription and not the resource group.
resource "azurerm_role_assignment" "databricks_data_blob_contributor" {
  scope                = azurerm_storage_account.data.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_databricks_access_connector.unity.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# --- Credentials, in the vault ----------------------------------------------
#
# Nothing in a notebook. The workspace URL is not a secret but it lives beside
# the token so that a consumer reads one place for both, and so that a teardown
# and re-apply cannot leave a stale host pointing at a workspace that no longer
# exists.

resource "azurerm_key_vault_secret" "databricks_host" {
  name         = "databricks-host"
  value        = "https://${azurerm_databricks_workspace.main.workspace_url}"
  key_vault_id = azurerm_key_vault.main.id

  depends_on = [azurerm_role_assignment.developer_key_vault_admin]
}

# Off unless var.databricks_service_principal_token_enabled is set. The app tier
# reads `databricks-host` and authenticates with its managed identity; there is
# deliberately no secret in the normal path. See databricks_compute.tf.
resource "azurerm_key_vault_secret" "databricks_token" {
  count = var.databricks_service_principal_token_enabled ? 1 : 0

  name         = "databricks-token"
  value        = databricks_obo_token.jobs[0].token_value
  key_vault_id = azurerm_key_vault.main.id

  # The token expires. Recording when, in a place a human will actually look,
  # is the difference between a scheduled rotation and a job that stops working
  # on a Tuesday for no visible reason.
  content_type    = "Databricks PAT for the jobs service principal"
  expiration_date = timeadd(timestamp(), "${var.databricks_token_lifetime_days * 24}h")

  depends_on = [azurerm_role_assignment.developer_key_vault_admin]

  lifecycle {
    # `timestamp()` moves every plan. Without this the secret would show a diff
    # on every single run and the expiry would creep forward without the token
    # behind it ever being reissued — which is worse than a stale date, because
    # it is a stale date that looks fresh. Rotation is deliberate:
    #   terraform apply -replace=databricks_obo_token.jobs
    ignore_changes = [expiration_date]
  }
}

# --- Unity Catalog's route to ADLS ------------------------------------------
#
# A storage credential wraps the access connector above; an external location
# binds that credential to one container. Both are metastore-level objects, not
# workspace-level ones, which is why they are gated: they need the workspace to
# have a metastore attached. Workspaces created after 2023-11-09 get one
# assigned automatically, and from 2026-09-30 every new workspace is UC-only —
# so the default here is on, and the flag exists for the case where the account
# has no auto-assign metastore in this region yet and someone has to go and set
# one before the rest of the stack can proceed.
#
# The catalogs and the bronze/silver/gold schemas are issue #32's job. This
# issue stops at "Unity Catalog can reach the data", which is exactly the
# credential and the locations below.

resource "databricks_storage_credential" "adls" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name    = "${local.base}-adls"
  comment = "Access connector identity for the chip_chat ADLS Gen2 account. Managed by infra/terraform (gh-31)."

  azure_managed_identity {
    access_connector_id = azurerm_databricks_access_connector.unity.id
  }

  # Unity Catalog validates the credential by reaching the storage account at
  # create time. The role assignment is eventually consistent, so without this
  # the first apply after a teardown fails on a permission that is on its way.
  depends_on = [azurerm_role_assignment.databricks_data_blob_contributor]
}

# The raw landing zone, as Unity Catalog sees it. `abfss://` and the /dfs
# endpoint rather than `wasbs://` and /blob: the hierarchical namespace is what
# makes directory semantics and atomic renames work, and it is why storage.tf
# sets `is_hns_enabled`.
resource "databricks_external_location" "raw" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name            = "${local.base}-raw"
  url             = "abfss://${azurerm_storage_container.raw.name}@${azurerm_storage_account.data.name}.dfs.core.windows.net/"
  credential_name = databricks_storage_credential.adls[0].name
  comment         = "Harvested menu, nutrition and policy source documents, before anything has been done to them."

  # Teardown is one command, and an external location that refuses to delete
  # because a table still references it is exactly the straggler that breaks
  # that promise. The data itself is in ADLS and is removed with the storage
  # account, not with this object.
  force_destroy = true
}

# Managed storage for the lakehouse catalogs #32 will create. It is a separate
# container rather than a prefix inside `raw` because Unity Catalog will not
# accept a managed location that overlaps an external one, and because "data
# someone harvested" and "data Unity Catalog owns the lifecycle of" should not
# be able to delete each other.
resource "databricks_external_location" "lakehouse" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  name            = "${local.base}-lakehouse"
  url             = "abfss://${azurerm_storage_container.lakehouse.name}@${azurerm_storage_account.data.name}.dfs.core.windows.net/"
  credential_name = databricks_storage_credential.adls[0].name
  comment         = "Managed storage for the bronze/silver/gold catalogs (gh-32)."

  force_destroy = true
}

# The service principal can read and write the landing zone, and nothing else.
# Ownership stays with the metastore admin.
resource "databricks_grants" "raw_external_location" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  external_location = databricks_external_location.raw[0].id

  grant {
    principal  = databricks_service_principal.jobs.application_id
    privileges = ["READ_FILES", "WRITE_FILES", "CREATE_EXTERNAL_TABLE"]
  }

  # READ_FILES and nothing else for the reader (gh-32). The gold-to-Snowflake
  # publish reads files as well as tables, and a principal that is read-only in
  # the catalog but could still write to the landing zone would not be read-only
  # in any sense worth verifying.
  grant {
    principal  = databricks_service_principal.readonly.application_id
    privileges = ["READ_FILES"]
  }
}

resource "databricks_grants" "lakehouse_external_location" {
  count = var.databricks_unity_catalog_enabled ? 1 : 0

  external_location = databricks_external_location.lakehouse[0].id

  grant {
    principal  = databricks_service_principal.jobs.application_id
    privileges = ["READ_FILES", "WRITE_FILES", "CREATE_EXTERNAL_TABLE"]
  }
}
