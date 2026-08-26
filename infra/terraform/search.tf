# Azure AI Search.
#
# IN EAST US, WHILE EVERYTHING ELSE IN THIS STACK IS IN EAST US 2. That is
# deliberate and it is the whole reason retrieval works. Do not "tidy" this back
# to var.location.
#
# East US 2 is out of AI Search capacity. Not the free pool — the region, at
# every tier: free 0 of 1, basic 0 of 16, standard 0 of 16 entitlement, all
# unused, all returning InsufficientResourcesAvailable (cc-3wo). Paying was
# tried and returns the identical 400 (cc-6wz), so money was never the lever.
# East US, next door, has capacity: a free-tier service creates there. The
# constraint was regional, so the fix is regional.
#
# Moving this one service does not move the region. var.location is pinned to
# eastus2 by Snowflake Cortex Analyst, which is Azure-native in East US 2 and
# West Europe only against an account whose region is fixed at signup
# (docs/service-inventory.md item 7). That pins Cortex Analyst and, through it,
# the estate around it. It does not pin AI Search, which is not talking to
# Snowflake. Nothing else should follow this file across the border.
#
# THE COST OF THE SPLIT is a cross-region hop on every query that touches
# retrieval: the agent runs in eastus2 Container Apps and the index answers from
# eastus. Measured 2026-08-26 from a eastus2 host against this service, see
# cc-okc for the method and the number. It lands on the same turn-latency budget
# as the Snowflake cross-region inference in GH #104, and the two costs ADD.
#
# Free, not Basic. Basic was authorised while the diagnosis was still "the free
# pool is full"; once the diagnosis turned out to be "the region is empty", the
# tier stopped being the variable and Basic stopped buying anything. It is
# pre-authorised as the fallback in this region if Free proves unworkable — but
# it costs $73.73/month and, measured rather than assumed, Free works. What Free
# costs instead is the 1,000-semantic-request monthly ceiling: a hard stop, not
# an overage, which is what makes #49's degrade-to-hybrid-without-reranking path
# required code rather than dead code.

resource "azurerm_search_service" "main" {
  count = var.search_enabled ? 1 : 0

  name                = "srch-${local.base}-${random_string.unique.result}"
  resource_group_name = azurerm_resource_group.main.name

  # The one resource that is not in var.location. See the header, and
  # var.search_location for the evidence. A resource group is a metadata
  # container with its own region, not a boundary — resources are free to sit
  # elsewhere, and rg-chip-chat stays in eastus2.
  location = var.search_location
  sku      = var.search_sku

  # Admin API keys off: the data plane is reachable only by the role assignments
  # below, per rfc-001 section 05 and finding 17. This is a security property,
  # not a tier feature, and it survived the revert from Basic to Free — Free
  # does not support a managed identity ON the service (outbound, for indexer
  # connections), but data-plane RBAC INTO the service works at every tier.
  # Verified on the live Free service on 2026-08-26 rather than assumed, because
  # cc-6wz set this expecting Basic and the obvious cheap move on reverting the
  # tier would have been to hand the admin key back.
  #
  # Nothing consumes a search key — the app gets AZURE_SEARCH_ENDPOINT and its
  # user-assigned identity, and no output exposes one — so this removes a
  # credential rather than a code path.
  local_authentication_enabled = false

  # "free" on Free, NOT null, and the difference is the whole reranker.
  #
  # cc-6wz assumed a Free service rejects this field and has to leave it unset.
  # It does not, and unset is not a neutral default — it resolves to
  # semanticSearch "disabled", which turns semantic ranking off outright rather
  # than capping it. Set to "free" the Azure control plane accepts it on a Free
  # SKU (verified live, 2026-08-26) and grants the 1,000-request monthly
  # allowance. "standard" it refuses in as many words: "Semantic Search Standard
  # Tier is not supported on Free SKU."
  #
  # So the tiers differ in the shape of the ceiling, not in whether reranking
  # exists. Free: 1,000 requests a month, then a billing error — a hard stop,
  # which is why #49's degrade-to-hybrid-without-reranking path is required
  # code. Basic and up: "standard", pay-as-you-go at $1.00 per 1,000 past the
  # same allowance, so overrunning costs money instead of failing. That makes
  # the pre-authorised upgrade a one-line change to var.search_sku.
  semantic_search_sku = var.search_sku == "free" ? "free" : "standard"

  tags = local.tags
}

# Data-plane RBAC for the app identity. These are the only way in, since local
# authentication is off above. Role assignments are control-plane objects scoped
# to the service's resource id, so they are unaffected by the service sitting in
# a different region from everything else.
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
