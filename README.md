# chip_chat

A conversational assistant for a fast-casual restaurant, built as a learning proof of concept
across Azure AI, Snowflake, Databricks, and Arize.

The bot is called **Cilantro**. It answers questions about the menu, answers questions about your
own account, takes actions on that account, and can turn a photograph of a meal into an order.

> **This is a proof of concept.** It runs on Chipotle's publicly published menu and nutrition data
> and on entirely synthetic customer accounts — no real customer data, no real orders, no payment,
> no fulfillment. It is not affiliated with or endorsed by Chipotle Mexican Grill.

## Documents

All three live in [`docs/`](docs/README.md).

| Document | What it covers |
| --- | --- |
| [System design & build plan](docs/system-design.md) | Architecture, the five capability lanes, twelve-phase build plan, cost guardrails |
| [Cilantro PRD](docs/cilantro-prd.md) | Problem, personas, goals and non-goals, requirements, flows, success metrics |
| [RFC-001 — Chip Chat](docs/rfc-001.md) | Components, data model, trust boundaries, tool contracts, failure modes, decisions |

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
catalog/      The consolidated menu catalogue: what is orderable
data-gen/     Seeded synthetic account generator
databricks/   Unity Catalog medallion pipelines, MLflow recommender
snowflake/    Schema, RBAC, row access policies, semantic view, stored procs
search/       The retrieval index: chunk schema, vectorization, alias-swap rebuilds
agent/        Hosted agent: the container image, its version manifest, the tools
vision/       Photo pipeline: validate, moderate, describe, match
api/          FastAPI service, sessions, budget enforcement, ops API
web/          Chat widget and entry flow
eval/         Golden set, adversarial suite, Arize experiments
otel/         Shared OpenInference instrumentation
```

Each directory is a uv workspace member holding one importable package under
`src/chip_chat/`, sharing a single lockfile at the repository root. `otel/` is a
leaf: everything may import it, it imports nothing. See
[`docs/README.md`](docs/README.md#repository-conventions) for the conventions.

The Azure account this all bills to — subscription and tenant ids, resource group,
Key Vault URI, and the monthly budget guarding it — is recorded in
[`infra/README.md`](infra/README.md).

## Getting started

```bash
make setup      # fresh clone -> working state (needs uv on PATH)
make ci         # format check, lint, type check, import contracts, tests
make dev        # start the local stack and send one instrumented turn through it
make deploy     # roll the chat app onto the public URL (see docs/deployment.md)
make help       # everything else
```

`make dev` brings up Phoenix — the agent-observability backend, in a container —
and sends it a session that exercises every span in the schema, so the trace tree
is there to read the first time you open <http://localhost:6006>. Tracing is not a
late deliverable here: it is how you find out why something does not work.
[`docs/local-tracing.md`](docs/local-tracing.md) explains the loop and how to read
what you see.

The agent runs in a container of its own, so one turn's trace crosses a process
boundary and carries two `service.name` values. `make agent-image-boundary` builds
that image and sends one turn through it — the app half here, the agent half in
the container — and it should arrive as **one** trace.
[`agent/README.md`](agent/README.md) is that story end to end.

[`docs/local-setup.md`](docs/local-setup.md) takes it from a clean machine: the `az`,
`terraform` and `databricks` CLIs, how each one authenticates, and the single rule for
how secrets reach a local process. It also explains why the `snow` CLI is deliberately
not installed yet.

## Status

> **⏳ The Snowflake trial expires 2026-09-24.** Started 2026-08-25 on AWS us-east-2, Enterprise,
> 30 days or roughly $400 of credits — whichever runs out first. The serving layer is the account
> and action lanes, so the clock is the demo's clock.
> [`docs/snowflake-account.md`](docs/snowflake-account.md) §10 has the burn against the allowance
> and the plan for the morning of the 25th.

Phase 0 is done and the **week-one ugly slice is deployed**: one end-to-end path — a menu
question, an account question, a simulated order — running on a literal three-item menu and one
hardcoded account, behind the inline spend cap, on the Container Apps default FQDN. Every turn
emits one `chat.turn` span tree.

The URL is live and has deliberately **not been shared**; the roadmap is explicit that those are
different things. [`docs/deployment.md`](docs/deployment.md) is the write-up — the procedure, what
it costs, and the ten things about the deployment story that turned out not to work the way the
documentation implies.

The data behind that slice is a placeholder and is meant to be deleted rather than extended. The
build plan runs twelve phases over roughly five weeks of evenings and weekends.

The work is filed as GitHub issues, each carrying exactly one priority label that encodes
implementation order. Label definitions live in [`.github/labels.yml`](.github/labels.yml).

| Label | What it covers |
| --- | --- |
| `P0` | Foundation and blockers — accounts, Terraform, the span schema, the week-one slice, the spend cap |
| `P1` | Data foundations — harvest, synthetic generator, Databricks lakehouse, Snowflake serving layer |
| `P2` | The five lanes, the agent, and the public app |
| `P3` | Evaluation and hardening, including both launch gates |
| `P4` | Cost, operations and documentation |
| `P5` | Deferred past V0 — V1 features and the named RFC revisit triggers |

Two ordering notes that override a strict reading of the labels. The **week-one ugly slice** cuts
across every phase on purpose and is P0 for that reason. And the **inline spend cap** ships before
the URL is shared with anyone, not when the Phase 10 hardening checklist is finally reached.

## Two things not to get wrong

1. **Identity is never a tool argument.** It is bound to the Snowflake session by the app and
   enforced by row access policies. The absence of the parameter is the enforcement mechanism.
2. **The spend cap is inline, not observability.** A public endpoint with no authentication needs a
   synchronous budget check in front of every model call. Azure budget alerts notify after the fact;
   Arize reports what was spent. Neither prevents anything.
