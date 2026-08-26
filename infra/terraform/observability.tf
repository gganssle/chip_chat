# Observability — the "is the service healthy" half.
#
# Application Insights and Arize are not overlapping purchases. App Insights
# answers request rate, container latency, dependency failures and exceptions.
# Arize answers whether the agent is behaving: the span tree for one turn, which
# tool it reached for, tokens per span. Only the first is Azure infrastructure,
# so only the first is here. Arize/Phoenix is Phase 8 and lives outside this
# stack.

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.base}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location

  sku               = "PerGB2018"
  retention_in_days = var.log_retention_days

  # A real spend control, not a tidiness setting. Ingestion is billed per GB and
  # a crash-looping container can produce a great deal of it overnight.
  # Ingestion stops when the cap is hit; logs resume the next day.
  daily_quota_gb = var.log_daily_quota_gb

  tags = local.tags
}

resource "azurerm_application_insights" "main" {
  name                = "appi-${local.base}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.location
  application_type    = "web"

  # Workspace-based. Classic Application Insights is retired.
  workspace_id = azurerm_log_analytics_workspace.main.id

  # Second layer of the same cap, because App Insights bills its own ingestion
  # through the workspace and has its own daily ceiling.
  daily_data_cap_in_gb                 = var.log_daily_quota_gb
  daily_data_cap_notifications_enabled = true

  # Strip client IPs. A public demo has no reason to retain them.
  ip_masking_enabled = true

  tags = local.tags
}
