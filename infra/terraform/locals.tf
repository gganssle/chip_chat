resource "random_string" "unique" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

locals {
  # The live stack keeps the unsuffixed names Phase 0 created by hand. Any other
  # environment gets its own namespace so a second stack can be stood up beside
  # it and thrown away.
  base = var.environment == "demo" ? "chip-chat" : "chip-chat-${var.environment}"

  # Storage account names are 3-24 characters, lowercase alphanumeric only, and
  # globally unique. Trim the readable part and let the random suffix carry
  # uniqueness rather than truncating the suffix away.
  compact = substr(replace(local.base, "-", ""), 0, 10)

  # Key Vault names are globally unique AND capped at 24 characters, which is the
  # tightest constraint in the stack after storage accounts.
  #
  # The live stack's name is derived deterministically from the subscription id
  # rather than randomly, because it has to resolve to the vault Phase 0 already
  # created for the import to find it. Every other environment gets the random
  # suffix instead: "kv-chip-chat-<env>-c8b63a" overruns 24 characters for any
  # environment name longer than two letters, and a random suffix also avoids
  # colliding with the soft-deleted remains of the previous scratch stack.
  key_vault_name = (
    var.environment == "demo"
    ? "kv-chip-chat-${substr(var.subscription_id, 0, 6)}"
    : "kv-${local.compact}-${random_string.unique.result}"
  )

  tags = {
    project      = "chip_chat"
    phase        = "0"
    issue        = "gh-5"
    environment  = var.environment
    "managed-by" = "terraform"
  }

  # Tags exactly as the Phase 0 az-CLI run (issue #3) left them on the resources
  # this stack adopts. They are preserved verbatim rather than reconciled to the
  # scheme above so that the plan immediately after `terraform import` is a true
  # no-op — which is how you tell the import found the real resource and is not
  # about to recreate it. They are also still accurate: these resources really
  # were created by hand in Phase 0 for gh-3.
  adopted_tags    = { project = "chip_chat", phase = "0", issue = "gh-3" }
  adopted_rg_tags = merge(local.adopted_tags, { "managed-by" = "manual-phase0" })

  foundation_tags    = var.adopt_existing_foundation ? local.adopted_tags : local.tags
  foundation_rg_tags = var.adopt_existing_foundation ? local.adopted_rg_tags : local.tags
}
