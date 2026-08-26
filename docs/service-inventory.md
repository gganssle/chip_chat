# Service inventory — verified

**Issue:** [#2](https://github.com/gganssle/chip_chat/issues/2) (bead `cc-2tm`) · **Phase 0**
**All entries checked:** 25 August 2026 · **Prices:** USD, East US 2, retail (no reservations, no EA discount)

Every planning document in this repo closes with the same warning: service names
and tiers move faster than the plan does. This is that verification pass. Each row
carries the source it came from and the date it was checked. Where the source is a
Microsoft Learn page, the `ms.date` of the page is given too, because Learn pages
carry their own staleness marker and it is more honest than "I looked at it today".

Prices marked *(retail API)* come from the
[Azure Retail Prices API](https://prices.azure.com/api/retail/prices) rather than the
marketing pricing pages, which now render their numbers client-side and show `$-`
to anything that isn't a browser. The API is the same data the calculator reads.

**Read [What changed versus the plan](#what-changed-versus-the-plan) first.** That
section is the actual deliverable; the tables are the evidence behind it.

---

## 1. The headline: Azure AI Foundry is now Microsoft Foundry

This rename touches every Azure row below, so it goes first.

| Was | Is now |
| --- | --- |
| Azure AI Foundry | **Microsoft Foundry** |
| Azure AI Foundry Agent Service | **Microsoft Foundry Agent Service** ("Foundry Agent Service") |
| Azure AI services (the umbrella) | **Foundry Tools** — this is the `serviceName` you now see on the Azure bill for Content Safety and Document Intelligence |
| `learn.microsoft.com/azure/ai-foundry/...` | `learn.microsoft.com/azure/foundry/...` — the old paths still resolve as `/azure/foundry-classic/` or `?view=foundry-classic` |
| — | **Foundry IQ** — new managed knowledge layer built on Azure AI Search; the AI Search *pricing page* is now titled "Foundry IQ pricing" |

The individual services (AI Search, Content Safety, Document Intelligence) keep
their own names. It is the umbrella and the portal that moved.

Source: [Microsoft Foundry docs: What's new for July 2026](https://learn.microsoft.com/en-us/azure/foundry/whats-new-foundry) (ms.date 2026-08-12);
[What is Microsoft Foundry Agent Service?](https://learn.microsoft.com/en-us/azure/foundry/agents/overview) (ms.date 2026-08-13);
retail API `serviceName` field. Checked 2026-08-25.

---

## 2. Azure

### 2.1 Microsoft Foundry Agent Service

| Item | Current answer | Source | Checked |
| --- | --- | --- | --- |
| Product name | Microsoft Foundry Agent Service. **GA.** | [overview](https://learn.microsoft.com/en-us/azure/foundry/agents/overview) (ms.date 2026-08-13) | 2026-08-25 |
| Is "Agent Service" still what it's called | Yes — but it now has **two agent types**: *prompt agents* (config only, Foundry runs it) and *hosted agents* (your container, Foundry runs it). There is also a third path, the **Responses API**, for an "ephemeral agent" defined entirely in your own code with no Foundry resource. | same | 2026-08-25 |
| Threads / tool calling | Still the model. Limits: 128 tools per agent, 100,000 messages per thread, 1,000 agent revisions. | [limits-quotas-regions](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions) (ms.date 2026-08-20) | 2026-08-25 |
| Agents region availability | 30 regions. **East US 2** and **East US** both fully supported. | same | 2026-08-25 |
| Tools by region | Not uniform. East US 2 supports every tool in the matrix including Computer Use; East US lacks Computer Use; `file search` is missing in Italy North and Brazil South. Azure AI Search as an agent tool: available in all 30. | same | 2026-08-25 |
| Model deployment options | Serverless API deployments in three categories — **standard** (pay-per-token), **provisioned** (reserved), **batch** — each available as **global**, **data zone**, or **regional**. Global Standard is the default choice for a PoC. | [deployment-types](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types) | 2026-08-25 |
| Models with vision + tool calling | `gpt-5.x` family, `gpt-4.1`, `gpt-4o` all support Azure AI Search, MCP, OpenAPI, Functions and file search per the model/tool matrix. `gpt-5` requires [registration](https://aka.ms/openai/gpt-5/2025-08-07). | [limits-quotas-regions](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions) | 2026-08-25 |
| Where agent state lives | **Basic setup** → Microsoft-managed storage. **Standard setup** → your own Blob Storage + Azure AI Search + **Cosmos DB** (threads/messages). Standard setup therefore adds a Cosmos DB bill the plan never budgeted. | same | 2026-08-25 |

**Tracing / OTel export — the answer Phase 7 needs.** Foundry's own tracing is
OpenTelemetry with the gen-ai semantic conventions and lands in **Application
Insights**; that path is not configurable to a third party. But a *hosted agent*
reads the standard OTel environment variables and will fan out to any OTLP endpoint
in parallel with App Insights:

```yaml
environment_variables:
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: https://<provider-otlp-endpoint>          # not secret
  - name: OTEL_EXPORTER_OTLP_PROTOCOL
    value: http/protobuf
  - name: OTEL_EXPORTER_OTLP_HEADERS
    value: ${{connections.otel-secrets.credentials.otlp_headers}}   # secret via a CustomKeys connection
```

Caveats that will bite: env vars are **immutable per agent version** (changing the
exporter means a new version); `service.name` is forced to the agent's name and
`OTEL_SERVICE_NAME` is ignored; and to send *only* to OTLP you must disable
monitoring at the project level, because `APPLICATIONINSIGHTS_CONNECTION_STRING`
is platform-injected and cannot be overridden.

Source: [Export hosted agent telemetry by using OpenTelemetry](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/configure-hosted-agent-telemetry) (ms.date 2026-07-01); [Agent tracing overview](https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-agent-concept) (ms.date 2026-07-31). Checked 2026-08-25.

### 2.2 Azure AI Search — including the reranker question

| Item | Current answer | Price | Source | Checked |
| --- | --- | --- | --- | --- |
| Product name | Azure AI Search (unchanged). Now also underpins **Foundry IQ**; the pricing page is titled "Foundry IQ pricing". | — | [pricing](https://azure.microsoft.com/en-us/pricing/details/search/) | 2026-08-25 |
| Free tier limits | 1 service per subscription, **50 MB** storage, **3 indexes**, 3 indexers, 3 datasources, 3 skillsets, 3 synonym maps. No fixed partitions/replicas, no SLA. May be **deleted after extended inactivity**. | $0 | [service limits](https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity) (ms.date 2026-08-17) | 2026-08-25 |
| Free tier indexer limits | Max **10,000 docs per invocation**; max running time **3 min** (blob) / **1 min** (other), or **3–10 min** with a skillset; invocation once per 180 s; **20 free enrichment transactions per indexer per day** for AI indexing that calls Foundry Tools. | $0 | same | 2026-08-25 |
| Free tier: managed identity | **Not available.** Free tier also has no customer-managed keys, no IP firewall, no private endpoint, no availability zones. | — | [choose a tier](https://learn.microsoft.com/en-us/azure/search/search-sku-tier) (ms.date 2026-08-04) | 2026-08-25 |
| **Semantic ranker on Free tier** | **Yes.** The feature-availability table reads: *"Semantic ranker — Runs on the Free tier but not recommended for large workloads."* | see below | [choose a tier](https://learn.microsoft.com/en-us/azure/search/search-sku-tier) (ms.date 2026-08-04) | 2026-08-25 |
| Semantic ranker billing plans | Two plans. **Free (default)** — a monthly free request allowance, then requests return a billing error; *"Available on all pricing tiers."* **Standard** — pay-as-you-go past the allowance; *"Requires the Basic tier or higher."* | — | [enable/disable semantic ranker billing](https://learn.microsoft.com/en-us/azure/search/semantic-how-to-enable-disable) (ms.date 2026-06-16) | 2026-08-25 |
| Free semantic allowance | **First 1,000 requests per month free.** | $0 | [pricing](https://azure.microsoft.com/en-us/pricing/details/search/) | 2026-08-25 |
| Semantic ranker standard price | **$1.00 per 1,000 queries**; overage meter **$2.00 per 1,000**. | $1.00/1K | retail API, meters `Semantic Ranker queries` / `Semantic Ranker Overage Queries` | 2026-08-25 |
| Basic tier | 15 GB per partition, 3 partitions × 3 replicas (services created after 2024-04-03), 15 indexes, 5 GB vector quota per partition. | **$0.101/hour ≈ $73.73/month** (730 h) *(retail API)* | [service limits](https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity) | 2026-08-25 |
| Semantic ranker throttling | Concurrency table starts at Basic (2 concurrent requests + queue of 4 per search unit). **The Free tier has no row in that table** — free-tier throughput is unpublished and shared. | — | [service limits](https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity) | 2026-08-25 |
| New: Serverless Developer tier | Consumption pricing (Compute Units/hr + GB/month), **public preview**, no SLA, 30 indexes, 1 GB max index, 300 MB vector per index. **Billing starts 2026-09-13.** Cannot migrate to or from other tiers. | preview | [choose a pricing model](https://learn.microsoft.com/en-us/azure/search/search-sku-tier) (ms.date 2026-08-04) | 2026-08-25 |

> **This resolves [issue #10](https://github.com/gganssle/chip_chat/issues/10) (RFC-001 §13 Q3).**
> See [The reranker decision](#the-reranker-decision-issue-10) below for the recommendation.

### 2.3 Azure Container Apps

| Item | Current answer | Price | Source | Checked |
| --- | --- | --- | --- | --- |
| Scale to zero | Supported on the Consumption ("Standard") plan with `minReplicas: 0`. Two traps: **(a)** with ingress disabled and no `minReplicas` and no custom scale rule, the app scales to zero *and has no way to start back up*; **(b)** the KEDA **CPU and memory scalers cannot scale to zero by design** — an HTTP or event-based scale rule is required. | — | [Scaling in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/scale-app) | 2026-08-25 |
| Cold start | Docs say the platform "automatically optimizes cold-start behaviour" and recommend a minimum of always-ready replicas if cold starts matter. **No numeric cold-start SLO is published.** The plan's "a couple of seconds" is an estimate, not a documented figure — measure it. | — | same | 2026-08-25 |
| Custom domain + managed cert | Supported and **free**, auto-renewing. Requires: HTTP ingress enabled; publicly reachable from the DigiCert validation IPs; **A record** for an apex domain / **CNAME straight to the app FQDN** for a subdomain (an intermediate CNAME — Cloudflare, Traffic Manager — **blocks issuance and renewal**); and if a CAA record exists on the root, an explicit `0 issue digicert.com`. | $0 | [custom domains + managed certificates](https://learn.microsoft.com/en-us/azure/container-apps/custom-domains-managed-certificates) (ms.date 2026-01-28) | 2026-08-25 |
| Consumption pricing | vCPU active **$0.000024/s**, vCPU idle **$0.000003/s**, memory active/idle **$0.000003/GiB-s**, requests **$0.40/million**. | *(retail API)* | retail API, `Azure Container Apps` / SKU `Standard` | 2026-08-25 |
| Idle billing | Note the separate *idle* meters: a replica kept alive at `minReplicas: 1` still bills, at roughly 1/8 the active vCPU rate. Scale-to-zero remains the right default. | — | same | 2026-08-25 |

### 2.4 Azure AI Content Safety

| Item | Current answer | Price | Source | Checked |
| --- | --- | --- | --- | --- |
| Product name | **Azure AI Content Safety** — unchanged. Billed under `serviceName` **Foundry Tools**; the pricing page is titled *"Content Safety in Foundry Control Plane"*. | — | [overview](https://learn.microsoft.com/en-us/azure/ai-services/content-safety/overview) (ms.date 2025-09-16, updated 2026-06-05) | 2026-08-25 |
| Tiers | **F0** (free) and **S0** (standard). | — | same | 2026-08-25 |
| F0 free quota | **5,000 text records/month** and **5,000 images/month**, at **5 RPS**. | $0 | [pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/content-safety/) | 2026-08-25 |
| S0 price | Text **$0.375 per 1,000 records**; images **$0.75 per 1,000**. Rate limit 1,000 requests / 10 s. | *(retail API)* | retail API, product `Content Safety` | 2026-08-25 |
| Text APIs available | Analyze text (sexual/violence/hate/self-harm, multi-severity), **Prompt Shields** (jailbreak + cross-prompt injection), Protected material text, Groundedness detection (preview), Task adherence (preview), Custom categories standard/rapid (preview). | — | [overview](https://learn.microsoft.com/en-us/azure/ai-services/content-safety/overview) | 2026-08-25 |
| Image APIs available | Analyze image (max **4 MB**, 50×50 to 7200×7200 px, JPEG/PNG/GIF/BMP/TIFF/WEBP). Multimodal (text+image) is **preview**. | — | same | 2026-08-25 |
| Region gotcha | In **East US 2**: image ✅, prompt shields ✅, groundedness ✅, protected material ✅ — but **multimodal ❌** and **custom categories (standard) ❌**. Both of those need **East US** or West Europe. | — | same | 2026-08-25 |

### 2.5 Azure Document Intelligence

| Item | Current answer | Price | Source | Checked |
| --- | --- | --- | --- | --- |
| Product name | **Azure Document Intelligence** — unchanged since the Form Recognizer rename. Billed under `serviceName` Foundry Tools. | — | retail API, product `Azure Document Intelligence` | 2026-08-25 |
| Tiers | **F0** (free) and **S0** (pay-as-you-go). Commitment tiers exist but are irrelevant at this volume. | — | same | 2026-08-25 |
| S0 prices | **Read $1.50 per 1,000 pages** (drops to $0.60/1K at the higher volume band) · **Layout $10/1K** · **Prebuilt $10/1K** · **Custom $30/1K** · Add-on features $6/1K · Training $3/hour. | *(retail API)* | same | 2026-08-25 |
| Relevance to Phase 1 | A handful of nutrition PDFs is a rounding error even on S0. F0 is fine and its rate limit is the only thing to watch. | — | — | 2026-08-25 |
| API version in use | `2024-11-30`, model `prebuilt-layout`. Called over its REST API with an Entra ID token for `https://cognitiveservices.azure.com/.default`; no key is read anywhere in this repository. | — | verified against the live account `di-chip-chat-4cy39i` | 2026-08-26 |
| **Correction to the plan** | The system design says *"Azure Document Intelligence handles any PDF nutrition sheets."* There are none. Chipotle published no PDF on any page this project harvests as of 2026-08-26, so issue #22's dataset is empty by correct operation rather than by failure. The reader was checked against the live account anyway. | — | [chipotle-pdf-spot-check.md](chipotle-pdf-spot-check.md) | 2026-08-26 |

### 2.6 Blob Storage / ADLS Gen2 — the 24-hour image expiry

| Item | Current answer | Source | Checked |
| --- | --- | --- | --- |
| Lifecycle rules supported | Yes, for block and append blobs in general-purpose v2, premium block blob, and Blob Storage accounts — which includes ADLS Gen2 (hierarchical namespace) accounts. Policies are **free**; delete operations are free. | [lifecycle management overview](https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-overview) (ms.date 2025-09-15, updated 2026-08-25) | 2026-08-25 |
| Conditions available | Creation Time, Last Modified Time, Last Accessed Time (needs access-time tracking on). Minimum granularity is **days** — `daysAfterCreationGreaterThan: 1` is the tightest expiry a lifecycle rule can express. | same | 2026-08-25 |
| **The catch** | *"When you add or edit the rules of a lifecycle policy, it can take up to 24 hours for changes to go into effect and for the first execution to start."* The engine then runs periodically, not continuously. So a `daysAfterCreationGreaterThan: 1` rule means **deleted roughly 24–48 hours after upload**, not exactly 24. | same | 2026-08-25 |
| Soft delete interaction | If blob soft-delete is enabled on the account, the lifecycle delete puts the blob into a **soft-deleted state for the full soft-delete retention** rather than actually removing it. For a "we do not keep strangers' photos" claim, soft delete must be off on that container's account. | same | 2026-08-25 |
| Other limits | 10 prefixes and 10 blob-index-tag conditions per rule. Policies must be read/written in full — no partial updates. Immutable containers ignore the delete action. | same | 2026-08-25 |

---

## 3. Databricks on Azure

| Item | Current answer | Price | Source | Checked |
| --- | --- | --- | --- | --- |
| Provisionable via Terraform? | **Yes**, and it takes two providers. `hashicorp/azurerm` → **`azurerm_databricks_workspace`** creates the workspace (`name`, `resource_group_name`, `location`, `sku`, `managed_resource_group_name`). `databricks/databricks` → everything inside it (catalogs, schemas, jobs, pipelines, compute policies), authenticated against the workspace URL. | — | [azurerm_databricks_workspace](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/databricks_workspace) · [Deploy a workspace using Terraform](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/terraform/azure-workspace) | 2026-08-25 |
| Tier to use | **Premium.** Unity Catalog requires it. | — | [Unity Catalog](https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/) | 2026-08-25 |
| ⚠️ Standard tier | **Azure Databricks Standard tier retires 1 October 2026** — inside this project's window. Do not create a Standard workspace. | — | [Azure Databricks pricing](https://azure.microsoft.com/en-us/pricing/details/databricks/) | 2026-08-25 |
| Unity Catalog availability | **Automatically enabled** for every workspace created after 2023-11-09; a metastore is created and assigned for you. Enable *workspace auto-assignment* on the metastore to keep that true for new workspaces in the region. | — | [Enable a workspace for Unity Catalog](https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/enable-workspaces) | 2026-08-25 |
| ⚠️ UC-only workspaces | From **30 September 2026**, all new workspaces are provisioned UC-only, without access to legacy features. An account admin must set an auto-assign metastore per region or assign one manually at provision time. | — | [Migrate to UC-only workspaces](https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/upgrade/uc-only-migration) | 2026-08-25 |
| Declarative pipelines | **Delta Live Tables is now "Lakeflow pipelines" / "Lakeflow Spark Declarative Pipelines."** Existing DLT code runs unchanged, but the Python API moved: `import dlt` → `from pyspark import pipelines as dp`; `@dlt` → `@dp`; `@view` → `@temporary_view`; new `@materialized_view`. SKU names and event-log schemas still say `DLT`. | — | [What happened to Delta Live Tables?](https://docs.databricks.com/aws/en/ldp/where-is-dlt) (updated 2026-07-10) · [Lakeflow pipelines release notes 2026](https://learn.microsoft.com/en-us/azure/databricks/release-notes/dlt/2026) | 2026-08-25 |
| Compute policies for cost control | **Policy families** are the prebuilt templates. A policy can set a default/maximum `autotermination_minutes`, cap `num_workers`, restrict instance types, forbid pools, require autoscaling, and enforce Photon. | — | [Compute policy reference](https://learn.microsoft.com/en-us/azure/databricks/admin/clusters/policy-definition) · [Default policies and policy families](https://learn.microsoft.com/en-us/azure/databricks/admin/clusters/policy-families) | 2026-08-25 |
| ⚠️ Policy trap | Pipeline compute shuts itself down when idle, so **a policy that sets `autotermination_minutes` cannot be attached to pipeline compute — it errors.** Keep the cost policy for job/all-purpose clusters separate from the pipeline policy. | — | [Configure classic compute for pipelines](https://docs.databricks.com/aws/en/ldp/configure-compute) | 2026-08-25 |
| ⚠️ Policy trap, wider than recorded | **Job compute rejects `autotermination_minutes` too** — not just pipeline compute. Job creation fails with *"Automated clusters do not support autotermination"*. Both tear their cluster down when the run ends, so termination there is structural and the attribute is meaningless; the ten-minute number only has somewhere to apply on **all-purpose** compute. So it is three policies, not two. Found by hitting it (gh-31). | — | observed on `dbw-chip-chat` | 2026-08-26 |
| ⚠️ `fixed` is two behaviours | A `fixed` policy attribute sometimes **rejects** a conflicting value (`cluster_type`, `autotermination_minutes`, and the `node_type_id` allowlist all do) and sometimes **silently overwrites** it (`num_workers: 4` becomes `0`). Same outcome, different error surface — do not read "the request succeeded" as "the policy did not apply". | — | probed on `dbw-chip-chat` (gh-31) | 2026-08-26 |
| ⚠️ Policies do not bind SQL warehouses | A cluster policy binds clusters. A SQL warehouse is not a cluster, and new workspaces ship with a **Serverless Starter Warehouse** (Small, PRO, auto-stop 10 min). Serverless SQL is $0.70/DBU-hr and Small draws ~12 DBU/hr — **~$8/hour**, the priciest compute in a workspace and the one no policy reaches. Remove `databricks-sql-access` from the `users` group if that matters. | $0.70/DBU-hr | observed on `dbw-chip-chat` (gh-31) | 2026-08-26 |
| ⚠️ **A workspace bills ~$36.50/month idle** | Secure cluster connectivity puts a **NAT gateway** ($0.045/hr = $32.85/mo) and a **standard static public IP** ($0.005/hr = $3.65/mo) in the Databricks-managed resource group. They bill with zero clusters running and are not on the Databricks bill — they are on the Azure one. Roughly a quarter of the project's $150 budget, for an idle workspace. `no_public_ip = false` removes them at the cost of public IPs on cluster nodes and a workspace replacement. | $36.50/mo | Azure retail prices API, `eastus2` (gh-31) | 2026-08-26 |
| ⚠️ Two things need a Databricks **account** admin | A workspace admin is not automatically one. Enabling the `billing` system schema (so `system.billing.usage` exists at all) and enabling on-behalf-of PATs for service principals are both account-console actions with no workspace API. Binding a job's `run_as` to a service principal also needs the account-level `roles/servicePrincipal.user`. | — | observed on `dbw-chip-chat` (gh-31) | 2026-08-26 |
| ⚠️ A Unity Catalog owner must be an **account**-level principal | `owner = "admins"` — the workspace admin group — fails with *"cannot create catalog: Could not find principal with name admins"*, which reads like a typo. `admins` and `users` are **workspace-local** groups and are not Unity Catalog principals; UC resolves principals against the account. An account group needs a provider pointed at `accounts.azuredatabricks.net` and an account admin to create it, so a catalog created from a workspace-scoped stack is owned by a person. | — | observed on `dbw-chip-chat` (gh-32) | 2026-08-26 |
| ⚠️ `system.access` is not enabled, so **lineage cannot be read from system tables** | The `system` catalog on this metastore holds only `ai` and `information_schema`. Enabling `system.access` (which is where `table_lineage` and `column_lineage` live) is an *account*-admin action with no workspace API — the third such wall, after the billing schema and on-behalf-of tokens. Use `POST /api/2.0/lineage-tracking/table-lineage` instead: workspace-level, needs nothing turned on, and returns the same graph. Lineage is also recorded **asynchronously**, so a read immediately after the write can legitimately come back empty — poll rather than assert once. | — | observed on `dbw-chip-chat` (gh-32) | 2026-08-26 |
| Schema-level managed storage | A `databricks_schema` accepts a `storage_root` inside an external location, and it does not have to sit under the catalog's own managed root — Unity Catalog only rejects locations that **overlap**. Worth setting: without it every schema is laid out under one directory keyed by GUID, which is fine for the engine and useless to a human with a storage browser open. | — | applied on `dbw-chip-chat` (gh-32) | 2026-08-26 |
| ⚠️ The lineage API is a **GET**, not a POST | The documented shape for `/api/2.0/lineage-tracking/table-lineage` is a POST with a JSON body, and a POST answers `404 ENDPOINT_NOT_FOUND: No API found for 'POST /lineage-tracking/table-lineage'` — which reads like the feature is switched off. The same path answers **200 to a GET** with `table_name` and `include_entity_lineage` in the query string. The response carries `fileInfo` for an external-location source, so a raw file really does appear as an upstream of a bronze table. | — | observed on `dbw-chip-chat` (gh-32) | 2026-08-26 |
| ⚠️ A pipeline's `run_as` principal needs `CAN_USE` on the **pipeline** policy, and nothing implies it | The jobs service principal held `CAN_USE` on the *job* policy from gh-31. Creating a `databricks_pipeline` against the pipeline policy succeeds and `terraform apply` reports no drift; the **update** fails two seconds in with *"Failed to create a pipeline cluster: PERMISSION_DENIED: You are not authorized to access this cluster policy"*, which reads like the policy is broken. A cluster policy's ACL is authoritative for the policy it names, so it needs its own `databricks_permissions`. | — | observed on `dbw-chip-chat` (gh-33) | 2026-08-26 |
| ⚠️ A `%md` cell that also holds code runs **none** of the code | Databricks reads a cell beginning `# MAGIC %md` as one markdown block; Python below it in the same cell is rendered, not executed. Nothing errors. A pipeline whose table definitions sat under a markdown header failed with `[NO_TABLES_IN_PIPELINE]`, which reads like the decorators are wrong. `databricks/tests/test_bronze.py` asserts no markdown cell holds code. | — | observed on `dbw-chip-chat` (gh-33) | 2026-08-26 |
| ⚠️ Auto Loader: `pathGlobFilter`, **not** `cloudFiles.pathGlobFilter` | It is a generic file-source option rather than an Auto Loader one, and Auto Loader validates its own namespace. The prefixed spelling is accepted at plan time and refused at stream start with `CF_UNKNOWN_OPTION_KEYS_ERROR`, naming the key lower-cased — which reads like a typo in the value. | — | observed on `dbw-chip-chat` (gh-33) | 2026-08-26 |
| ⚠️ Auto Loader: `binaryFile` accepts **no** schema evolution mode but `none` | Its schema is four fixed columns, so there is nothing for a new column to arrive in — but the reader does not treat the setting as vacuous. `addNewColumns` fails the flow with `CF_UNSUPPORTED_SCHEMA_EVOLUTION_MODE`. | — | observed on `dbw-chip-chat` (gh-33) | 2026-08-26 |
| ⚠️ The rescued data column does **not** catch a document that failed to parse whole | A truncated JSON file read with `multiLine` produces a row of nulls and an **empty** `_rescued_data`, indistinguishable from a legitimately sparse record. A quarantine keyed on the rescued column alone therefore lets a corrupt document through as clean data. Test the row's identity as well: a row with nothing to name it by did not arrive. | — | observed on `dbw-chip-chat` (gh-33) | 2026-08-26 |
| Single-node clusters | `num_workers: 0` — driver acts as both master and worker. Still the right shape for this data volume. | — | [Compute policy reference](https://learn.microsoft.com/en-us/azure/databricks/admin/clusters/policy-definition) | 2026-08-25 |
| ⚠️ Node types: East US 2 will not start most of them | On this subscription, **`Standard_D4ds_v5` — Databricks' own default node type — has a quota of zero cores** (`standardDDSv5Family`), and `Standard_D4ds_v4`, `Standard_DS3_v2`, `Standard_F4s_v2`, `Standard_D4s_v3` and `Standard_E4ds_v4` are **not offered in East US 2 at all**. `Standard_D4ds_v4` additionally failed live with `CLOUD_PROVIDER_RESOURCE_STOCKOUT`. `Standard_F4ads_v7` ($0.343/hr, 4 vCPU / 16 GB) is the one 4-vCPU type with quota and no restrictions of any kind. Region ceiling is 10 cores. Neither failure appears at plan time: a quota miss fails minutes into the run, a capacity miss hangs until the job times out. | $0.343/hr | `az vm list-usage` / `az vm list-skus`, `eastus2` (gh-31) | 2026-08-26 |
| DBU prices (East US 2) | Premium **Jobs Compute $0.30/DBU-hr** · Jobs Compute Photon $0.30 · **All-purpose $0.55/DBU-hr** · Automated Serverless $0.45 · Interactive Serverless $0.95 · Serverless SQL $0.70. **Plus** the underlying VM cost. | *(retail API)* | retail API, `Azure Databricks` | 2026-08-25 |
| Free options | **Free trial:** Premium workspace, **up to $400 in credits, 14 days**, full platform, billed to your Azure subscription afterwards. **Free Edition:** forever-free but *one serverless workspace, no classic compute, non-commercial use, and Databricks reserves the right to train on your data.* | $0 | [Free trial vs Free Edition](https://learn.microsoft.com/en-us/azure/databricks/getting-started/free-trial-vs-free-edition) (ms.date 2026-06-23) | 2026-08-25 |

The all-purpose-versus-jobs gap is the design's stated cost trap and it is real:
$0.55 against $0.30 per DBU-hour, before VM cost, for compute that is easy to leave
running. The policy is worth writing on day one.

---

## 4. Snowflake

| Item | Current answer | Source | Checked |
| --- | --- | --- | --- |
| Trial terms | **30 days or $400 of credits, whichever comes first.** No credit card. Cloud platform, region and edition are chosen at signup and **cannot be changed afterwards**. At expiry the account is suspended; reactivating requires a card, which converts it to paid. | [Snowflake trial](https://www.snowflake.com/en/snowflake-trial/) · [Trial accounts](https://docs.snowflake.com/en/user-guide/admin-trial-account) | 2026-08-25 |
| Verdict on the plan | The design's "30-day clock and roughly $400 of credits" is **correct**. Deferring the trial to Phase 4 remains right, and the irreversible region choice makes it more important, not less. | — | 2026-08-25 |
| **Cortex Analyst region availability** | Natively available in nine regions only. **On Azure: `East US 2` and `West Europe`.** (AWS: us-east-1, us-west-2, eu-central-1, eu-west-1, ap-northeast-1, ap-southeast-2, and US East Commercial Gov.) Anywhere else requires **cross-region inference** to be enabled, which sends inference out of your region. | [Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst) | 2026-08-25 |
| Cortex Analyst access control | Requires the `SNOWFLAKE.CORTEX_USER` or `SNOWFLAKE.CORTEX_ANALYST_USER` database role, plus SELECT on the referenced tables and read on any stage holding a semantic model. | same | 2026-08-25 |
| **Semantic model format** | **Native semantic views are now the recommended form** — `CREATE SEMANTIC VIEW`, or the Snowsight wizard, with full RBAC, sharing, derived metrics, access modifiers and custom instructions. Legacy **YAML semantic model files on stages are supported only for backward compatibility.** | [Overview of semantic views](https://docs.snowflake.com/en/user-guide/views-semantic/overview) · [Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst) | 2026-08-25 |
| Cortex Analyst cost model | Billed by **number of messages processed**; only successful (HTTP 200) responses count. Token counts only matter when Cortex Analyst is driven through Cortex Agents. Warehouse compute for the generated SQL is billed separately as usual. | [Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst) | 2026-08-25 |
| Row access policies | Unchanged and still the right mechanism for binding a visitor to their demo id at the session. | [RFC-001 §serving](rfc-001.md) | 2026-08-25 |

---

## 5. Arize

| Item | Current answer | Price | Source | Checked |
| --- | --- | --- | --- | --- |
| AX on Azure | Arize is **generally available as an Azure Native Integration** — provision and run it from the Azure portal, with Azure SDK/CLI support, Entra SSO, and **unified Azure billing**. This is a stronger integration than the plain Marketplace SaaS listing the plan assumed. | — | [Arize AI now GA as part of Azure Native Integrations](https://arize.com/blog/arize-ai-now-generally-available-as-part-of-azure-native-integrations/) · [Arize · Azure](https://arize.com/partners/azure/) | 2026-08-25 |
| Marketplace listing | A `Arize AI` SaaS listing still exists on Microsoft Marketplace (publisher `arizeai1657829589668`). The listing page blocks automated fetches (HTTP 403), so **confirm the plan/price surface in a browser while you are in the portal.** This is the one row below that is not fully verified. | — | [marketplace.microsoft.com](https://marketplace.microsoft.com/en-us/product/saas/arizeai1657829589668.arize_ai?tab=overview) | 2026-08-25 |
| AX plans | **AX Free — $0**: 25,000 spans/month, 1 GB/month, **15-day retention**. **AX Pro — $50/month**: 50,000 spans/month, 10 GB/month, 30-day retention. **AX Enterprise** — custom. | $0 / $50 | [Arize pricing](https://arize.com/pricing/) | 2026-08-25 |
| Phoenix self-hosting | Alive and current. Docker Hub images `arizephoenix/phoenix:latest`, `:latest-nonroot`, and pinned `:version-X.X.X` (pin for anything real). Docker Compose and a Kubernetes Helm chart are both supported; auth options include OAuth2, LDAP and local accounts. Free to self-host with no feature gating, and it can run fully air-gapped. | $0 + container | [Phoenix self-hosting](https://arize.com/docs/phoenix/self-hosting) · [Arize-ai/phoenix](https://github.com/arize-ai/phoenix) | 2026-08-25 |
| Do both still speak OpenInference | **Yes.** OpenInference is the shared OTel-based semantic-convention layer for both, maintained at [Arize-ai/openinference](https://github.com/Arize-ai/openinference), with instrumentation packages per framework/SDK. Arize's Azure page goes further and describes OpenInference as what "Microsoft's open trust stack runs on". | — | [Arize-ai/openinference](https://github.com/Arize-ai/openinference) · [Arize · Azure](https://arize.com/partners/azure/) | 2026-08-25 |
| Python packages | `arize-phoenix` (the app), `arize-phoenix-otel` (the OTel/OTLP wiring), plus `openinference-instrumentation-*` per framework. | — | [PyPI: arize-phoenix](https://pypi.org/project/arize-phoenix/) · [PyPI: arize-phoenix-otel](https://pypi.org/project/arize-phoenix-otel/) | 2026-08-25 |

---

## What changed versus the plan

This is the section the ticket was actually filed for. Each item names the document
it contradicts and what to do about it.

### Naming — cosmetic but pervasive

1. **"Azure AI Foundry Agent Service" is now "Microsoft Foundry Agent Service."**
   *system-design.md* ("Brain: Azure AI Foundry Agent Service", Phase 7) and
   *rfc-001.md* both use the old name throughout. The service is the same and it is
   GA; only the branding moved. Worth a find-and-replace so the write-up in Phase 11
   doesn't read as two years old.

2. **"Azure AI services" is now billed as "Foundry Tools."** Anything that greps the
   Azure cost export by service name — the Phase 9 cost dashboard — must look for
   `Foundry Tools`, not `Cognitive Services`. Content Safety and Document
   Intelligence both bill under it.

3. **Documentation URLs moved** from `/azure/ai-foundry/` to `/azure/foundry/`. Any
   link in the planning docs pointing at the old path now lands on the *classic*
   view, which documents the previous generation of the product.

### Architecture — these change what gets built

4. **The agent has three shapes now, not one.** *rfc-001.md* describes "a single
   Foundry agent with eleven tools" as though there were one way to build it. There
   are three: a **prompt agent** (declarative, Foundry hosts it), a **hosted agent**
   (your container, Foundry hosts it), or a plain **Responses API** call from the
   FastAPI service with no Foundry agent resource at all. This is a real design
   decision that the RFC does not currently make, and it interacts with item 5.

5. **Only a *hosted agent* can export OTel to a third party.** The design's
   observability plane — "instrument once and fan out to both exporters", Phoenix
   locally then AX in Phase 8 — is confirmed possible, but only through
   `OTEL_EXPORTER_OTLP_*` environment variables on a **hosted agent**. A prompt
   agent's tracing goes to Application Insights and stays there. If the two-exporter
   story is load-bearing (and *system-design.md* says it is, twice), the agent must
   be a hosted agent, or the instrumentation must live in the FastAPI service and
   the agent must be driven through the Responses API. **This is the single most
   consequential finding in this document.** Note also that the exporter env vars
   are immutable per agent version, so "switch the exporter to AX in Phase 8" means
   *cut a new agent version*, not edit a setting.

6. **Standard agent setup pulls in Cosmos DB.** If agent threads and messages are to
   live in your own subscription rather than Microsoft-managed storage, that is
   Blob + AI Search + **Cosmos DB**. No planning document budgets a Cosmos account.
   Basic setup avoids it, at the cost of Microsoft holding conversation state.

7. **Cortex Analyst pins the region.** *This is the constraint the whole stack has to
   bend around, and no planning document mentions it.* Cortex Analyst is natively
   available on Azure in **East US 2 and West Europe only**. Everywhere else needs
   cross-region inference — sending inference outside the region you chose, on an
   account whose region is fixed at signup and cannot be changed. Combined with
   Foundry Agent Service (East US 2: full tool matrix) and Content Safety (East US 2:
   image, prompt shields, groundedness all present), **East US 2 is the region.**
   See the recommendation below.

8. **Cortex Analyst wants a semantic *view*, not a semantic model YAML.**
   *system-design.md* Phase 4 says "build the semantic view Cortex Analyst needs",
   which turns out to be more accurate than it knew: native `CREATE SEMANTIC VIEW`
   objects are now the recommended path, with real RBAC and sharing, and the
   stage-hosted YAML files are backward compatibility only. Build the view; don't
   follow an older tutorial into a YAML file on a stage.

9. **Delta Live Tables is Lakeflow.** *system-design.md* Phase 3 says "a declarative
   pipeline"; the product is now **Lakeflow Spark Declarative Pipelines**. Old code
   runs, but the Python API is `from pyspark import pipelines as dp` with `@dp`
   decorators, `@view` is now `@temporary_view`, and `@materialized_view` is new.
   Writing new code against `import dlt` in 2026 is writing it deprecated.

10. **Two Databricks deadlines land inside this project's five weeks.** Standard tier
    retires **2026-10-01**; new workspaces become UC-only from **2026-09-30**.
    Neither is a problem if the Phase 0 workspace is created Premium with an
    auto-assigned metastore — which it should be anyway, since Unity Catalog needs
    Premium. It *is* a problem if a Standard workspace gets created this week and
    then needs to survive into October.

### Costs and quotas — mostly good news

11. **The reranker is available free.** See below; this closes issue #10 and means
    the ~$75/month Basic line item can stay out of the cost model for now.
    *(Superseded 2026-08-26: Basic was authorised and is now configured, so the
    line item is back in the cost model the moment the service can be created —
    which it currently cannot, at any tier. See the note under "The reranker
    decision" below, and cc-3d5 for what a fixed $73.73/month does to the budget
    thresholds.)*

12. **The $75/month Basic estimate was accurate.** $0.101/hour is $73.73 over a
    730-hour month. *system-design.md*'s "roughly $75/month" needs no correction.

13. **The Snowflake trial assumption was accurate** — 30 days *or* $400, whichever
    comes first. Phase 4 timing stands. Emphasise "whichever comes first": a careless
    all-purpose warehouse can end the trial well before day 30.

14. **Arize is better than the plan assumed, and cheaper.** It is a full **Azure
    Native Integration** (portal provisioning, Entra SSO, Azure billing), not merely a
    Marketplace SaaS listing. And **AX Free gives 25,000 spans/month at 15-day
    retention for $0** — enough to run the Phase 8 public demo on AX without paying
    anything, which makes the "Phoenix until public, then AX" plan easier rather than
    harder. Phoenix remains right for the build weeks: local, air-gappable,
    unmetered.

15. **New since the plan: AI Search Serverless Developer tier** (preview, billing
    starts **2026-09-13**). Consumption-priced with per-index caps (1 GB index,
    300 MB vectors). Not recommended here — no SLA, no migration path in or out, and
    the free tier already covers this corpus — but worth knowing it exists before
    someone spots it in the portal and picks it.

### Things the plan is quietly wrong about

16. **"a 24-hour lifecycle rule" is not a 24-hour guarantee.** *system-design.md*
    Phase 6 and the cost guardrails both promise images are deleted after 24 hours.
    Lifecycle rules have **day** granularity and the engine takes **up to 24 hours**
    to even begin executing after a change, then runs periodically. Real behaviour is
    deletion **24–48 hours** after upload. Two fixes: state the honest number in the
    UI copy ("deleted within 48 hours"), and **make sure blob soft delete is off on
    that account** — otherwise the lifecycle delete only soft-deletes and the images
    are retained for the full soft-delete window, which is precisely the thing the
    design says it doesn't want to do.

17. **The AI Search free tier has no managed identity.** *rfc-001.md* leans on
    identity-bound access as a design property. On the free tier, AI Search must be
    reached with an API key — no managed identity, no customer-managed keys, no IP
    firewall, no private endpoints. That's an acceptable PoC trade-off, but it should
    be a stated one rather than a surprise, and it's the strongest non-reranker
    argument for Basic.
    *(Resolved in configuration on 2026-08-26: with Basic authorised,
    `search.tf` sets `local_authentication_enabled = false`, so the data plane is
    reachable only through the two role assignments on the app's user-assigned
    identity. Nothing ever consumed a search key — the app is handed
    `AZURE_SEARCH_ENDPOINT` and its identity — so this removed a credential
    rather than changing a code path. It takes effect when the service can
    actually be created.)*

18. **The free AI Search tier can starve the indexer.** 50 MB of storage and 3 indexes
    are generous for this corpus, but with a skillset attached the indexer is capped
    at **3–10 minutes per run** and **10,000 documents per invocation**, plus 20 free
    enrichment transactions per indexer per day for skills that call Foundry Tools.
    Integrated vectorization on the free tier will need the harvest chunked across
    scheduled runs rather than one big load. Budget an evening for this in Phase 5.

19. **Container Apps scale-to-zero has two documented ways to strand the app**: no
    ingress and no scale rule means it can never wake up; and CPU/memory scale rules
    *cannot* scale to zero at all. The HTTP scale rule is the one that works. Also,
    the managed certificate requires the subdomain CNAME to point **directly** at the
    container app FQDN — putting Cloudflare in front of the demo domain breaks
    certificate issuance and renewal.

20. **No published cold-start number exists.** *system-design.md* says a cold start
    "costs a visitor a couple of seconds". Microsoft publishes no figure. Measure it
    in Phase 0 and put the real number in the write-up; it is exactly the sort of
    detail that makes a demo narration credible.

---

## The reranker decision (issue #10)

> **Superseded on 2026-08-26, on cost grounds that were then overtaken by an
> outage.** The account owner authorised the Basic tier (cc-6wz) to get past the
> capacity failure in cc-3wo, so `var.search_sku` is now `"basic"` and the
> semantic ceiling below no longer binds. That authorisation did not achieve
> what it was bought for: East US 2 is out of AI Search capacity **at every
> tier**, not merely in the shared free pool, so Basic returns the same
> `InsufficientResourcesAvailable` as Free and no search service exists yet.
> Verified 2026-08-26 through both Terraform and `az search service create`,
> against untouched regional quota (free 0/1, basic 0/16, standard 0/16).
>
> The analysis below is left intact because its reasoning is still sound and
> will apply again if the tier is ever reconsidered. Only its premise — that a
> Free service can be created in this region — turned out to be false.

**Answer: option 1 — the free tier is fine, with a stated ceiling.**

The facts, all checked 2026-08-25:

- Semantic ranker **runs on the Free search tier**. The current wording in
  [Choose a pricing model and service tier](https://learn.microsoft.com/en-us/azure/search/search-sku-tier)
  (ms.date 2026-08-04) is: *"Semantic ranker — Runs on the Free tier but not
  recommended for large workloads."* This is a change from the older guidance that
  the free tier did not support semantic ranking, and it is the reason this ticket
  was worth filing.
- The semantic-ranker **free billing plan is "Available on all pricing tiers"** and
  grants **the first 1,000 semantic requests per month**. Past that, requests return
  a billing error rather than silently charging you.
- The **standard billing plan requires Basic or higher.** So on the Free tier the
  1,000/month allowance is not a soft limit you can pay past — it is the ceiling.
- If you do move to Basic: **$0.101/hour ≈ $73.73/month**, and semantic queries past
  the free 1,000 cost **$1.00 per 1,000**.

**Why option 1 rather than option 2.** Demo traffic will not approach 1,000 semantic
queries a month. The risk the ticket correctly identified is the *evaluation* runs —
Phase 5 retrieval eval and Phase 9 experiments are far heavier than visitors. Two
things keep that inside the ceiling: charges only accrue when `queryType=semantic`
**and** the search string is non-empty (a `search=*` semantic query is free), and
retrieval evaluation should be run against a fixed question set — an 80-question
golden set run 12 times is 960 queries, which fits, and if it doesn't, the failure
mode is an explicit billing error rather than a surprise invoice.

**What to write down alongside the decision:**

- **The ceiling is 1,000 semantic queries per calendar month, and it is hard.** Add a
  counter to the retrieval eval harness so a runaway sweep is caught before the
  quota is.
- **The free tier's semantic throughput is unpublished.** The concurrency and
  queue-depth table in the service limits starts at Basic; Free has no row. Expect
  it to be slow and shared, and do not benchmark latency on it.
- **Reconsider Basic if any of these become true**, at which point the ~$74/month is
  buying more than the reranker: managed identity is wanted for AI Search access
  (item 17), the corpus outgrows 50 MB, or the free-tier indexer's 3–10 minute
  skillset cap makes integrated vectorization painful (item 18).
- If you do buy Basic, **add teardown to the runbook** — it bills hourly whether or
  not anyone is using it.

---

## Region recommendation: East US 2

GH #2 asks for the region decision to be recorded here.

**East US 2**, for both the Azure resources and the Snowflake account.

The constraint that decides it is Cortex Analyst: on Azure it is natively available
only in **East US 2** and **West Europe**, the Snowflake account's region is fixed at
signup, and the alternative is cross-region inference. Of those two, East US 2 wins
on everything else:

| | East US 2 |
| --- | --- |
| Snowflake Cortex Analyst | ✅ native (one of two Azure regions) |
| Foundry Agent Service | ✅ Agents + Responses API, and the **full tool matrix** including Computer Use |
| Azure AI Search | ✅ semantic ranker region, and in the higher-capacity partition set |
| Content Safety | ✅ image, prompt shields, groundedness, protected material |
| Document Intelligence, Container Apps, Blob/ADLS | ✅ |

**The one trade-off:** Content Safety **multimodal** (text+image in one call, still
preview) and **custom categories (standard)** are not in East US 2 — they need East
US or West Europe. The design does not use either: Phase 6 calls image moderation on
the upload and text moderation on the prompt as separate calls, which East US 2
supports. If multimodal moderation later becomes desirable, it can run as a second
Content Safety resource in East US without moving anything else.

---

## Notes on method

- Prices came from the [Azure Retail Prices API](https://prices.azure.com/api/retail/prices)
  (`armRegionName eq 'eastus2'`, `currencyCode 'USD'`) rather than the pricing pages,
  which now render prices client-side and serve `$-` to a fetch. Spot-checked against
  East US: identical for every meter used here.
- Microsoft Learn `ms.date` values are quoted so a future reader can tell whether a
  page has moved on since this pass. Where a page's `ms.date` is materially older
  than its `updated_at`, both are noted.
- **One row is not fully verified:** the Arize AX Microsoft Marketplace listing page
  returns HTTP 403 to non-browser clients, so its plan and price surface was inferred
  from Arize's own pricing page and the Azure Native Integrations announcement rather
  than read directly. Confirm it in a browser during Phase 0 — it is a two-minute job
  while you are in the portal anyway.
- Nothing here required an Azure login. Everything is public documentation, public
  pricing, or the public retail price API.
