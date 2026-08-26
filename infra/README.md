# infra

Terraform for every Azure resource, and the account groundwork it builds on.

Everything is in Terraform so that teardown is one command and the monthly bill
can be taken to zero deliberately rather than hopefully:

```bash
make infra-destroy
```

That is the whole point of this directory. A demo that can be abandoned at any
moment needs the abandoning to be cheap, or the cost story is a promise rather
than a property.

---

## The loop

```bash
make infra-bootstrap   # once per subscription: creates the remote state backend
make infra-init        # once per checkout
make infra-plan        # what would change
make infra-apply       # stand it up
make infra-destroy     # take it down
make infra-output      # names and endpoints of whatever is standing
```

### What you need in the environment

Nothing secret, which is the point.

| | |
| --- | --- |
| `az login` | Terraform authenticates as you, through the Azure CLI. There is no service principal and no client secret in this repo. |
| `terraform` ≥ 1.9 | Import blocks with `for_each` need 1.7; the lock file pins the providers. |
| Owner or equivalent on the subscription | Role assignments require `Microsoft.Authorization/roleAssignments/write`, which Contributor does not have. |

`ARM_SUBSCRIPTION_ID` is *not* required — the subscription id is a variable with a
default, and the backend block names it explicitly. Override with
`-var subscription_id=…` if this ever points at a different account.

### Remote state

`infra/scripts/bootstrap-state.sh` creates it, and it is the one piece of the
estate that cannot itself be Terraform: a stack cannot hold the storage account
its own state lives in without a chicken-and-egg problem at both create and
destroy. It is a shell script rather than a second Terraform stack so that there
is no orphan local state file for someone to lose.

| | |
| --- | --- |
| Resource group | `rg-chip-chat-tfstate` — **a different group on purpose.** `terraform destroy` has to be able to leave `rg-chip-chat` empty, which it cannot do if the state is inside it. |
| Storage account | `sttfstatec8b63a` (deterministic from the subscription id, so the backend block can be a literal) |
| Container | `tfstate`, key `chip-chat.tfstate` |
| Auth | Entra ID (`use_azuread_auth`). Shared-key access is **disabled** on the account, so there is no storage key to leak into a CI secret. |
| Locking | The azurerm backend's native blob lease. Nothing else to provision. |
| Safety | Blob versioning and 30-day soft delete on the state container. |

---

## What the stack contains

Twenty-eight resources beyond the Phase 0 foundation. One flat root module rather
than a directory of sub-modules: a module wrapping a single instantiation of a
single resource is indirection without reuse, and the "second stack" requirement
is served by variables and workspaces instead. Files are split by concern.

| File | Resources |
| --- | --- |
| `foundation.tf` | Resource group, app managed identity, Key Vault, the two vault role assignments |
| `cost.tf` | Cost action group, subscription budget |
| `storage.tf` | ADLS Gen2 account (`raw` + `uploads` containers, lifecycle policy), Functions storage account, four data-plane role assignments |
| `search.tf` | Azure AI Search + two data-plane role assignments |
| `ai.tf` | Foundry account and project, model deployments, Content Safety, Document Intelligence, four role assignments |
| `observability.tf` | Log Analytics workspace, Application Insights |
| `compute.tf` | Container Apps environment and app, Flex Consumption plan and Function App |
| `imports.tf` | Adoption of the Phase 0 estate |
| `backend.tf` | Remote state |

### Nothing runs on a connection string

Both storage accounts have `shared_access_key_enabled = false`. That is stronger
than "we prefer managed identity" — there is no account key to leak, because the
account will not mint one. It has two consequences worth knowing before you debug
something:

- The Function App authenticates to its own storage over the app's user-assigned
  identity: `storage_authentication_type = "UserAssignedIdentity"` for the
  deployment container, and `AzureWebJobsStorage__credential = managedidentity`
  for the host's runtime state. That needs **Storage Blob Data Owner**, **Storage
  Queue Data Contributor** and **Storage Table Data Contributor**, all in
  `storage.tf`, and the Function App `depends_on` them — role assignments are
  eventually consistent and the host fails its first start without them.
- `az storage blob …` against either account needs `--auth-mode login`. The
  default is key auth and it will fail.

