# chip_chat

A conversational assistant for a fast-casual restaurant, built as a learning proof of concept
across Azure AI, Snowflake, Databricks, and Arize.

The bot is called **Cilantro**. It answers questions about the menu, answers questions about your
own account, takes actions on that account, and can turn a photograph of a meal into an order.

> **This is a proof of concept.** It runs on Chipotle's publicly published menu and nutrition data
> and on entirely synthetic customer accounts — no real customer data, no real orders, no payment,
> no fulfillment. It is not affiliated with or endorsed by Chipotle Mexican Grill.

## Documents

The three plans:

| Document | What it covers |
| --- | --- |
| [System design & build plan](docs/system-design.md) | Architecture, the five capability lanes, twelve-phase build plan, cost guardrails |
| [Cilantro PRD](docs/cilantro-prd.md) | Problem, personas, goals and non-goals, requirements, flows, success metrics |
| [RFC-001 — Chip Chat](docs/rfc-001.md) | Components, data model, trust boundaries, tool contracts, failure modes, decisions |

Read them in that order. The system design frames the problem, the PRD defines what to build, and
the RFC defines how. Where two of them disagree, whichever one owns the subject wins.

They are no longer the whole of [`docs/`](docs/README.md), which now holds thirty-odd write-ups
and a `decisions/` directory. Six worth knowing by name:

| | |
| --- | --- |
| [deployment.md](docs/deployment.md) | Getting the app onto the public URL, and the twelve things that did not work the way the documentation implies |
| [runbook.md](docs/runbook.md) | Kill switch, rollback, scale, demo reset, teardown, rebuild, incident triage — written to be run from a phone |
| [cost.md](docs/cost.md) | What one conversation costs across four platforms, and the guardrail audit |
| [red-team.md](docs/red-team.md) | Both launch gates, attacked rather than argued |
| [failure-isolation.md](docs/failure-isolation.md) | Which lane goes down when which dependency does |
| [decisions/](docs/decisions/) | One file per question the plans left open |

[`docs/README.md`](docs/README.md) is the index, and it explains what kind of document each one is.

## Architecture in one paragraph

