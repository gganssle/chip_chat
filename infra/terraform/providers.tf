# Provider configuration.
#
# The `features` block is where teardown actually gets decided. Azure keeps
# several of the resources below in a soft-deleted purgatory after a delete,
# and a name that is still soft-deleted cannot be reused. Left at their
# defaults, `terraform destroy` would leave a Key Vault name reserved for seven
# days, three Cognitive Services accounts reserved for forty-eight hours and a
# Log Analytics workspace reserved for fourteen — which turns "teardown is one
# command" into "teardown is one command and then you wait".
#
# Every purge flag below exists to make destroy mean destroy. That is the right
# trade for a demo subscription and the wrong one for production: purge is
# irreversible, and on a stack holding real data you want the recovery window.

provider "azurerm" {
  subscription_id = var.subscription_id

  # A subscription only has the resource providers somebody has registered, and
  # azurerm stopped registering them automatically. Creating a registry on a
  # subscription that has never had one fails with a 409
  # MissingSubscriptionRegistration -- which reads like a permissions problem and
  # is not one. Registering it here means the next fresh subscription does not
  # rediscover that.
  #
  # "core" is azurerm's own default set. Everything else this stack uses was
  # already registered by the Phase 0 work; Microsoft.ContainerRegistry is the
  # one that arrived with issue #16.
  resource_provider_registrations = "core"
  resource_providers_to_register  = ["Microsoft.ContainerRegistry"]

  features {
    key_vault {
      # Purge on destroy so the globally unique vault name is immediately
      # reusable; recover on create so a re-apply inside the soft-delete window
      # adopts the old vault instead of failing on a name conflict.
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = true
    }

    cognitive_account {
      # Foundry, Content Safety and Document Intelligence all soft-delete.
      purge_soft_delete_on_destroy = true
    }

    log_analytics_workspace {
      permanently_delete_on_destroy = true
    }

    application_insights {
      # Application Insights otherwise creates a "Smart Detection" alert rule
      # beside itself that Terraform does not own. It is harmless and the
      # resource-group delete sweeps it, but an unmanaged resource sitting inside
      # the group is exactly the kind of straggler the teardown story is trying
      # not to have.
      disable_generated_rule = true
    }

    resource_group {
      # Anything created outside this stack inside rg-chip-chat should not be
      # able to block the resource-group delete. The whole point is that
      # teardown does not need a human to go and find the straggler.
      prevent_deletion_if_contains_resources = false
    }
  }
}

provider "azuread" {}

provider "random" {}

data "azurerm_client_config" "current" {}

data "azurerm_subscription" "current" {}
