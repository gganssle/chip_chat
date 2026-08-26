# The container registry, and the pull that needs no password.
#
# It exists because issue #16 asks for the app to be *on the public URL*, and an
# image has to come from somewhere. The alternative — a public registry outside
# Azure — would have put the deployment story half outside the estate this
# repository tears down with one command, which is the property `make
# infra-destroy` exists to keep.
#
# Basic tier: ~$5/month, the cheapest ACR there is, and there is no free one.
# That is a real line on a $150 budget and is the reason it is a variable rather
# than a literal.

resource "azurerm_container_registry" "main" {
  name                = "acr${local.compact}${random_string.unique.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  sku                 = var.container_registry_sku

  # No admin user. An admin user is a username and password that would then have
  # to live somewhere, and the app already has an identity — which is the whole
  # reason id-chip-chat-app exists. `az acr login` uses the developer's own
  # Entra token for the same reason.
  admin_enabled = false

  tags = local.tags
}

# The pull. Without this the Container App's revision fails to start with an
# UNAUTHORIZED that reads like a missing image rather than a missing grant, and
# that is worth knowing before it happens rather than after.
resource "azurerm_role_assignment" "app_acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}
