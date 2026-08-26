# Decision: the agent is a hosted agent on a basic-setup project

**Issue:** [#102](https://github.com/gganssle/chip_chat/issues/102) (bead `cc-ozf`) · **Decided:** 26 August 2026
**Also resolves:** the Cosmos DB question and the state-ownership half of [#11](https://github.com/gganssle/chip_chat/issues/11) (RFC-001 §13 Q2)
**Unblocks:** [#60](https://github.com/gganssle/chip_chat/issues/60) (agent definition), [#64](https://github.com/gganssle/chip_chat/issues/64) (instrumentation), [#78](https://github.com/gganssle/chip_chat/issues/78) (exporter repoint)

---

## The decision, in one paragraph

Cilantro's agent is a **hosted agent** — our container, run by Foundry Agent Service —
on a project provisioned with **basic setup**, so agent threads live in
Microsoft-managed storage and no Cosmos DB account is created. Conversation *messages*
belong to the Foundry thread; everything with a security role or a cost role —
order drafts, confirmation state, budget counters, persona assignment, and the
`thread_id` itself — belongs to the app's durable session store, which
[#9](https://github.com/gganssle/chip_chat/issues/9) already required us to build.

Two things forced this. Only a hosted agent reads `OTEL_EXPORTER_OTLP_*`, so only a
hosted agent can deliver decision **D6**. And basic setup is the only shape that keeps
the observability plane inside a $150/month subscription, because standard setup's real
cost is not the Cosmos line — it is the collision with the one free Azure AI Search
service the knowledge lane is already spending.

---

## Why not a prompt agent

A prompt agent is the cheapest thing to build: declarative config, no container, no
image to ship, no build step between an idea and a Phase 7 demo. If the observability
plane were incidental, it would win.

It is not incidental. It is engineering goal six — *"one instrumentation standard
feeding both an infrastructure backend and an agent-evaluation backend"* — it is
decision **D6**, and `system-design.md` calls it a deliberate demonstration twice.
A prompt agent's tracing goes to Application Insights and stays there; that path is not
configurable to a third party.

The precise shape of the loss is worth stating, because "a prompt agent kills dual
export" is too strong. Our FastAPI service can instrument whatever it likes and export
wherever it likes, prompt agent or not. What a prompt agent cannot export to Arize are
the spans *Foundry itself* emits: `agent.step`, `llm.completion`, per-tool spans with
arguments and token counts. Those are exactly the spans Arize exists to consume —
trajectory evals and tool-selection evals score the model's choices, and
`system-design.md` names tool selection as *"the single question this entire
architecture turns on"*. So a prompt agent does not cost us some observability; it
costs us the observability the second backend was bought for, and leaves the first
backend answering a question App Insights was already answering.

There is a second, quieter reason. Under a prompt agent, Foundry originates the HTTP
calls to our tool endpoints. RFC §05 requires that visitor identity reach the data tier
as request context that the model cannot see or set — and under a prompt agent that
means depending on Foundry's ability to attach per-turn context to an OpenAPI tool
call, a mechanism we have not verified and would not want the isolation guarantee to
rest on. With a hosted agent, our container receives the turn and originates every tool
call itself, so the identity plumbing is ours end to end. *(Inference, not a verified
capability claim — but the risk asymmetry is real either way, because the shape that
makes it moot is the one we picked.)*

## Why not the Responses API

Option 3 — drive the model through the Responses API from the FastAPI service, with no
Foundry agent resource at all — genuinely solves the export problem. We would own the
whole telemetry path with nothing to work around.

It reopens a settled decision. RFC-001 §14 rejects *"a custom orchestrator instead of
Foundry Agent Service"* on the grounds that **the managed agent runtime is the thing
worth learning here**, and hand-rolling the loop teaches agent internals at the cost of
the platform. That rationale has not changed, and the issue that raised this question
was right to insist that reopening it be argued rather than slid in. We are not
reopening it.

The honest complication is that a hosted agent is a partial concession in that
direction: the orchestration loop does run in our container. What it keeps is
everything the §14 rejection was actually protecting — the agent resource, thread
storage, agent versioning, tool registration, platform identity, and Foundry's own
tracing alongside ours. The Responses API keeps none of that. Hosted agent is the
shape that pays the smallest amount of the "custom orchestrator" price for the whole
of the D6 benefit.

## What the hosted agent costs us

Three documented constraints, all of which change something downstream.

**Exporter environment variables are immutable per agent version.** Repointing the
exporter from Phoenix to AX is not editing a setting; it is cutting a new agent
version. See the note on #78 below.

**`service.name` is forced to the agent's name** — `OTEL_SERVICE_NAME` is ignored. So a
single turn's trace now spans two service names: the app emits `chat.turn`,
`guard.*` and `render.response`, and the agent container emits `agent.step`,
`llm.completion` and `tool.*` under the agent's name. Two consequences for #64:
W3C trace context must propagate across the app→agent boundary or the trace splits in
half, and any dashboard or eval that filters on `service.name` must expect both values.
Neither is hard; both are unpleasant to discover in Phase 9.

**`APPLICATIONINSIGHTS_CONNECTION_STRING` is platform-injected and cannot be
overridden**, so App Insights export cannot be turned off per agent — only disabled at
the project level. The design wants both backends anyway, so this is informational
rather than a constraint, and it is the reason RFC §09's "fan out" is literally true
for the agent's spans: we get App Insights whether we ask for it or not, and add OTLP
alongside.

And we now ship a container. That is a real build cost, not a paper one: an image, a
registry, and a deployment step that has to exist before the Phase 7 demo rather than
after it.

---

## The Cosmos DB question

**Answer: basic setup. No Cosmos DB account.**

Standard setup would put agent threads and messages in our own subscription, at the
cost of Blob + Azure AI Search + Cosmos DB. Three reasons it is the wrong trade here.

**1. There is nothing to protect.** The RFC's non-goals say there is no PII in this
system *by construction*, and every account, order and loyalty row is synthetic. The
thing standard setup buys — Microsoft not holding your conversation state — has real
value on a system with real customer data and approximately none on a system whose
data was generated by a seeded script. Paying for data residency over synthetic
personas is paying for a property we cannot use.

**2. The Cosmos line is affordable; the line next to it is not.** Cosmos itself is
small — East US 2 retail, checked 26 August 2026 via the Azure Retail Prices API:

| Cosmos mode | Price | At demo volume |
| --- | --- | --- |
| Serverless | $0.25 per 1M RUs + $0.25/GB-month stored | cents |
| Provisioned, shared-throughput database at the 400 RU/s floor | $0.008 per 100 RU/s-hour | **$23.36/month** (730 h) |

Whether Foundry's standard setup will accept a *serverless* Cosmos account is not
established by the service inventory, so $23.36/month is the number to plan against,
not the cents.

The expensive part is Azure AI Search. Standard setup wants an AI Search resource of
its own, and the free tier is **one service per subscription with three indexes** —
the same single free service the knowledge lane is already spending, and on the
strength of which [#10](https://github.com/gganssle/chip_chat/issues/10) kept the
~$75/month Basic line item out of the cost model. Three indexes do not comfortably hold
a menu index, the alias-swap spare that RFC §08 requires during a re-harvest, and a
Foundry-owned agent-state index. The free tier also has **no managed identity**, which
makes it a poor thing to hand a platform service a connection to. *(That standard setup
cannot share the free service is an inference from those two published limits, not a
documented refusal — but it is the way to bet, and the bet is one-directional: if it is
wrong we saved nothing, and if it is right we would have found out by having Phase 5
starve.)*

So standard setup's realistic bill is **$23/month of Cosmos plus $73.73/month of AI
Search Basic ≈ $97/month**, against a $150/month budget whose expected steady state is
$30–60 (bead `cc-8b6`). It would roughly triple the run rate and turn the 50% budget
alert from a real signal into monthly noise — which is precisely the property `cc-8b6`
set the threshold to preserve.

**3. It buys nothing #9 needs.** A returning visitor resumes because the app stores
their `thread_id`, not because the thread bytes sit in our subscription.

---

## State ownership (resolves the design half of #11)

| State | Owner | Why |
| --- | --- | --- |
| Message history | **Foundry thread** (Microsoft-managed, basic setup) | The managed runtime's job; addressed by `thread_id` |
| `thread_id` | **App** — column on `demo_visitors` | The pointer must outlive the visit; #9 made the visitor row durable |
| Order drafts | **App** | `propose_order` mints a `draft_id` the ops API later validates; this is a security artefact |
| Confirmation state | **App** | RFC §06: confirmation is enforced in the ops API, never in the prompt or the thread |
| Receipts | **Snowflake** (`orders`, `loyalty_ledger`) | System of record. A later turn re-queries; it does not remember |
| Budget counters | **App session store** | RFC §11 requires an inline synchronous check in front of every model call |
| Persona assignment | **App** (`demo_visitors.persona_id`), mirrored in Snowflake | Identity originates server-side; RFC §05 |

**Persona switch starts a new thread.** A thread carrying another persona's context
degrades lane selection and pollutes the trajectory evals that justify AX.

**One empirical question remains, and it belongs to [#8](https://github.com/gganssle/chip_chat/issues/8) (`cc-v9q`): how long
Microsoft-managed thread storage retains a thread, and whether a thread can be
retrieved by id after an arbitrary gap between visits.** The inventory establishes the
message ceiling (100,000 per thread) but not a retention period. If threads do not span
visits, the fallback is cheap *because of this decision, not despite it*: the app
already owns a durable per-visitor store, so message history moves into it and nothing
else in the table changes. That is #11's "changes the app tier materially" branch, and
it is now much less material than it sounded when #11 was written — the app tier is
already durable, already keyed by `demo_id`, and already holds four other kinds of
state.

#11 stays open until #8 reports that number. Nothing else in it is unresolved.

---

## Consequences for open tickets

- **#60** — write the agent definition as a hosted agent: a container image, tools
  registered against it, and identity arriving as request context rather than as a tool
  argument. Unblocked.
- **#64** — propagate W3C trace context across the app→agent boundary, and expect two
  `service.name` values in one trace. Unblocked.
- **#78** — the "prove it was a config change" criterion is rewritten for agent-version
  immutability. The claim worth proving is now: *no instrumentation code changes, and
  the switch is expressed entirely as a new agent version whose only diff is the
  exporter environment variables and the connection holding the AX credentials.* Note
  the switch is asymmetric — the FastAPI tier's exporter is an environment variable and
  a restart; the agent's is a new version and a deployment.
- **#11** — state ownership settled above; stays open for the thread-retention number
  from #8.
- **RFC-001** — §01, §09, §12 (new D8) and §13 Q2 updated to match.

## Revisit

If a hosted agent's build-and-ship overhead is genuinely blocking the Phase 7 demo, a
prompt agent is a legitimate fallback — but taking it means striking D6 and #78 in the
same commit, not quietly discovering in Phase 9 that the second backend has nothing
interesting to show. Make that trade explicitly or not at all.

## Sources

`docs/service-inventory.md` §2.1 and items 4, 5 and 6 of *What changed versus the
plan*, checked 25 August 2026 — itself sourced from
[Export hosted agent telemetry by using OpenTelemetry](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)
and [Foundry Agent Service limits and quotas](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions).
Cosmos DB prices from the [Azure Retail Prices API](https://prices.azure.com/api/retail/prices),
East US 2, retail, checked 26 August 2026. Budget figures from bead `cc-8b6`.
