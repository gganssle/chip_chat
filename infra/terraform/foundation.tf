# The Phase 0 foundation: resource group, developer/app identity and Key Vault.
#
# All three already exist. Issue #3 created them imperatively with the az CLI
# because subscription choice and billing are one-time human actions. They are
# adopted into state by the import blocks in imports.tf rather than recreated —
# the Key Vault name in particular is globally unique and, under soft delete,
# not immediately reusable.

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.base}"
  location = var.location
  tags     = local.foundation_rg_tags
}

# The identity everything at runtime hangs off. It is user-assigned rather than
# system-assigned so that the role grants below can exist before the Container
# App that carries it, and survive the app being replaced.
resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.base}-app"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  tags                = local.foundation_tags
}

resource "azurerm_key_vault" "main" {
  name                = local.key_vault_name
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # RBAC rather than the legacy access-policy model, so grants are ordinary role
  # assignments that Terraform manages alongside everything else.
  rbac_authorization_enabled = true

  soft_delete_retention_days = 7

  # Off deliberately. Purge protection is irreversible and would reserve this
  # globally unique name for 90 days after every teardown, which is exactly the
  # property issue #5 is trying to avoid. Turn it on the day this stack holds
  # anything real, and accept the slower teardown.
  purge_protection_enabled = false

  tags = local.foundation_tags
}

# Secret *values* never appear in this configuration. Terraform creates the
# vault and the grants; a human puts secrets in it. See infra/README.md.

resource "azurerm_role_assignment" "developer_key_vault_admin" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "app_key_vault_secrets_user" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}
