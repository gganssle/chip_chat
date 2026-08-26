# Adopting the Phase 0 foundation.
#
# Issue #3 created the resource group, Key Vault, app identity, cost action
# group and subscription budget imperatively with the az CLI. They are real, the
# budget is the project's live cost guardrail, and the Key Vault name is globally
# unique and not immediately reusable under soft delete — so they are imported
# into state rather than destroyed and recreated.
#
# The test that the import is right is that `terraform plan` immediately after
# it is a no-op against these five. If plan wants to change or recreate any of
# them, the import is wrong, not the estate.
#
# These are declarative import blocks rather than `terraform import` calls so
# that adoption shows up in a plan before it happens and so that re-running init
# on a fresh checkout of the same state is a no-op. They are gated on
# `adopt_existing_foundation`: a disposable second stack has no Phase 0 estate to
# adopt and must create its own foundation from nothing.

locals {
  adopt = var.adopt_existing_foundation ? toset(["adopt"]) : toset([])

  subscription_scope = "/subscriptions/${var.subscription_id}"
  rg_scope           = "${local.subscription_scope}/resourceGroups/rg-${local.base}"
}

import {
  for_each = local.adopt
  to       = azurerm_resource_group.main
  id       = local.rg_scope
}

import {
  for_each = local.adopt
  to       = azurerm_user_assigned_identity.app
  id       = "${local.rg_scope}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-${local.base}-app"
}

import {
  for_each = local.adopt
  to       = azurerm_key_vault.main
  id       = "${local.rg_scope}/providers/Microsoft.KeyVault/vaults/${local.key_vault_name}"
}

import {
  for_each = local.adopt
  to       = azurerm_monitor_action_group.cost
  id       = "${local.rg_scope}/providers/Microsoft.Insights/actionGroups/ag-${local.base}-cost"
}

import {
  for_each = local.adopt
  to       = azurerm_consumption_budget_subscription.monthly
  id       = "${local.subscription_scope}/providers/Microsoft.Consumption/budgets/${local.base}-monthly"
}

# Role assignment names are server-generated GUIDs, so unlike everything above
# they cannot be derived and have to be supplied. Read them back with:
#
#   az role assignment list \
#     --scope "$(terraform output -raw key_vault_id)" \
#     --query "[].{role:roleDefinitionName, name:name}" -o table
#
# Set either to "" to skip that import — which is what you want if the grant does
# not exist yet and Terraform should create it.

import {
  for_each = var.adopt_existing_foundation && var.adopted_key_vault_admin_assignment_id != "" ? toset(["adopt"]) : toset([])
  to       = azurerm_role_assignment.developer_key_vault_admin
  id       = "${local.rg_scope}/providers/Microsoft.KeyVault/vaults/${local.key_vault_name}/providers/Microsoft.Authorization/roleAssignments/${var.adopted_key_vault_admin_assignment_id}"
}

import {
  for_each = var.adopt_existing_foundation && var.adopted_key_vault_secrets_user_assignment_id != "" ? toset(["adopt"]) : toset([])
  to       = azurerm_role_assignment.app_key_vault_secrets_user
  id       = "${local.rg_scope}/providers/Microsoft.KeyVault/vaults/${local.key_vault_name}/providers/Microsoft.Authorization/roleAssignments/${var.adopted_key_vault_secrets_user_assignment_id}"
}
