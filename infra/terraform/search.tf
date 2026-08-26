# Azure AI Search.
#
# Basic, authorised by the account owner on 2026-08-26 (cc-6wz) to get past the
# capacity outage in cc-3wo. IT HAS NOT BEEN CREATED YET, and the reason matters:
# eastus2 returns InsufficientResourcesAvailable for Basic exactly as it does for
# Free. Verified on 2026-08-26 by `terraform apply` and independently by
# `az search service create` at both tiers, against a subscription whose Search
# quota in the region is untouched — free 0 of 1, basic 0 of 16, standard 0 of 16.
#
# So the premise this tier change was made on turned out to be wrong. The outage
# is not the shared *free pool* being full, which paying would step around; it is
# the *region* being out of Search capacity for every tier, which paying does
# not. Buying Basic did not unblock retrieval, and no amount of spending will
# until Azure adds capacity in eastus2. The region is pinned by Snowflake Cortex
# Analyst, so moving is not on the table either.
#
# This configuration stays as-is rather than reverting to Free, because Basic is
# the authorised and better tier for the two reasons below and because both tiers
# are equally uncreatable today — reverting would buy nothing and lose the
# identity work. When capacity returns, one apply gets the service.
#
# When it does provision, it bills hourly whether or not anyone is using it:
# $0.101/hour, $73.73 over a 730-hour month, from the Azure Retail Prices API
# meter "Basic Unit" in eastus2 on 2026-08-26. Add it to the teardown runbook,
# and re-anchor the budget thresholds in cost.tf, which were set against a
# $30-60 steady state that a fixed $73.73 breaks (cc-3d5).
#
# Two things Free cannot do that this configuration depends on, and that are the
# reason to keep Basic configured while waiting:
#
#  - Managed identity, so the data plane is reached by RBAC and the admin API
#    key is switched off outright below. On Free that key was the only way in.
#  - The standard semantic billing plan. On Free, 1,000 semantic requests a
#    month was a HARD ceiling — the standard plan requires Basic or higher, so
#    past the allowance requests returned a billing error rather than a charge.
#    Here they cost $1.00 per 1,000 instead. That converts a sweep that runs
#    long from a broken eval into an invoice, which is the better failure but
#    is still a failure: keep the counter in the retrieval eval harness.

resource "azurerm_search_service" "main" {
  count = var.search_enabled ? 1 : 0

  name                = "srch-${local.base}-${random_string.unique.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  sku                 = var.search_sku

  # Admin API keys off, so the data plane is reachable only by the role
  # assignments below. This is the tier change actually paying for itself: on
  # Free this had to be `true`, which made AI Search the one service in the
  # stack reached with a shared key rather than an identity, against rfc-001
  # section 05. Nothing consumes a search key — the app is handed
  # AZURE_SEARCH_ENDPOINT and its user-assigned identity, and no output exposes
  # one — so turning them off removes a credential rather than a code path.
  #
  # Expressed against the tier rather than hardcoded false: reverting
  # var.search_sku to "free" would otherwise produce a service the provider
  # cannot configure, since Free has no RBAC data plane to fall back to.
  local_authentication_enabled = var.search_sku == "free"

  # "standard" here, since the service is Basic. The free billing plan is
  # "available on all pricing tiers" and is what a Free service gets implicitly;
  # the standard plan — pay-as-you-go past the 1,000-request allowance —
  # requires Basic or higher, and the API rejects the field outright on a free
  # service. The conditional is what makes reverting the tier a one-line change.
  semantic_search_sku = var.search_sku == "free" ? null : "standard"

  tags = local.tags
}

# Data-plane RBAC for the app identity. Written while the service was Free, where
# they were inert; on Basic they are the only way in, since local authentication
# is off above.
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
