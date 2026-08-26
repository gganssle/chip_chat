# infra

Terraform and the Azure account groundwork it builds on.

Terraform itself is [issue #5](https://github.com/gganssle/chip_chat/issues/5) and is
not written yet. This file records the part that comes first: the subscription-level
resources created by hand in Phase 0, because subscription choice, billing and budget
configuration are one-time human actions that do not belong in a state file.

---

## Identifiers

Everything below is a resource id or a public endpoint. **No secrets are recorded
here** — secrets live in the Key Vault named below, which is the point of the Key
Vault. The root `.gitignore` excludes `.env`; keep it that way.

| | |
| --- | --- |
| Subscription name | `Azure subscription 1` |
| Subscription id | `c8b63a71-218d-4d4c-991c-b963ed2fd1f0` |
| Tenant id | `afededb7-6b20-4ec3-afd5-b27ac9242bbf` |
| Region | **East US 2** (`eastus2`) |
| Resource group | `rg-chip-chat` |
| Key Vault | `kv-chip-chat-c8b63a` |
| Key Vault URI | `https://kv-chip-chat-c8b63a.vault.azure.net/` |
| App managed identity | `id-chip-chat-app` |
| — its client id | `8874457f-dd55-4007-9de5-14e4710920cb` |
| — its principal id | `d5afe409-bd0e-4176-a873-46f14e1f0424` |
| Budget | `chip-chat-monthly` (subscription scope) |
| Cost action group | `ag-chip-chat-cost` |

Read them back from the account rather than trusting this table, which goes stale the
moment someone renames something:

```bash
az group show -n rg-chip-chat --query "{name:name, location:location}" -o table
az keyvault show -n kv-chip-chat-c8b63a --query properties.vaultUri -o tsv
az identity show -n id-chip-chat-app -g rg-chip-chat --query "{clientId:clientId, principalId:principalId}" -o table
```

### Why East US 2

Forced, not preferred. Snowflake Cortex Analyst is natively available on Azure in East
US 2 and West Europe only, and a Snowflake account's region is fixed at signup — so the
account lane pins the region for everything else. East US 2 also carries the full
Foundry Agent Service tool matrix and complete Content Safety coverage. The full
comparison, including the one trade-off, is in
[docs/service-inventory.md](../docs/service-inventory.md#region-recommendation-east-us-2).

---

## Key Vault

Every secret the app tier reads goes here: Snowflake credentials, the Databricks PAT,
the Arize API key, Foundry keys, and the ops API function key. No secret in source, no
secret in a committed `.env`.

The vault uses **RBAC authorization**, not the legacy access-policy model, so grants are
ordinary role assignments and Terraform can manage them alongside everything else.

| Principal | Role | Why |
| --- | --- | --- |
| `grahamganssle@gmail.com` (developer) | Key Vault Administrator | Writes the secrets |
| `id-chip-chat-app` (managed identity) | Key Vault Secrets User | Reads them at runtime; read-only on purpose |

The app identity exists now, ahead of the Container App that will carry it, so that the
grant is real rather than a promise. Phase 8 assigns `id-chip-chat-app` to the Container
App; nothing about the vault changes when it does.

Soft-delete retention is the 7-day minimum and **purge protection is off**, deliberately:
issue #5 wants teardown to be a single command, and purge protection is irreversible and
would block reusing the vault name for 90 days after every teardown. That is the right
trade for a demo subscription and the wrong one for production — if this stack ever holds
real customer data, turn purge protection on and accept the slower teardown.

```bash
az keyvault secret set  --vault-name kv-chip-chat-c8b63a --name <name> --value <value>
az keyvault secret show --vault-name kv-chip-chat-c8b63a --name <name> --query value -o tsv
```

---

## Cost guardrails

> **Budget alerts notify. They do not prevent anything.**

This deserves saying plainly because the alert's existence is exactly the kind of thing
that creates false comfort. The budget below emails after Azure's cost pipeline has
already recorded the spend — hours late, and with no ability to stop what caused it. A
`curl` loop pointed at a public, unauthenticated demo overnight will produce a real bill
and a polite email about it the next morning.

The thing that actually prevents spend is the inline synchronous token cap in
[issue #17](https://github.com/gganssle/chip_chat/issues/17) — a running daily counter
checked in the request path, before the model is called, that flips the app into a
friendly exhausted state. Both are required and they do different jobs: the cap stops the
bleeding, the budget tells you the cap failed. Arize is not this guardrail either;
observability is asynchronous and always slightly behind.

### The budget

`chip-chat-monthly`, subscription scope, **$150/month**, resetting monthly from
2026-08-01.

$150 is chosen to sit clearly above expected steady state — Container Apps scaled to
zero, AI Search on free tier, Document Intelligence and Content Safety on their free
allowances, modest token spend, the occasional single-node Databricks job — which should
land somewhere around $30–60. That gap matters: at $150, crossing 50% is a genuine signal
that something is wrong rather than a monthly formality you learn to ignore.

| Threshold | Type | Meaning |
| --- | --- | --- |
| 50% ($75) | Actual | Spend is roughly double the expected run rate. Look now. |
| 80% ($120) | Actual | Something is wrong and has been for a while. |
| 100% ($150) | Actual | The ceiling is gone. |
| 100% ($150) | Forecasted | Azure projects the month will end over the ceiling — the earliest of the four, and the only one that arrives while there is still time to act. |

All four notify `grahamganssle@gmail.com` directly and through the `ag-chip-chat-cost`
action group. The action group is the extensible half: adding SMS, a webhook or a
PagerDuty leg later means editing one action group, not four notification blocks.

```bash
# Read the budget and its current spend
az rest --method get --url "https://management.azure.com/subscriptions/c8b63a71-218d-4d4c-991c-b963ed2fd1f0/providers/Microsoft.CostManagement/budgets/chip-chat-monthly?api-version=2023-11-01"

# What has actually been spent this month
az consumption usage list --start-date $(date -u +%Y-%m-01) --end-date $(date -u +%Y-%m-%d) -o table
```

### Pending: the test notification

Issue #3's fourth acceptance criterion asks for a received test notification. Azure will
not let you trip a real budget threshold on demand, so the way to prove the delivery path
works is to fire a synthetic budget alert at the action group:

```bash
az monitor action-group test-notifications create \
  -g rg-chip-chat --action-group ag-chip-chat-cost \
  --alert-type budget -a email cost-owner grahamganssle@gmail.com
```

**This has not been run yet** — it sends real mail to a real inbox, so it is left for the
account owner. Everything it exercises is already verified as configured: the receiver
shows `status: Enabled`, and all four notifications reference the action group. Running
it confirms the last hop, which is the mailbox.

---

## Resource providers

A fresh subscription starts with most providers unregistered, and the failure is a
confusing `NoRegisteredProviderFound` at `terraform apply` time rather than at plan time.
These were registered during Phase 0:

`Microsoft.KeyVault` · `Microsoft.App` · `Microsoft.Search` · `Microsoft.OperationalInsights` · `Microsoft.Storage` · `Microsoft.Insights`

Already registered on the subscription: `Microsoft.CognitiveServices`,
`Microsoft.ManagedIdentity`, `Microsoft.Consumption`, `Microsoft.CostManagement`.

```bash
az provider show -n Microsoft.KeyVault --query registrationState -o tsv
```

---

## What Terraform owns, and what it does not

Issue #5 puts every Azure resource in Terraform so teardown is one command. The
boundary:

**Outside the state file, permanently.** The subscription, the budget, and the cost
action group. A budget that Terraform can destroy is a budget that vanishes exactly when
someone tears down the stack to stop spending money — which is the worst possible moment
for the alerting to go with it.

**Inside the state file, once #5 lands.** The resource group, the Key Vault and its role
assignments, the managed identity, and everything Phases 1–9 add. `rg-chip-chat`,
`kv-chip-chat-c8b63a` and `id-chip-chat-app` exist by hand today and should be
`terraform import`ed rather than recreated — the Key Vault name in particular is globally
unique and, under soft delete, not immediately reusable.

Secret *values* are never in Terraform. Terraform creates the vault and the grants; a
human puts secrets in it.

---

## Verified

2026-08-26, against subscription `c8b63a71-218d-4d4c-991c-b963ed2fd1f0` with
`az` 2.89.1. Resource group, vault and identity all report `Succeeded`; a secret was
written, read back and deleted through the developer's RBAC grant to confirm the
authorization model works rather than merely exists.
