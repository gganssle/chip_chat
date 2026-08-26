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
| `registry.tf` | Container registry for the agent image, plus its pull and push grants |
| `databricks.tf` | Databricks workspace, Unity Catalog access connector and its ADLS grant, storage credential, external locations, Key Vault entries |
| `databricks_compute.tf` | Cluster policies, cluster-create entitlements, the jobs service principal, the app identity's registration, the ADLS smoke job |
| `databricks_catalog.tf` | The `chip_chat` catalog, the six medallion schemas, the grants on both, the read-only principal, and the two jobs that verify lineage and refusal |
| `imports.tf` | Adoption of the Phase 0 estate |
| `backend.tf` | Remote state |

### The registry is here rather than made by hand

Decision D8 made the agent a hosted agent, so this repository produces a
container image and the image needs a registry.
[Issue #103](https://github.com/gganssle/chip_chat/issues/103) is explicit that it
belongs in Terraform: this estate has already had to adopt one imperatively-created
foundation (see *Adopting Phase 0* below) and should not acquire a second.

Its **admin account is disabled**, for the same reason both storage accounts have
shared keys off — an admin account is a username and password with push rights,
stored in the registry. Access is two role assignments instead:

| Grant | Who | Why |
| --- | --- | --- |
| `AcrPull` | the app managed identity | The runtime may fetch an image and may not replace one. This is what the Container App and the hosted agent pull with. |
| `AcrPush` | you | The documented local build path, `make agent-image-push`. Subscription Owner does *not* imply it: registry push is a data action and Owner carries none, so without this `az acr login` succeeds and the push after it does not. |

CI pushes over OIDC federation rather than either of those — see
`.github/workflows/agent-image.yml`, which skips the publish (and says so) when the
federation secrets are absent, so a clone with no Azure account still gets the
build gate.

Basic does not support a retention policy for untagged manifests, and the estate
does not pretend to have one. That is the right way round here anyway: an agent
version pins an image by **digest**, so an untagged manifest is still a live
reference and deleting it on a timer would break the version pointing at it.

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
| Container registry **Basic** tier | `var.container_registry_sku` | There is no free tier. Basic is ~$0.167/day (~$5/month) and includes 10 GB — two orders of magnitude more than one 60 MB agent image. Standard and Premium buy storage, geo-replication and private endpoints that nothing here uses. Set `var.container_registry_enabled = false` on a disposable stack that does not need the agent. |
| Databricks **single-node compute**, all three policies | `databricks_compute.tf` | `num_workers` fixed at zero. There is no second VM to forget about and no autoscale ceiling to get wrong. |
| Databricks **job policy forbids all-purpose clusters** | `databricks_cluster_policy.job_single_node` | All-purpose is $0.55/DBU-hr against $0.30 for jobs, for compute that is easy to leave running. A cluster made under this policy cannot outlive its run. |
| Databricks **10-minute autotermination**, interactive only | `var.databricks_autotermination_minutes` | Fixed, not a maximum — a maximum still lets someone type 4320. Job and pipeline compute reject the attribute and terminate structurally instead. |
| Databricks **node type allowlist**, all 4-vCPU | `var.databricks_node_type_allowlist` | Editing the list is where the decision to spend more belongs. |
| Databricks **no instance pools** | `databricks_compute.tf` | Pools keep VMs warm, which is the opposite of the point. |
| Databricks **no cluster-create entitlement** on `users` | `databricks_entitlements.users` | What turns the policies from selectable into mandatory for everyone who is not an admin. |
| Databricks **no SQL access** on `users` | `databricks_entitlements.users` | Serverless SQL is ~$8/hour for a Small warehouse and no cluster policy binds it. |

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

## Databricks

Issue #31. The workspace, three cluster policies, the identity Unity Catalog
reaches ADLS with, and one job that proves the path.

| | |
| --- | --- |
| Workspace | `dbw-chip-chat`, **Premium**, East US 2 |
| Managed resource group | `rg-chip-chat-databricks-managed` — created and owned by the Databricks RP |
| Unity Catalog | Metastore `metastore_azure_eastus2` **auto-assigned** at creation; default catalog `dbw_chip_chat` |
| Access connector | `dbac-chip-chat`, system-assigned identity, **Storage Blob Data Contributor** on the data account and nothing else |
| External locations | `chip-chat-raw` → `abfss://raw@…`, `chip-chat-lakehouse` → `abfss://lakehouse@…` |
| Jobs identity | Databricks service principal `chip-chat-jobs`. **No credential exists for it** — see below |
| App identity | `id-chip-chat-app` registered as a Databricks service principal, so the app tier authenticates over Entra with no stored secret |

**Premium is not a preference.** Standard tier retires 2026-10-01 and every new
workspace becomes Unity-Catalog-only from 2026-09-30 — both inside this
project's window. Unity Catalog needs Premium anyway, so Premium with an
auto-assigned metastore costs nothing extra and sidesteps both dates. The
`precondition` in `databricks.tf` makes "create it Standard and upgrade later"
fail at plan time rather than in October.

### What it costs when nothing is running

> **Not zero.** An idle Databricks workspace in this estate bills **≈$36.50 a
> month** before a single job is submitted.

This is the one number worth reading twice, because it is a standing charge and
because it is not in the Databricks bill — it is in the Azure bill, for
networking inside the Databricks-managed resource group:

| Resource | Meter | Rate | Monthly |
| --- | --- | --- | --- |
| `nat-gateway` | NAT Gateway, Standard Gateway | $0.045/hour | **$32.85** |
| `nat-gw-public-ip` | Standard IPv4 Static Public IP | $0.005/hour | **$3.65** |
| | | | **$36.50** |

Rates from the Azure retail prices API, East US 2, 2026-08-26. Data processed
through the NAT gateway is another $0.045/GB on top.

Databricks provisions the NAT gateway because the workspace uses **secure
cluster connectivity** — cluster VMs get no public IP and reach the internet
through the gateway instead. It is created with the workspace, bills whether or
not any cluster exists, and is deleted with the workspace. `var.databricks_sku`
carries no standing charge of its own; Premium changes DBU rates, not a monthly
fee.

Against a $150/month budget this is 24%, and it roughly doubles the estate's
expected idle floor — the rest of the stack idles near $0 by design (Container
Apps at zero replicas, AI Search Free, F0 Cognitive Services). It is worth
knowing before the budget's 50% alert arrives and looks like a runaway.

**The lever, if that is too much:** turning secure cluster connectivity off
(`no_public_ip = false`) removes the NAT gateway and gives cluster VMs their own
public IPs, billed only while a cluster runs. That is a real security downgrade
and it needs the workspace replaced, so it is not the default and it is not
wired up. The honest position is that $36.50/month buys cluster nodes with no
inbound-addressable public IP, and that is worth it here.

Everything else about the workspace is genuinely zero at rest: **no cluster
exists until a job is submitted, and nothing in this stack starts one on a
schedule.** The smoke job below has no trigger and no schedule; the cluster
policies forbid instance pools, which are the other way to keep VMs warm.

### What a run costs

Single-node job compute, `Standard_D4ds_v4`:

| | |
| --- | --- |
| DBU rate | Premium **Jobs Compute, $0.30/DBU-hour** (all-purpose would be $0.55) |
| VM | $0.226/hour, 4 vCPU / 16 GB |
| Measured | see [Verified](#databricks-verified) below |

### The cluster policies, and exactly how much they enforce

Three policies, in `databricks_compute.tf`. Every constraint below is `fixed` —
a value a user cannot raise — unless the table says otherwise.

| | `chip-chat-job-single-node` | `chip-chat-interactive-single-node` | `chip-chat-pipeline-single-node` |
| --- | --- | --- | --- |
| `cluster_type` | `job` (fixed) | `all-purpose` (fixed) | `dlt` (fixed) |
| `num_workers` | `0` (fixed) | `0` (fixed) | `0` (fixed) |
| `autoscale.*` | forbidden | forbidden | forbidden |
| `autotermination_minutes` | — *(rejected by the platform)* | **`10` (fixed)** | — *(rejected by the platform)* |
| `node_type_id` | allowlist of five 4-vCPU types | same | same |
| `instance_pool_id` | forbidden | forbidden | forbidden |
| `max_clusters_per_user` | — | **1** | — |

**Where the ten minutes actually applies, and why it is only one column.** Job
compute and pipeline compute both *reject* `autotermination_minutes` outright —
job creation fails with "Automated clusters do not support autotermination".
This is not a limitation: both tear their cluster down when the run finishes, so
termination there is structural rather than a timeout. There is no idle cluster
to time out because there is no cluster once the work stops. The ten minutes
belongs to the only kind of compute that *can* sit idle and bill — the
interactive one — which is also the only place the design's cost trap can
happen. `docs/service-inventory.md` records this trap for pipelines; it is true
of jobs as well, found by hitting it on 2026-08-26.

#### Is the policy attached by default, or only available to be selected?

Databricks has no concept of a policy that is automatically applied to every
cluster. So the literal answer is **selectable, not default-attached** — and
that would be worth nothing on its own. What makes it binding is removing the
alternative:

1. The `users` group has **no `allow-cluster-create` entitlement** (and none for
   instance pools), asserted by `databricks_entitlements.users`. Without that
   entitlement, the only way a member can create compute at all is through a
   policy they hold `CAN_USE` on.
2. The five built-in policy families — Personal Compute, Power User Compute,
   Shared Compute, Job Compute, Legacy Shared Compute — are granted to
   **`admins` only** on this workspace, verified 2026-08-26. So they are not an
   escape route for a non-admin.
3. The only policy granted to a non-admin principal is the job policy, granted
   to `chip-chat-jobs`.

So for everyone who is not a workspace admin, policy-scoped creation is the only
creation available. That is enforcement, not advice.

> **A workspace admin bypasses all of it.** Admins can create unrestricted
> compute and can edit or delete these policies. Databricks provides no way to
> prevent that, and today the only human in this workspace *is* an admin. Read
> the guardrail as: it removes every accidental path, and it does not constrain
> a deliberate one.

Two things worth knowing about the built-in Personal Compute policy, since it is
the usual leak: its `autotermination_minutes` is **`unlimited`, defaulting to
4320 minutes — three days** — and its default node type is `Standard_D4ds_v5`,
which this subscription has zero quota for. Admin-only here, but that is what it
would grant if it were ever opened up.

#### What the policies do not reach

- **SQL warehouses are not clusters, and no cluster policy binds them.** The
  workspace ships with a `Serverless Starter Warehouse` (Small, PRO, serverless,
  auto-stop 10 minutes) that nothing in this design uses — the
  natural-language-to-SQL lane is Snowflake Cortex Analyst. Serverless SQL is
  **$0.70/DBU-hour** and a Small warehouse draws about 12 DBU/hour, so a started
  warehouse is roughly **$8/hour**: the most expensive way to spend money in
  this workspace and the one the policies cannot govern. It is `STOPPED`, and
  `databricks_entitlements.users` removes `databricks-sql-access` from the
  `users` group so that starting it needs an admin. That is a narrowing — the
  group had the entitlement — and today it changes nothing, because the only
  member is an admin.
- **Jobs and pipelines only obey a policy they reference.** A `policy_id` on the
  cluster spec is what binds them; there is no ambient default. The smoke job
  sets it. Issues #33 and #34 must set
  `databricks_pipeline_policy_id` on their pipeline compute, which is why it is
  an output.
- **Serverless compute** is billed per DBU with no cluster to police. Nothing
  here uses it.

### Credentials: there are none, and that is the design

Issue #31 asks for "a service principal for automated jobs; PAT stored in Key
Vault". It gets the service principal. It does not get the PAT, for two reasons
and in that order:

1. **The jobs principal never needs one.** `run_as` is resolved inside
   Databricks, so a job running as `chip-chat-jobs` authenticates without
   anything being issued, stored or rotated. The credential in the original
   scope was for the other direction — something outside Databricks calling in.
2. **For that direction the app already has an identity.** `id-chip-chat-app`,
   the user-assigned managed identity every other Azure service in this stack is
   reached with, is registered here as a Databricks service principal. The app
   tier presents an Entra token and is recognised. No PAT, no Key Vault secret,
   nothing to expire.

This is the same rule `storage.tf` applies with `shared_access_key_enabled =
false`: the strongest version of "the key did not leak" is that there is no key.

`databricks-host` is in Key Vault, so a consumer reads one place for the
endpoint. It is not a secret; it is there so a teardown cannot leave a stale
host in someone's config.

**If something genuinely cannot do Entra**, `var.databricks_service_principal_token_enabled`
mints an on-behalf-of PAT for `chip-chat-jobs` and writes it to the
`databricks-token` secret with an expiry. It is off, and it currently cannot be
turned on without a human: on-behalf-of token creation is disabled for new
Databricks accounts, there is no workspace API for it, and an account admin has
to enable it at **accounts.azuredatabricks.net → Settings → Feature enablement →
"Personal access tokens for service principals"**. Verified against this account
on 2026-08-26 — the API answers *"On-behalf-of token creation for service
principals is not enabled for this workspace"*, and `enableTokensConfig` (which
is already `true`) is a different setting.

### Two things that need an account admin, not Terraform

Both are one-time, and both are the reason this issue is labelled *human /
portal work*:

- **`var.databricks_account_id`** is the Databricks account GUID. Binding a
  job's `run_as` to a service principal is an *account*-level permission
  (`roles/servicePrincipal.user`) and is not implied by being an account admin —
  without it, creating the job fails with a message about workspace ACLs that is
  not about workspace ACLs. The account id is not derivable from the Azure
  subscription; read it from accounts.azuredatabricks.net, or provoke it out of
  the API, which names the account in its error.
- **The on-behalf-of token toggle**, above, if a PAT is ever wanted.

### The VM quota trap

The subscription has **zero cores of `standardDDSv5Family` quota in East US 2**,
and the whole-region ceiling is **10 cores**. `Standard_D4ds_v5` is Databricks'
own default node type and comes from that family, so a cluster that asks for it
fails several minutes into the run with `QuotaExceeded` — long after, and far
from, the place the choice was made. Every entry in
`var.databricks_node_type_allowlist` is a 4-vCPU type from a family this
subscription actually has quota in, verified against `az vm list-usage -l
eastus2`. Check before adding one.

### Databricks, verified

Against workspace `dbw-chip-chat`, 2026-08-26.

| | |
| --- | --- |
| Workspace | Premium, East US 2. Unity Catalog metastore `metastore_azure_eastus2` **auto-assigned at creation** — no manual assignment was needed |
| Idempotence | `terraform plan` after apply: **No changes.** |
| Smoke job | Run `703497916585597`: **SUCCESS**. Wrote a three-row Delta table to `abfss://raw@…/_smoke/`, read it back, asserted the count, deleted it |
| **Cluster terminated on its own** | Yes — `0826-065118-n104vpq8`, termination reason `JOB_FINISHED`, `num_workers` 0, node `Standard_F4ads_v7`, under the job policy. Non-terminated clusters in the workspace afterwards: **0** |
| Timing | **320.0 s total: 292.0 s provisioning, 27.0 s of actual work.** Cluster start dominates a job this small by more than 10×, which is the number to remember when sizing anything later |
| Credentials | Key Vault holds `databricks-host` and nothing else. There is no `databricks-token`, on purpose |

#### The policies, probed

Each of these is an actual API call made against the live workspace, not a
reading of the policy JSON:

| Attempt | Result |
| --- | --- |
| All-purpose cluster under the job policy | **Rejected** — "the value must be job (is all-purpose)" |
| Two workers under the job policy | **Rejected** (same check — `/clusters/create` only makes all-purpose clusters) |
| Autoscale 1→8 under the job policy | **Rejected** (same) |
| 480-minute autotermination under the interactive policy | **Rejected** — "the value must be 10 (is 480)" |
| `Standard_E64ds_v5` under the interactive policy | **Rejected** — node type and driver node type both outside the allowlist |
| **Four workers under the interactive policy** | **Accepted, and silently clamped** to `num_workers: 0`, `autotermination_minutes: 10`, `cluster_profile: singleNode`, `ResourceClass: SingleNode` |

The last row is worth reading carefully, because it is the one that looks like a
failure and is not. Databricks `fixed` policy attributes have two behaviours and
the platform picks: some reject a conflicting value, some overwrite it. Asking
for four workers does not get you four workers — it gets you a single-node
cluster with a ten-minute timeout. The outcome is the same either way, which is
the property that matters; the probe cluster was deleted before it finished
provisioning.

#### DBU consumption: measured in part, and here is the missing half

The run's billable cluster time is **320.0 seconds (0.0889 hours)**, and the
rates are known:

| | |
| --- | --- |
| VM, `Standard_F4ads_v7` | $0.343/hour → **$0.0305** for this run |
| DBU rate, Premium Jobs Compute | $0.30/DBU-hour |
| DBU **quantity** | **not measured** |

The quantity is the gap and it is worth being exact about why, rather than
substituting a plausible number. Databricks does not publish DBUs-per-node
through any API reachable from the workspace: it is not in
`clusters/list-node-types`, and the Azure pricing calculator's API carries
warehouse sizes but no instance-to-DBU mapping. The authoritative source is
`system.billing.usage`, and querying it fails with `TABLE_OR_VIEW_NOT_FOUND`
because the `billing` system schema has to be enabled by a **Databricks account
admin** — the same class of account-console action as the on-behalf-of token
toggle, and the workspace admin is not automatically one. `az consumption usage
list` will also carry the DBU meter once Azure's billing pipeline catches up,
which is hours rather than minutes.

So: the run cost about three cents of VM plus an unmeasured DBU component that,
at $0.30/DBU-hour over 0.089 hours, cannot exceed a few cents. Read the exact
figure once billing lands:

```bash
az consumption usage list --start-date 2026-08-26 --end-date 2026-08-27 \
  --query "[?contains(meterCategory,'Databricks')].{meter:meterName,qty:usageQuantity,cost:pretaxCost}" -o table
```

Tracked as a bead so the number gets written down rather than remembered.


## Unity Catalog

Issue #32. The governance layer, created before there were any tables in it —
ownership and grants are cheap to set on an empty catalog and tedious to
retrofit onto a populated one, because everything made in between inherits
whatever was true at the time.

`docs/lakehouse-catalog.md` is the full write-up. The short version:

| | |
| --- | --- |
| Catalog | `chip_chat`, managed storage at `abfss://lakehouse@…/_catalog` |
| Schemas | Six — `{bronze,silver,gold}_{harvested,synthetic}` — each with its own managed root at `abfss://lakehouse@…/schemas/<name>` and `properties = {layer, stream}` |
| Writer | `chip-chat-jobs`: `USE_CATALOG`; per schema `USE_SCHEMA`, `SELECT`, `MODIFY`, `CREATE_TABLE`, `CREATE_MATERIALIZED_VIEW`, `REFRESH` |
| Reader | `chip-chat-readonly`: `USE_CATALOG`, `USE_SCHEMA`, `SELECT`, and `READ_FILES` on `chip-chat-raw`. Nothing else |
| Ambient access | **None.** `account users` has no grant, the app tier has no grant, and `databricks_grants` is authoritative — a grant added in the UI is removed by the next apply |
| Verification | Two manual-trigger jobs: `chip-chat-uc-lineage` and `chip-chat-uc-readonly-denied` |

**Six schemas and not three.** Unity Catalog has three levels, so the issue's
"two streams kept visibly separate within `bronze`/`silver`/`gold`" could only
have been a table-naming convention — which is invisible on the day this ships,
because there are no tables. As a schema suffix the boundary is visible in an
empty catalogue, it is *grantable*, and it is a queryable property of the object.
The header of `databricks_catalog.tf` carries the argument in full.

### Two things Unity Catalog will not let a workspace-scoped stack do

> ⚠️ **A Unity Catalog owner must be an account-level principal.** `owner =
> "admins"` — the workspace admin group — fails with `cannot create catalog:
> Could not find principal with name admins`, which reads like a typo and is
> not. `admins` and `users` are workspace-local; UC resolves principals against
> the *account*. An account group needs a provider pointed at
> `accounts.azuredatabricks.net` and an account admin to run it, so the catalog
> is owned by whoever applied. `var.databricks_catalog_owner` is the seam.

> ⚠️ **`system.access` is not enabled, so lineage cannot be read from system
> tables.** The `system` catalog here holds only `ai` and `information_schema`,
> and enabling `system.access` — where `table_lineage` lives — is an
> account-admin action with no workspace API. This is the third such wall, after
> the `billing` schema and on-behalf-of tokens. The lineage probe uses
> `POST /api/2.0/lineage-tracking/table-lineage` instead, which is
> workspace-level and needs nothing turned on.

### Unity Catalog, verified

Against workspace `dbw-chip-chat`, 2026-08-26. Both jobs assert their own result
and fail the run otherwise, so "SUCCESS" below is the assertion passing rather
than the notebook finishing.

| | |
| --- | --- |
| Catalog and schemas | `chip_chat` + six, created by `terraform apply`. `databricks schemas list chip_chat` shows all six with their comments and `properties = {layer, stream}` |
| Managed storage | Catalog at `lakehouse/_catalog`; each schema at `lakehouse/schemas/<name>` — Unity Catalog accepted the sibling roots without complaint |
| **Lineage** | `chip-chat-uc-lineage`, run `1113423536362313`: **SUCCESS** |
| **Refusal** | `chip-chat-uc-readonly-denied`, run `420068911637398`: **SUCCESS** — the `SELECT` returned rows and all five writes were refused |
| Cluster start | **4.8 min** cold, **~1.2 min** once the runtime image is cached on the host |

The lineage the platform recorded, read back through the API:

```
abfss://raw@stchipchat….dfs.core.windows.net/_lineage_probe/<run>/menu.json
   [securable_type EXTERNAL_LOCATION, securable_name chip-chat-raw]
 → chip_chat.bronze_harvested.lineage_probe
 → chip_chat.silver_harvested.lineage_probe
 → chip_chat.gold_harvested.lineage_probe
```

The file really does appear as an upstream of the bronze table, which is the
half of "a raw file through to a gold mart" that table-to-table lineage would
not have shown.

### When a run sits at "Setting up 1 nodes" for twenty minutes

The run status says `RUNNING`, the cluster says `PENDING`, and the VM in
`rg-chip-chat-databricks-managed` says `PowerState/running`. Everything looks
like slow provisioning and it is not necessarily that. Read the cluster's own
event log, which is the only place the reason appears:

```bash
databricks clusters events <cluster-id> -o json | jq '.events[] | select(.type=="ADD_NODES_FAILED")'
```

On 2026-08-26 that returned `SPARK_IMAGE_DOWNLOAD_FAILURE` — the node had booted
and then spent twenty-five minutes failing to pull the Databricks Runtime image
from `arprodeastus2a12.blob.core.windows.net` ("Timed out with exception after
24023 attempts"), for the same `17.3.x-scala2.13` runtime that had downloaded
fine hours earlier. It is a transient platform fault, not a configuration one:
cancel the run and start it again, and the fresh node pulls the image normally.
Worth knowing because the symptom is indistinguishable from the *node-type*
capacity stall the inventory records, and the fix for that one — changing
`var.databricks_node_type` — would have been a change to the wrong thing.

### Verifying it

```bash
cd infra/terraform
databricks jobs run-now $(terraform output -raw databricks_lineage_job_id)
databricks jobs run-now $(terraform output -raw databricks_readonly_job_id)
```

In that order: the second reads what the first writes. Both run on single-node
job compute under `chip-chat-job-single-node`, so they inherit #31's cost
guardrail and cannot outlive themselves. Neither is scheduled.

`chip-chat-uc-lineage` writes one JSON document into ADLS with `dbutils.fs.put`
— not with Spark, so the only lineage edge touching that path is the read — then
carries it through bronze, silver and gold and asserts that Unity Catalog
recorded the chain. It leaves the three `lineage_probe` tables behind, because
lineage is a property of objects that exist; dropping them would delete the
evidence. Lineage is recorded asynchronously, so the notebook polls for up to
five minutes rather than treating an early answer as a failure.

`chip-chat-uc-readonly-denied` runs as the read-only principal. It reads first —
a refusal proves nothing if the principal has no access at all — then attempts
five writes and requires every one to be refused, including a file write to the
landing zone, because the catalog is not the only way out of a workspace. It
also fails if a write is refused for a reason that is *not* a permission error,
so a syntax mistake cannot make it pass.

---

## Bronze ingestion

Issue #33. One Lakeflow Spark Declarative Pipeline, `chip-chat-bronze-ingest`,
carrying both streams out of the ADLS landing zone and into `bronze_harvested`
and `bronze_synthetic` — ten streaming tables and a quarantine view per stream.

`docs/bronze-ingestion.md` is the full write-up, including the five things that
did not work the way the documentation implies. The short version:

| | |
| --- | --- |
| Pipeline | `chip-chat-bronze-ingest`, triggered (`continuous = false`), `development = false`, no schedule, single-node under `chip-chat-pipeline-single-node` |
| Source | `databricks/notebooks/bronze_ingest.py` — a loop over `chip_chat.databricks.bronze.SOURCES` |
| Packaging | `bronze.py` and `catalog.py` are uploaded as **workspace files** beside the notebook and imported off `sys.path`; both are stdlib-only so that this works, and `make ci` asserts they agree with the harvest, the generator, the Terraform and the notebook |
| Idempotence | Auto Loader's per-table file ledger under `abfss://lakehouse@…/_autoloader/<schema>/<table>`. `cloudFiles.allowOverwrites` is deliberately off |
| Quarantine | `_rescued_data IS NOT NULL OR <every identity column> IS NULL`, surfaced as `_quarantined` and as a view per stream. Nothing is dropped |
| Verification | `chip-chat-bronze-verify`, manual trigger, asserts its own result |

### Bronze, verified

Against `dbw-chip-chat`, 2026-08-26. The landing zone was seeded from the
committed fixtures through the real writers — a live harvest would have made paid
Document Intelligence calls #22 has already made once — and the population is the
full 500-customer one.

| | |
| --- | --- |
| Cold start | Pipeline update `ff4e1703` with `--full-refresh`: **COMPLETED** in 333 s, 289 s of it waiting for the VM |
| Re-run | Update `11f2087d` over an unchanged landing zone: **COMPLETED**, no count changed |
| Malformed input | Update `4792873c`, two deliberately malformed documents seeded: **COMPLETED** |
| **Assertions** | `chip-chat-bronze-verify` run `416167014729058`: **SUCCESS** — 10 tables, both streams, `COUNT(*) = COUNT(DISTINCT identity)` everywhere, 2 rows quarantined |

Row counts: 84 `raw_documents` (82 harvested plus the 2 malformed, which landed
flagged rather than being dropped), 79 `raw_bodies`, 1 `document_analyses`, and
on the synthetic side 500 / 18,898 / 48,767 / 32,234 / 28 / 7 / 1 — every one the
generator's own number, to the row.

### Three ways this failed first

> ⚠️ **A pipeline's `run_as` principal needs `CAN_USE` on the pipeline policy.**
> The jobs principal had it on the *job* policy from #31 and nothing implied it
> here. `terraform apply` reports no drift; the update fails two seconds in with
> `PERMISSION_DENIED: You are not authorized to access this cluster policy`,
> which reads like the policy is broken.

> ⚠️ **A `%md` cell that also holds code runs none of the code.** Databricks
> renders everything below `# MAGIC %md` in the same cell. The pipeline failed
> with `[NO_TABLES_IN_PIPELINE]`, which reads like the decorators are wrong.
> `databricks/tests/test_bronze.py` now asserts no markdown cell holds code.

> ⚠️ **`pathGlobFilter`, not `cloudFiles.pathGlobFilter`.** It is a generic
> file-source option, and Auto Loader validates its own namespace: the prefixed
> spelling is accepted at plan time and refused at stream start with
> `CF_UNKNOWN_OPTION_KEYS_ERROR` naming the key lower-cased.

`docs/bronze-ingestion.md` §3 has the other two, both about what the rescued data
column does and does not catch.

### Verifying it

```bash
cd infra/terraform
databricks pipelines start-update $(terraform output -raw databricks_bronze_pipeline_id)
databricks jobs run-now $(terraform output -raw databricks_bronze_verify_job_id)
```

In that order: the second reads what the first writes. The verify job reads and
never writes, runs on the same single-node job compute as #31's and #32's, and
raises rather than returns if any claim fails — so SUCCESS is the assertion
passing. It returns its row counts as the run's notebook output, which is how the
numbers above were quoted without opening the workspace.

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
| Databricks workspace | `dbw-chip-chat` (Premium) — URL is a `terraform output`, and the Key Vault secret `databricks-host` |
| Databricks managed group | `rg-chip-chat-databricks-managed` — owned by the Databricks RP, **holds the NAT gateway that bills whether or not anything runs** |
| Databricks access connector | `dbac-chip-chat` |
| Databricks account id | `43b2ef58-9ed2-4bb9-9962-8b20eb5cd8b4` — the Databricks account, not the Azure subscription |
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

Every secret the app tier reads goes here: Snowflake credentials, the Arize API key,
Foundry keys, and the ops API function key. **Databricks is the exception, and
deliberately so** — the app authenticates there with the `id-chip-chat-app` managed
identity, so there is no Databricks secret to store. Only the non-secret
`databricks-host` is written. See [Databricks](#credentials-there-are-none-and-that-is-the-design). No secret in source, no
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

Issue #31 added **`Microsoft.Databricks`**, which was also `NotRegistered`. Same
rule — subscription-level, outlives any stack, so it is not in the Terraform:

```bash
az provider register -n Microsoft.Databricks
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

- **DBU quantity for a pipeline run** — the run is measured and the rates are
  known, but the DBUs-per-node figure needs the `billing` system schema, which a
  Databricks *account* admin has to enable. See
  [DBU consumption](#dbu-consumption-measured-in-part-and-here-is-the-missing-half).
- **Databricks on-behalf-of tokens** — the feature is disabled for this account
  and needs an account-console toggle. Nothing depends on it; the app tier uses
  its managed identity.
- **`no_public_ip = false`** — the lever that would remove the workspace's
  $36.50/month NAT gateway. It needs the workspace replaced and it is a security
  downgrade, so it has not been tried.

- **Azure AI Search** — cannot be provisioned in East US 2 at all right now. See
  [Known issue](#known-issue-ai-search-will-not-provision) and bead `cc-3wo`.
- **Model deployments** — `var.model_deployments` is empty by design; issue #8
  chooses the models and confirms quota.
- **The budget's test notification** — see above; it sends real mail and is left
  for the account owner.