The one exception is Azure AI Search, and it is a stated trade rather than an
oversight: the Free tier has no managed identity at all — no customer-managed
keys, no IP firewall, no private endpoints — so it is reached with an API key.
The RBAC grants are written anyway, so moving to Basic is a one-line change. See
[item 17 in the service inventory](../docs/service-inventory.md#things-the-plan-is-quietly-wrong-about).

### Teardown is a `features` block

Azure keeps several of these resources in a soft-deleted purgatory after delete,
and a soft-deleted name cannot be reused. Left at their defaults, `terraform
destroy` would reserve the Key Vault name for 7 days, three Cognitive Services
accounts for 48 hours and the Log Analytics workspace for 14 — turning "teardown
is one command" into "teardown is one command and then you wait". Every purge
flag in `providers.tf` exists to make destroy mean destroy.

That is the right trade for a demo subscription and the wrong one for production.
Purge is irreversible; on a stack holding real data you want the recovery window.

---

## Adopting Phase 0

Issue #3 created the resource group, Key Vault, app identity, cost action group
and subscription budget imperatively with the `az` CLI. They are real, the budget
is the project's live cost guardrail, and the Key Vault name is globally unique
and not immediately reusable under soft delete. So they are **imported**, not
recreated.

`imports.tf` does this with declarative `import` blocks rather than
`terraform import` calls, so adoption shows up in a plan before it happens. The
blocks are gated on `adopt_existing_foundation`, because a disposable second
stack has no Phase 0 estate to adopt and must build its own.

The test that an import is right is that the plan is a no-op against the adopted
resources. This one was, with a single exception:

```
Plan: 7 to import, 28 to add, 1 to change, 0 to destroy.
```

Zero to destroy and zero to replace, and no attribute drift on the resource
group, Key Vault, identity or action group — including tags, which are preserved
exactly as the Phase 0 run left them (`issue = gh-3`, and `managed-by =
manual-phase0` on the group) rather than reconciled to this stack's scheme. They
are still accurate, and churning them would have obscured the only diff that
matters.

**The one change** was the budget, and it is worth writing down because it looks
alarming and is not:

```
~ resource "azurerm_consumption_budget_subscription" "monthly" {
    - contact_groups = [ ".../providers/microsoft.insights/actionGroups/ag-chip-chat-cost" ]
    + contact_groups = [ ".../providers/Microsoft.Insights/actionGroups/ag-chip-chat-cost" ]
```

The `az` CLI stored the provider namespace lowercase; Terraform emits the
canonical casing. `notification` is a *set*, so a case-only difference in one
string makes every element look new. It is an in-place update, not a replace —
**the budget never blinks out** — and it is one-time. The plan after apply is
clean.

Role assignment names are server-generated GUIDs, so unlike everything else they
cannot be derived from a name and are supplied as variables. Read them back with:

```bash
az role assignment list --scope "$(terraform -chdir=infra/terraform output -raw key_vault_id)" \
  --query "[].{role:roleDefinitionName, name:name}" -o table
```

Set either to `""` to have Terraform create the grant instead of adopting one.

---

## A second, disposable stack

`environment` and `location` are variables so a parallel stack can be stood up
and thrown away — which is also how teardown gets exercised without touching the
live estate.

```bash
terraform -chdir=infra/terraform workspace new scratch
terraform -chdir=infra/terraform apply \
  -var environment=scratch -var adopt_existing_foundation=false
```

`environment = "demo"` produces the unsuffixed names Phase 0 created by hand;
anything else namespaces the whole estate into `rg-chip-chat-<env>`. State is a
Terraform workspace, which the azurerm backend keys separately in the same
container.

**Two free tiers are one-per-subscription** — AI Search Free, and F0 on each
Cognitive Services kind — so a second stack has to pay for them or the apply
fails on a quota error. `terraform.tfvars.example` has the overrides. Budget for
roughly $74/month of AI Search Basic for as long as the scratch stack exists,
which is the main reason to destroy it promptly.

---

## Cost guardrails, encoded

These are Terraform defaults, not documentation. Changing one should feel like a
decision.

| Guardrail | Where | Why |
| --- | --- | --- |
| AI Search **Free** tier | `var.search_sku` | Basic is $0.101/hour ≈ $73.73/month. The semantic reranker runs on Free, capped at 1,000 semantic queries a month — a hard ceiling, since the pay-as-you-go plan requires Basic. [The reranker decision](../docs/service-inventory.md#the-reranker-decision-issue-10) |
| Container Apps **min replicas 0** | `var.web_min_replicas` | An idle replica still bills, at roughly an eighth of the active vCPU rate. |
| An **HTTP scale rule**, not CPU or memory | `compute.tf` | Not tuning. Scale-to-zero has two documented ways to strand an app: no ingress and no scale rule means it can never wake, and the KEDA CPU/memory scalers cannot scale to zero at all. |
| Blob lifecycle **deleting uploads after a day** | `var.uploads_retention_days` | Data hygiene first, cost second. |
| **Blob soft delete OFF** on the uploads account | `storage.tf` | With it on, the lifecycle rule only *soft*-deletes and the images are retained for the full soft-delete window. Asserted with a `postcondition`, because it is disabled by omitting a block and an omission is easy to add back by accident. |
| Log Analytics **1 GB/day cap** | `var.log_daily_quota_gb` | Ingestion is billed per GB and a crash-looping container makes a lot of it overnight. Applied twice: on the workspace and on the App Insights component. |
| Model deployment capacity **10K TPM**, and none by default | `var.model_deployments` | Tokens-per-minute quota is a spend control as much as a performance setting. |
| Container Apps **max replicas 2** | `var.web_max_replicas` | Scale-out is spend. |

### What none of this prevents

> Budget alerts notify. They do not prevent anything. Nor does any of the above.

Every guardrail here is a ceiling on *capacity*, not on *spend in the request
path*. A `curl` loop against a public, unauthenticated demo will drive real token
cost inside all of them. The thing that actually prevents that is the inline
daily token counter in [issue #17](https://github.com/gganssle/chip_chat/issues/17),
checked before the model is called. This directory cannot do that job and should
not be mistaken for doing it.

### The 24-hour rule is a 24–48 hour rule

`uploads_retention_days = 1` is the tightest expiry a lifecycle rule can express:
conditions have *day* granularity, the engine takes up to 24 hours to begin
executing after a policy change, and it then runs periodically rather than
continuously. Real behaviour is deletion **24 to 48 hours** after upload.
User-facing copy should say "within 48 hours" and mean it.

---

## Known issue: AI Search will not provision

As of 2026-08-26, creating the Free-tier search service in East US 2 fails:

```
InsufficientResourcesAvailable: The region 'eastus2' is currently out of the
resources required to provision new services.
```

This is Azure's shared **regional capacity**, not subscription quota — the usage
API reports free 0 of 1 used — and it reproduces through the `az` CLI as readily
as through Terraform. The region is not negotiable (Cortex Analyst), so waiting
is the option that keeps the design intact.

`var.search_enabled = false` applies the rest of the estate meanwhile. Note that
it also unblocks the Container App, which references the search endpoint and is
therefore ordered behind it. Retrieval is Phase 5, so nothing earlier is blocked.
Tracked as bead `cc-3wo`; if capacity does not return, the escalation is Basic at
~$74/month, which is a cost decision for the account owner.


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
| App managed identity | `id-chip-chat-app` — client and principal ids are `terraform output`, see below |
| Budget | `chip-chat-monthly` (subscription scope) |
| Cost action group | `ag-chip-chat-cost` |
| Terraform state | `sttfstatec8b63a/tfstate` in `rg-chip-chat-tfstate` |

Everything above is stable across a teardown because it is either fixed
(subscription, tenant, region) or deterministic (the resource group name; the
Key Vault name is derived from the subscription id).

**The identity's client and principal ids are not.** They are server-generated
and a teardown rotates them, which is why they are outputs rather than a table
entry. So are every storage account, search service, Foundry account and Function
App name, all of which carry a random suffix. Read them from the stack, never
from a document:

```bash
make infra-output
terraform -chdir=infra/terraform output -raw app_identity_client_id
```

If Terraform is not initialised, the account will answer too:

```bash
az group show -n rg-chip-chat --query "{name:name, location:location}" -o table
az keyvault show -n kv-chip-chat-c8b63a --query properties.vaultUri -o tsv
az resource list -g rg-chip-chat -o table
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

Issue #5 added one more: **`Microsoft.Web`**, which the Flex Consumption plan and
Function App need and which was still `NotRegistered`. It is not in the Terraform
— provider registration is subscription-level and outlives any stack — so on a
fresh subscription, register it before the first apply:

```bash
az provider register -n Microsoft.Web
```

```bash
az provider show -n Microsoft.KeyVault --query registrationState -o tsv
```

---

## What Terraform owns, and what it does not

**Outside the state file, permanently.** The subscription itself, and the
remote-state storage account in `rg-chip-chat-tfstate`.

**Inside the state file.** Everything else, including the resource group, the Key
Vault and its role assignments, the managed identity, the cost action group and
the subscription budget.

### The budget is inside, and that was argued about

An earlier draft of this file put the budget and the action group permanently
outside Terraform, on the reasoning that *a budget Terraform can destroy is a
budget that vanishes exactly when someone tears down the stack to stop spending
money*. That is a good argument and it is recorded here rather than deleted,
because the counter-argument is what decided it and the counter-argument is
narrower than it looks.

Issue #5 is asked for so that teardown is **one** command. A cost guardrail that
survives `terraform destroy` is a resource someone has to remember to remove by
hand, which is exactly the class of thing that is still billing three months
after a demo was abandoned. And the failure mode the earlier draft feared —
alerting disappearing at the moment you need it — requires a *partial* teardown:
destroy removes everything the budget was watching, so there is nothing left for
it to watch.

So: the budget and the action group are in state. **The one rule that follows
from this**: if you ever tear down part of this estate and leave the rest
running, recreate the budget first. `make infra-destroy` is all-or-nothing and
safe; `terraform destroy -target=…` is not.

The adoption itself was in-place, not a recreate — the budget did not blink out.
See [Adopting Phase 0](#adopting-phase-0).

### Secrets

Secret *values* are never in Terraform, not even in state. Terraform creates the
vault and the grants; a human puts secrets in it. The two places this rubs:

- **The AI Search admin key** is a computed attribute of the search service, so
  it exists in state. It is not written to Key Vault by Terraform — copy it
  across by hand, and treat the state container as sensitive regardless (it is
  Entra-only and has no shared key, which is why it is safe enough).
- **The Application Insights connection string** carries an ingestion key and is
  an app setting on both the Container App and the Function App. It is marked
  `sensitive` in outputs; read it deliberately with
  `terraform output -raw application_insights_connection_string`.

---

## Verified

Against subscription `c8b63a71-218d-4d4c-991c-b963ed2fd1f0`, 2026-08-26, with
`az` 2.89.1, Terraform 1.15.8 and `hashicorp/azurerm` 5.2.0.

### The live stack

| | |
| --- | --- |
| Adoption | `7 to import, 28 to add, 1 to change, 0 to destroy`. No attribute drift on the resource group, Key Vault, identity or action group. The one change was the budget's case-only `contact_groups` diff, applied in place. |
| Standing | 15 resources in `rg-chip-chat`, everything except AI Search. |
| Idempotence | `terraform plan` after apply: **No changes.** |
| Chat app | `https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io` returns **HTTP 200** over TLS on the Container Apps default FQDN, with its automatic managed certificate. No DNS zone, no CNAME, no certificate resource — issue #4 was closed in favour of this. |
| **Cold start from zero replicas** | **0.26 s** to first byte, 0.21 s warm. The design guessed "a couple of seconds" and Microsoft publishes no figure; this is the measured number for a trivial container, and a real FastAPI image will be slower. |
| Budget | Alive and unchanged at $150/month throughout. |

### `terraform apply` from clean, and `terraform destroy`

Exercised on a **disposable second stack** rather than on the live estate, which
is what the `environment` variable is for — tearing down `rg-chip-chat` to prove
teardown works would have taken the budget with it for the duration.

```bash
terraform apply  -var environment=scratch -var adopt_existing_foundation=false \
                 -var search_enabled=false -var content_safety_sku=S0 \
                 -var document_intelligence_sku=S0
terraform destroy -var environment=scratch ...
```

| | |
| --- | --- |
| Apply from nothing | `30 added, 0 changed, 0 destroyed` — including the Container App, which the live stack cannot reach while it is ordered behind AI Search. |
| Idempotence | **No changes** on the following plan. |
| Guardrails, read back from Azure | HNS `true`, shared-key access `false`, TLS 1.2. Blob soft delete `false`, container soft delete `false`, versioning `false`. Lifecycle rule `expire-uploads`: `blockBlob`, prefix `uploads/`, `daysAfterCreationGreaterThan: 1`. Container App `minReplicas` 0, `maxReplicas` 2, HTTP scale rule at 20 concurrent requests. |
| Destroy | `32 destroyed` in **9m20s**. |
| Result | `az group exists -n rg-chip-chat-scratch` → **false**. The group is gone, not merely empty. |
| No residue | `az keyvault list-deleted` and `az cognitiveservices account list-deleted` both empty — the purge flags in `providers.tf` did their job, so every globally unique name is immediately reusable. |
| Live estate | Untouched throughout: 15 resources, budget intact. |

The scratch run also earned its keep by catching a real bug before it could
matter: `kv-chip-chat-<env>-c8b63a` overruns the 24-character Key Vault name
limit for any environment name longer than two letters. Non-`demo` stacks now
take the random suffix instead.

### Not verified

- **Azure AI Search** — cannot be provisioned in East US 2 at all right now. See
  [Known issue](#known-issue-ai-search-will-not-provision) and bead `cc-3wo`.
- **Model deployments** — `var.model_deployments` is empty by design; issue #8
  chooses the models and confirms quota.
- **The budget's test notification** — see above; it sends real mail and is left
  for the account owner.
