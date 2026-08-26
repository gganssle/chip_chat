# Azure AI Search.
#
# Free tier, which is a decision rather than a default. The semantic reranker
# runs on Free (Microsoft's guidance changed on this in 2026), the free billing
# plan grants the first 1,000 semantic requests a month, and Basic is $0.101/hour
# — roughly $74 a month for a corpus that fits in 50 MB. The full reasoning,
# including when to reconsider, is in
# docs/service-inventory.md#the-reranker-decision-issue-10.
#
# Two consequences worth knowing before you debug something:
#
#  - The 1,000 semantic queries a month is a HARD ceiling on Free. The standard
#    billing plan requires Basic or higher, so you cannot pay past it; requests
#    return a billing error instead. Evaluation sweeps are what will hit this,
#    not visitors.
#  - Free-tier semantic throughput is unpublished and shared. Do not benchmark
#    latency on it.

resource "azurerm_search_service" "main" {
  count = var.search_enabled ? 1 : 0

  name                = "srch-${local.base}-${random_string.unique.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  sku                 = var.search_sku

  # The Free tier has no managed identity, no customer-managed keys, no IP
  # firewall and no private endpoints. So this is the one service in the stack
  # reached with an API key rather than an identity — a stated PoC trade-off
  # rather than an oversight. The role assignments below still apply on Basic and
  # above; leaving them in place means moving tier is a one-line change.
  local_authentication_enabled = true

  # Null on Free, and not by omission. The semantic ranker's *free billing plan*
  # is "available on all pricing tiers" and is what a Free-tier service gets
  # implicitly; the `standard` plan — pay-as-you-go past the 1,000-request
  # allowance — requires Basic or higher, and the API rejects the field outright
  # on a free service. So on Free there is nothing to set, and the 1,000/month
  # allowance is a hard ceiling rather than a soft one you can pay past.
  semantic_search_sku = var.search_sku == "free" ? null : "standard"

  tags = local.tags
}

# Data-plane RBAC for the app identity. Inert on the Free tier (see above) and
# correct the moment the tier changes.
resource "azurerm_role_assignment" "app_search_index_data_contributor" {
  count = var.search_enabled ? 1 : 0

  scope                = azurerm_search_service.main[0].id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_search_service_contributor" {
  count = var.search_enabled ? 1 : 0

  scope                = azurerm_search_service.main[0].id
  role_definition_name = "Search Service Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}
