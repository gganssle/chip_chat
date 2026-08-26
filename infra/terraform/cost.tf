# Cost guardrails.
#
# > Budget alerts notify. They do not prevent anything.
#
# The budget emails after Azure's cost pipeline has recorded the spend — hours
# late, with no ability to stop what caused it. The thing that actually prevents
# spend is the inline daily token cap in issue #17, checked in the request path
# before the model is called. Both are required and they do different jobs: the
# cap stops the bleeding, the budget tells you the cap failed.
#
# Both of these already exist (issue #3) and are adopted, not recreated. Issue
# #5 asks for them to be inside the state file so that teardown is genuinely one
# command; the cost of that is that `terraform destroy` takes the budget with it.
# That is the correct trade only because destroy removes everything the budget
# was watching. If you ever tear down part of this estate and leave the rest
# running, recreate the budget first.

resource "azurerm_monitor_action_group" "cost" {
  name                = "ag-${local.base}-cost"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "chipcost"
  location            = "global"

  # The extensible half of the alerting path: adding SMS, a webhook or a
  # PagerDuty leg later means editing one action group, not four notification
  # blocks.
  email_receiver {
    name                    = "cost-owner"
    email_address           = var.cost_alert_email
    use_common_alert_schema = false
  }
}

resource "azurerm_consumption_budget_subscription" "monthly" {
  name            = "${local.base}-monthly"
  subscription_id = data.azurerm_subscription.current.id
  amount          = var.monthly_budget_usd
  time_grain      = "Monthly"

  time_period {
    start_date = var.budget_start_date
    end_date   = var.budget_end_date
  }

  # 50% is set well below the expected run rate on purpose. Steady state should
  # land around $30-60, so crossing $75 is a genuine signal rather than a
  # monthly formality you learn to ignore.
  notification {
    enabled        = true
    threshold      = 50
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.cost_alert_email]
    contact_groups = [azurerm_monitor_action_group.cost.id]
  }

  notification {
    enabled        = true
    threshold      = 80
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.cost_alert_email]
    contact_groups = [azurerm_monitor_action_group.cost.id]
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = [var.cost_alert_email]
    contact_groups = [azurerm_monitor_action_group.cost.id]
  }

  # The forecast alert is the only one of the four that arrives while there is
  # still time to act.
  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Forecasted"
    contact_emails = [var.cost_alert_email]
    contact_groups = [azurerm_monitor_action_group.cost.id]
  }
}
