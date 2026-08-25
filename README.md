# chip_chat

A conversational assistant for a fast-casual restaurant, built as a learning proof of concept
across Azure AI, Snowflake, Databricks, and Arize.

The bot is called **Cilantro**. It answers questions about the menu, answers questions about your
own account, takes actions on that account, and can turn a photograph of a meal into an order.

> **This is a proof of concept.** It runs on Chipotle's publicly published menu and nutrition data
> and on entirely synthetic customer accounts — no real customer data, no real orders, no payment,
> no fulfillment. It is not affiliated with or endorsed by Chipotle Mexican Grill.

## Documents

| Document | What it covers |
| --- | --- |
| [System design & build plan](https://claude.ai/code/artifact/6943b476-51fe-481b-8399-980d477730b8) | Architecture, the five capability lanes, twelve-phase build plan, cost guardrails |
| [Cilantro PRD](https://claude.ai/code/artifact/3eff0560-14f4-4f81-84e2-04e71a90fb95) | Problem, personas, goals and non-goals, requirements, flows, success metrics |
| [RFC-001 — Chip Chat](https://claude.ai/code/artifact/4221a4e4-6bcc-423e-9b63-0bd00e0ec26c) | Components, data model, trust boundaries, tool contracts, failure modes, decisions |

Read them in that order. The system design frames the problem, the PRD defines what to build, and
the RFC defines how.

## Architecture in one paragraph

Two clocks. **Nightly**, Databricks ingests two sources — Chipotle's published menu pages and a
seeded generator of synthetic accounts — through a medallion lakehouse in Unity Catalog, then
publishes a chunked knowledge index to Azure AI Search and personalization gold marts to Snowflake.
**Per turn**, a visitor types a name, is assigned a loaded demo persona, and talks to a single
Azure AI Foundry agent that calls eleven tools across five lanes: menu knowledge (RAG), account
questions (Snowflake Cortex Analyst), personalization (gold marts), photo matching (vision model
plus a deterministic catalogue matcher), and actions (an Azure Functions ops API). Every account
read and write runs over a Snowflake session whose demo identity is bound by the app and enforced
by row access policies — no tool signature accepts a visitor identifier. Every turn emits one
OpenTelemetry span tree, exported to Application Insights for service health and Arize for agent
evaluation.

## The five lanes

| Lane | Example | Path |
| --- | --- | --- |
| Knowledge | "Is the barbacoa spicy?" | Hybrid RAG over the real published menu |
| Account | "How many points do I have?" | NL→SQL via Cortex Analyst |
| Action | "Reorder my usual, add guac" | Ops API → Snowflake procs, behind a confirmation card |
| Personalization | "What's my usual?" | Databricks gold marts, computed nightly |
| Vision | "Make me what's in this photo" | Vision model describes → matcher resolves SKUs |

## Repository layout

```
infra/        Terraform for all Azure resources
harvest/      Public menu, nutrition, and policy ingestion
data-gen/     Seeded synthetic account generator
databricks/   Unity Catalog medallion pipelines, MLflow recommender
snowflake/    Schema, RBAC, row access policies, semantic view, stored procs
agent/        Foundry agent definition and tool implementations
vision/       Photo pipeline: validate, moderate, describe, match
api/          FastAPI service, sessions, budget enforcement, ops API
web/          Chat widget and entry flow
eval/         Golden set, adversarial suite, Arize experiments
otel/         Shared OpenInference instrumentation
```

## Status

Planning complete; implementation not started. The build plan runs twelve phases over roughly five
weeks of evenings and weekends. Phase 0 is Terraform scaffolding and verifying current service
names and tiers across all four platforms.

## Two things not to get wrong

1. **Identity is never a tool argument.** It is bound to the Snowflake session by the app and
   enforced by row access policies. The absence of the parameter is the enforcement mechanism.
2. **The spend cap is inline, not observability.** A public endpoint with no authentication needs a
   synchronous budget check in front of every model call. Azure budget alerts notify after the fact;
   Arize reports what was spent. Neither prevents anything.
