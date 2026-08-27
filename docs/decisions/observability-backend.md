# Decision: Phoenix now, Arize AX at launch, and "a config change" means one specific thing

**Issue:** [#13](https://github.com/gganssle/chip_chat/issues/13) · **Decided:** 27 August 2026
**Implements:** RFC-001 [D6](../rfc-001.md#openinference-over-otel-dual-export-phoenix-then-arize-ax), and depends on [D8](../rfc-001.md#hosted-agent-on-a-basic-setup-project-not-a-prompt-agent)
**Unblocks:** [#78](https://github.com/gganssle/chip_chat/issues/78) (repoint the exporter), [#76](https://github.com/gganssle/chip_chat/issues/76) (online evals)
**Corrected by:** [#102](https://github.com/gganssle/chip_chat/issues/102) and [`foundry-agent-shape.md`](foundry-agent-shape.md)

---

## The decision, in one paragraph

**Phoenix, self-hosted in a container, from week one. Arize AX for the public phase.**
Both consume OpenInference over OpenTelemetry, so the instrumentation is written once
and the backend is a value in the environment rather than a branch in the code. The
switch is expressed **entirely as configuration** — an endpoint, a set of headers, and
the connection holding the credentials — but the *unit* of configuration differs by
tier, and this record says so plainly because the version of the claim that does not
say so gave #78 an acceptance criterion it could not meet.

Said as the phrase worth remembering: **no instrumentation code changes; the FastAPI
tier is an environment variable and a restart, and the agent is a new agent version.**

---

## Which backend when

| Phase | Backend | Why | Cost |
| --- | --- | --- | --- |
| Weeks 1–7, local development | **Phoenix**, self-hosted (`arizephoenix/phoenix:version-20.3.0`, pinned) | Tracing an agent while you build it is how you debug it, and paying a vendor for that is silly. `compose.yaml` brings it up; `make dev` sends a turn through it. | A container. Nothing else. |
| The public phase | **Arize AX** | Online evals against real traffic are what justify the cost, and a batch of offline questions will never find what strangers ask. AX brings managed monitoring, alerting and online-eval automation. | AX Free is \$0 at 25,000 spans/month, 1 GB, 15-day retention. AX Pro is \$50/month at 50,000 spans/month, 10 GB, 30-day retention. |
| Throughout, both phases | **Application Insights**, alongside | A different question. App Insights answers *is the service healthy*; Arize answers *is the agent behaving*. This is a fan-out, never a migration. | Inside the existing Log Analytics workspace and its daily cap. |

**Phoenix stays running locally after the switch.** #78 asks whether it should and the
answer is yes: it costs a container, iterating against a local backend is faster than
iterating against a hosted one, and a developer with no AX credentials must still be
able to run `make dev` and read a span tree.

### What AX costs at this demo's volume, and the number that decides the tier

The estate's budget is **\$150/month** (`infra/terraform/variables.tf`,
`monthly_budget_usd`), with alerts at 50%, 80% and 100%, and the 50% threshold is set at
\$75 *deliberately below* an expected steady state of \$30–60 so that crossing it is a
signal rather than a monthly formality. Adding **AX Pro at \$50/month moves steady state
to \$80–110 and makes the 50% alert fire permanently**, which is the same as switching
it off.

So the decision is **AX Free unless a measurement says otherwise**, and the measurement
is the span count. One conversation is four or five turns; RFC-001 §09's tree is roughly
a dozen spans a turn, so a conversation is fifty to sixty spans, and 25,000 spans a month
is on the order of **450 conversations a month** — comfortably above what a shared link
produces, and comfortably below what a link that gets passed around does. The row to
watch is spans per day in AX itself, and the escalation is deliberate: cross 20,000
spans in a month and either buy Pro *and* raise the 50% alert threshold in the same
commit, or reduce the fan-out. Fifteen-day retention is the free tier's real constraint
and it is acceptable here, because the artefacts that have to outlive a demo —
`eval/*/BASELINE.md`, `eval/experiments/results/*.json`, `eval/dataset/DATASET.json` —
are committed to the repository rather than kept in a vendor's database.

Two things about the listing that a cost model has to carry:

- **AX is an Azure Native Integration, not merely a Marketplace SaaS listing.** It
  provisions from the portal with Azure CLI/SDK support, Entra SSO and unified Azure
  billing, so it bills against the subscription and shows up in the same cost export
  everything else does. The plain `Arize AI` Marketplace SaaS listing (publisher
  `arizeai1657829589668`) still exists and is the fallback.
- **The resource provider is `ArizeAi.ObservabilityEval` and it is `NotRegistered` on
  this subscription** as of 27 August 2026. Its `operationStatuses` are offered in East
  US, East US 2 EUAP and Central US EUAP — **not East US 2 GA**, which is where the rest
  of the estate lives. Confirm the deployable resource type's own regions in the portal
  before committing one; a cross-region observability backend is fine (spans go over
  HTTPS) but it is a thing to have decided rather than discovered.

---

## The exporter configuration surface

Every variable, and which tier reads it. This table is the whole of the "switch"; there
is nothing else.

| Variable | Read by | Purpose | Local value | AX value |
| --- | --- | --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | both tiers | Base URL; `/v1/traces` is appended | `http://localhost:6006` | the AX OTLP endpoint |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | both tiers | Full traces URL, used verbatim; wins over the base | unset | optional |
| `OTEL_EXPORTER_OTLP_HEADERS` | both tiers | `api_key=…,space_id=…` | unset | **secret** — Key Vault (app) or a CustomKeys connection (agent) |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | both tiers | Signal-specific form of the above | unset | optional |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | **agent only** | `http/protobuf` | `http/protobuf` | `http/protobuf` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | app, ops API | Enables the App Insights exporter | unset locally | injected by Terraform (app) and by the platform (agent) |
| `CHIP_CHAT_ENVIRONMENT` | both tiers | `deployment.environment` | `local` | `production` |

Two asymmetries worth writing down, because both were surprises:

**`OTEL_EXPORTER_OTLP_PROTOCOL` is an agent-tier variable only.**
`chip_chat.otel.config` never reads it; the FastAPI tier's exporter is OTLP over HTTP
unconditionally. The agent's version manifest carries it because Foundry's hosted-agent
runtime reads it. So the often-quoted "three variables" is the agent's story, and the
app's story is two.

**`APPLICATIONINSIGHTS_CONNECTION_STRING` is platform-injected on the agent and cannot
be overridden.** App Insights export cannot be turned off per agent, only disabled at
the project level. The design wants both backends anyway, so this is informational
rather than a constraint — and it is the reason RFC §09's "fan out" is literally true
for the agent's spans: App Insights arrives whether it is asked for or not, and OTLP is
added alongside.

### Confirmed: the endpoint and the headers are environment configuration, never compiled in

This is #13's second acceptance criterion and it is met **mechanically** rather than by
inspection.

- `otel/src/chip_chat/otel/config.py` resolves the endpoint and the headers from the
  standard OpenTelemetry environment variables and nothing else. There is no file, no
  default endpoint, and no vendor name.
- `otel/src/chip_chat/otel/exporters.py` is a list comprehension over configured slots.
  There is no `if backend == …` in it and no product name.
- `otel/tests/test_export_configuration.py::test_the_exporter_code_names_no_vendor`
  parses `exporters.py`, `config.py` and `tracing.py` with `ast`, collects every
  identifier and every *runtime* string literal (docstrings excluded on purpose — prose
  explaining why the vendor is absent is not the vendor being present), and fails if
  `phoenix` or `arize` appears in any of them.
- `test_switching_the_backend_is_only_a_configuration_change` builds a local-shaped and
  a hosted-shaped configuration and asserts the same exporter type, the same count and
  the same code path.

That test is the proof #78 cites. It is worth being clear about what it does and does
not establish: it establishes that **the instrumentation code does not know which
backend it is talking to**, which is exactly the property D6 was bought for. It does not
establish that the deployment mechanics are symmetric, and they are not.

---

## What "switching is a config change" concretely means in this codebase

The honest version, corrected by #102. There are three tiers and they cost three
different things.

### The FastAPI tier: a variable and a restart

`infra/terraform/compute.tf` builds `local.web_env` and injects it into the Container
App. `var.otlp_endpoint` (default `""`) becomes `OTEL_EXPORTER_OTLP_ENDPOINT` when
non-empty. Setting it and applying rolls a new revision. That is genuinely an
environment variable and a restart.

**One gap, and it is real.** There is no Terraform path for
`OTEL_EXPORTER_OTLP_HEADERS`. `compute.tf` carries deliberately no secrets — its comment
says so — and AX needs an API key and a space id in that header. Closing it means a
Container Apps secret backed by the Key Vault entry `.env.example` already points at.
That is configuration work, not instrumentation work, and #78 records it as such.

### The hosted agent: a new agent version

Foundry hosted-agent environment variables are **immutable per agent version**. There is
no edit. Repointing the agent's exporter means registering a new version whose
`environment_variables` differ, and that is a deployment.

`chip_chat.agent.version` is where that diff is expressed, and it is structurally
opinionated about three things it will not let a version be registered with — a literal
value for `OTEL_EXPORTER_OTLP_HEADERS` (only a `${{connections.…}}` reference), a moving
image tag, and an empty exporter endpoint — because all three are mistakes that cost a
whole agent version to correct.

Two consequences worth stating:

- **`service.name` is forced to the agent's name** and `OTEL_SERVICE_NAME` is ignored,
  which is why one turn legitimately spans two service names and why
  `otel/src/chip_chat/otel/service.py` has a `turn_service_names` at all.
- **Foundry allows 1,000 agent revisions.** Cutting a version to move an endpoint is
  cheap in that budget and expensive in ceremony, which is the right way round.

### The ops API (Functions)

Application Insights only, wired through `site_config`. It emits no OpenInference spans,
so it is not part of the switch.

### The claim, stated so it can be checked

> Moving from Phoenix to Arize AX changes **no instrumentation code**. It changes the
> value of `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` in two places,
> adds one Foundry connection holding the credentials, and is carried to the agent by a
> new agent version whose only diff is those variables.

The first sentence is enforced by a test. The second is a procedure, and #78's writeup
records the exact diff.

---

## Do Foundry's own tracing and our OpenInference spans duplicate?

#13 asks and the answer is **no, they coexist, and the reason is worth keeping.**

Foundry's own tracing is OpenTelemetry with the gen-AI semantic conventions and lands in
Application Insights; that path is not configurable to a third party. Our spans are the
ones `chip_chat.otel` emits, carrying OpenInference conventions and the span names
RFC-001 §09 froze. They travel over the same OTLP wire and land in the same places, but
they answer different questions and are distinguished by name: nothing Foundry emits is
called `tool.search_menu_knowledge` or `retriever.search`, and every eval in `eval/`
attaches to names from our schema. Where they overlap in App Insights, the duplication
is two views of one call rather than two calls.

There is one thing to watch after the switch, and it is the reason
`otel/tests/test_span_tree.py` exists: a trace that arrives as **two traces** — the app's
half and the agent's half unlinked — makes every trajectory and grounding number
meaningless while leaving every span present. `make trace-boundary` is the check, and
`eval/trajectory/BASELINE.md` and `eval/grounding/BASELINE.md` both gate on it.

---

## What was considered and rejected

**App Insights alone.** It answers request rate, container latency, dependency failures
and exceptions. It does not answer *which tool did the model reach for, and what came
back* in a form an eval can attach to, and it has no online-eval automation. The three
things Phase 9 exists to do are the three things it cannot do.

**Arize AX alone, from week one.** Paying a vendor to watch a system nobody else can
reach, during the weeks when the span schema is still moving, for the sake of avoiding a
switch that a test already proves is free. The switch is the cheap part; the four weeks
are not.

**Phoenix alone, self-hosted, through the public phase.** Tempting, and it is the option
that costs nothing. It loses managed monitoring and alerting, which is precisely what
#76 needs *before* the URL is shared, and it puts the observability plane's uptime on
the same container platform as the thing being observed. A monitoring system that goes
down with the product is not a monitoring system.

---

## What this record does not decide

- **Whether AX is purchased.** #78 owns the provisioning and it is currently blocked on
  a purchase decision that belongs to the repository owner. Nothing in this record
  authorises a transaction.
- **The AX region.** See the note above on `ArizeAi.ObservabilityEval` and East US 2.
- **Whether Phoenix's traces are ever persisted.** They are not, deliberately —
  `compose.yaml` has no volume, so `make dev-down` takes the database with it. The
  traces worth keeping come from the deployed app.

## References

RFC-001 D6, D8, §09. System design — Observability, *"Which Arize, though"*, Phase 0,
Phase 9. [`docs/service-inventory.md`](../service-inventory.md) §2.1 and §5, and items 4
and 5 of *What changed versus the plan*. [`docs/local-tracing.md`](../local-tracing.md)
for the local half. [`docs/decisions/foundry-agent-shape.md`](foundry-agent-shape.md)
for why the agent tier costs a version.

## What was confirmed on 25 August 2026, and what was not

Kept from the parallel record written while this one was being written, because the unconfirmed half is the half that goes missing when two documents are merged and somebody keeps the tidier one.


**Confirmed 2026-08-25** (evidence in `docs/service-inventory.md`): the AX tier
structure and quotas above; that Phoenix self-hosts free with no feature gating;
that both speak OpenInference, so the same instrumentation targets both
unchanged.

**Not confirmed, and the ticket's own wording asked for it:** whether the AX
Azure Marketplace listing is genuinely *transactable against this subscription*.
It has not been tested because AX Free needs no transaction, and the session
authority for this project excludes Marketplace purchases. If the demo ever needs
Pro, that question is unanswered.

**Not confirmed either:** whether Foundry's own tracing export and these
OpenInference spans coexist cleanly or duplicate. The deployed app exports only
to Application Insights, so the two have never run side by side.

