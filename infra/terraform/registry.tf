# The container registry.
#
# Decision D8 chose a hosted agent -- our container, run by Foundry Agent Service
# -- so this repository now produces an image, and an image needs somewhere to
# live. Issue #103 is explicit that it belongs here rather than in an `az acr
# create`: this estate already had to adopt one imperatively-created foundation
# (see imports.tf) and should not acquire a second.
#
# What it costs. There is no free tier. Basic is a fixed daily charge plus egress
# and storage above 10 GB, which for one small image and a demo's worth of pulls
# is the daily charge and nothing else -- about $5/month against the $150 ceiling
# in cost.tf. That is the whole bill for the hosted-agent shape's registry half,
# and it is why Basic is the default rather than Standard.

resource "azurerm_container_registry" "main" {
  count = var.container_registry_enabled ? 1 : 0

  name                = substr("acr${local.compact}${random_string.unique.result}", 0, 50)
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  sku                 = var.container_registry_sku

  # Off. An admin account is a username and password with push rights that lives
  # in the registry itself, which is precisely the credential this estate has
  # spent two storage accounts avoiding. CI authenticates with a federated
  # workload identity and the runtime pulls with the app's managed identity; both
  # are role assignments below, and neither is a secret anybody has to hold.
  admin_enabled = false

  # Basic does not support the retention policy that would clean untagged
  # manifests up automatically, so the estate does not pretend to have one. An
  # agent version pins an image by digest and an untagged manifest is still a
  # live reference -- deleting them on a timer would break the versions that
  # point at them, which is the opposite of what "immutable per version" needs.

  tags = local.tags
}

# --- Grants -----------------------------------------------------------------
#
# Pull for the runtime, push for the developer. Nothing here is a key.

# The app identity is what the Container App and the hosted agent run as. AcrPull
# is the whole of what a runtime needs: it may fetch an image and it may not
# replace one.
resource "azurerm_role_assignment" "app_registry_pull" {
  count = var.container_registry_enabled ? 1 : 0

  scope                = azurerm_container_registry.main[0].id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# The developer's own grant, for the documented local build path (`make
# agent-image-push`). Subscription Owner does not imply this: registry push is a
# data action, and Owner carries none, so without this `az acr login` succeeds
# and the push that follows it does not.
resource "azurerm_role_assignment" "developer_registry_push" {
  count = var.container_registry_enabled ? 1 : 0

  scope                = azurerm_container_registry.main[0].id
  role_definition_name = "AcrPush"
  principal_id         = data.azurerm_client_config.current.object_id
}
