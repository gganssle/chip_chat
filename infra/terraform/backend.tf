# Remote state.
#
# The storage account is created by infra/scripts/bootstrap-state.sh and lives in
# its own resource group, so that `terraform destroy` can empty rg-chip-chat
# without taking the state file with it.
#
# Locking is the azurerm backend's native blob lease — nothing else to provision.
# Authentication is Entra, not a storage key: the account has shared-key access
# disabled outright, so there is no key to put in a CI secret or leave in a
# shell history.
#
# A second, disposable stack is a Terraform *workspace* rather than a second
# backend. The azurerm backend suffixes the state key with the workspace name, so
# `terraform workspace new scratch` gets its own state in the same container.
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-chip-chat-tfstate"
    storage_account_name = "sttfstatec8b63a"
    container_name       = "tfstate"
    key                  = "chip-chat.tfstate"
    subscription_id      = "c8b63a71-218d-4d4c-991c-b963ed2fd1f0"
    use_azuread_auth     = true
  }
}