Two clocks. **Nightly**, Databricks ingests two sources — Chipotle's published menu pages and a
seeded generator of synthetic accounts — through a medallion lakehouse in Unity Catalog, then
publishes a chunked knowledge index to Azure AI Search and personalization gold marts to Snowflake.
**Per turn**, a visitor types a name, is assigned a loaded demo persona, and talks to a single
Azure AI Foundry agent that calls eleven tools across five lanes: menu knowledge (RAG), account
questions (Snowflake Cortex Analyst), personalization (gold marts), photo matching (vision model
plus a deterministic catalogue matcher), and actions (an Azure Functions ops API). Inbound text is
moderated before it reaches the model and every write goes through a confirmation card the ops API
checks in code. Every account read and write runs over a Snowflake session whose demo identity is
bound by the app and enforced by row access policies — no tool signature accepts a visitor
identifier, at four tiers, each with its own test. Every turn emits one OpenTelemetry span tree,
exported to Application Insights for service health; the Arize half is an endpoint and a header
away and is [#78](https://github.com/gganssle/chip_chat/issues/78), not yet wired.

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
eval/         Eight suites: golden, adversarial, photos, trajectory, grounding,
              retrieval, dietary, dataset — each with a committed BASELINE.md
otel/         Shared OpenInference instrumentation
```

Thirteen directories, each a uv workspace member holding one importable package
under `src/chip_chat/`, sharing a single lockfile at the repository root.
`otel/` is a leaf: everything may import it, it imports nothing, and
`make imports` enforces that. **Every one of them has a `README.md`** saying what
it owns and how to run it. See
[`docs/README.md`](docs/README.md#repository-conventions) for the conventions and
[`CLAUDE.md`](CLAUDE.md) for the invariants.

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

`make help` is not a formality — there are about eighty targets in ten families:
`infra-*` (Terraform), `search-*` (the retrieval index), `snowflake-*` (the serving layer),
`agent-image-*`, `verify-*`, the eight eval suites, `reharvest`/`freshness`, and the deploy,
rollback and takedown group that [`docs/runbook.md`](docs/runbook.md) documents. Anything whose
help text says **free** costs nothing and needs no credential; everything else does one or both,
which is why none of it is in `make ci`.

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
`terraform`, `databricks` and `snow` CLIs, how each one authenticates, and the single rule
for how secrets reach a local process.

## Status

> **⏳ The Snowflake trial expires 2026-09-24.** Started 2026-08-25 on AWS us-east-2, Enterprise,
> 30 days or roughly $400 of credits — whichever runs out first. The serving layer is the account
> and action lanes, so the clock is the demo's clock.
> [`docs/snowflake-account.md`](docs/snowflake-account.md) §10 has the burn against the allowance
> and the plan for the morning of the 25th.

**The app is live** at
<https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io>, on revision
`0000018`, scaled to zero when nobody is looking. The URL has deliberately **not been shared**;
the roadmap is explicit that deploying and sharing are different things, and
[`docs/deployment.md`](docs/deployment.md) §5 lists what still has to be true before it is.

**The foundations and the data are built.** The real Chipotle menu, nutrition and store data is
harvested and consolidated into a catalogue; a seeded generator produces 500 synthetic customers
across 30 stores with eighteen months of history; a medallion lakehouse in Unity Catalog runs
four pipelines and publishes a chunked corpus to Azure AI Search and four personalization marts to
Snowflake every night. The Snowflake serving layer has its roles, row access policies, semantic
view and stored procedures, and a nightly demo reset. All five lanes, the ops API, content safety,
the public demo tier and the red-team suites exist and are tested.

**Two things are honestly incomplete, and both are visible.** The tool layer on the deployed
revision still answers from in-memory fallbacks rather than the lanes behind it —
`GET /healthz/lanes` returns `not_wired` for all five, which is the correct answer and not a
contradiction. And a visitor can see one consequence of that: the opening message reads the
assigned persona while `get_points_balance` reads a hardcoded account, so one conversation can
present two balances. [`docs/public-demo.md`](docs/public-demo.md) has the transcript. That is the
first thing to fix before the link is given to a stranger.

**What it costs, measured rather than projected.** One median conversation is about **1.6 cents**
of model tokens — inside PRD §05's $0.05 target with room. One *account question* is **$0.20**,
four times the whole per-conversation budget, because Cortex Analyst bills 67 credits per 1,000
messages. And the standing infrastructure behind the nightly marts costs about **$41.50 a month**
whether anybody talks to Cilantro or not, which at any plausible demo volume outweighs both.
[`docs/cost.md`](docs/cost.md) has the arithmetic, the reconciliation, and the guardrail audit.

The build plan runs twelve phases over roughly five weeks of evenings and weekends. Where it is,
as of **27 August 2026**:

| | closed | open |
| --- | ---: | ---: |
| `P0` Foundation and blockers | 16 | 2 |
| `P1` Data foundations | 29 | 2 |
| `P2` The five lanes, the agent, the public app | 19 | 6 |
| `P3` Evaluation and hardening | 6 | 9 |
| `P4` Cost, operations, documentation | 1 | 5 |
| `P5` Deferred past V0 | 0 | 6 |

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

Two ordering notes that override a strict reading of the labels. The **week-one ugly slice** cut
across every phase on purpose and was P0 for that reason. And the **inline spend cap** shipped
before the URL was shared with anyone rather than waiting for the Phase 10 hardening checklist;
it is in the request path today, and `guard.budget_check` costs a median of 0 ms across the
deployed spans.

## Four things not to get wrong

[`CLAUDE.md`](CLAUDE.md) is the long form, with the file and test enforcing each one. In short:

1. **Identity is never a tool argument.** It is bound to the Snowflake session by the app and
   enforced by row access policies. The absence of the parameter is the enforcement mechanism.
2. **No write without explicit confirmation**, checked in the ops API in code — not by prompt
   instruction and not by UI convention.
3. **The spend cap is inline, not observability.** A public endpoint with no authentication needs a
   synchronous budget check in front of every model call. Azure budget alerts notify after the fact;
   Arize reports what was spent. Neither prevents anything.
4. **Real published menu, entirely synthetic accounts.** Everything Cilantro says about food comes
   from what the restaurant publishes; everything it says about "you" comes from a generated
   customer. Never blur them.
