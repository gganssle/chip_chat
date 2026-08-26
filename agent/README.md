# `agent/` — the hosted agent

Cilantro's agent is a **hosted agent**: our container, run by Foundry Agent
Service. [`docs/decisions/foundry-agent-shape.md`](../docs/decisions/foundry-agent-shape.md)
records why, and is blunt about the bill:

> And we now ship a container. That is a real build cost, not a paper one: an
> image, a registry, and a deployment step that has to exist before the Phase 7
> demo rather than after it.

This directory is that, plus the agent itself: the eleven tools of RFC-001 §06,
the versioned system prompt, and the loop that runs them.

| Module | What it is |
| --- | --- |
| `surface.py` | The eleven tools of RFC-001 §06 — the *definition* |
| `tools.py` | The subset built so far, running against hardcoded data |
| `prompt.py`, `prompts/` | The system prompt, and the version that follows it into traces |
| `loop.py` | The agent loop: model, tools, model again |
| `definition.py` | What the container assembles: deployment + prompt + eleven tools |
| `envelope.py` | The response format of D9 — citations as a field, not a sentence |
| `orders.py` | Drafts, confirmation, receipts |
| `model.py`, `hardcoded.py`, `testing.py` | The chat-model seam, the slice's data, test doubles |
| `selection.py` | A live probe: which lane does the deployed model pick? |
| `foundry.py` | Where the models are, and which deployment answers for which lane |
| `container.py` | The image's entrypoint: `check` and `agent-half` |
| `version.py` | The hosted agent version manifest, and its registration |
| `verify.py` | Phase 0: prove the chat and vision deployments answer |
| `threads.py` | The thread-retention probe for [#11](https://github.com/gganssle/chip_chat/issues/11) |

---

## The prompt is not load-bearing for security

That is the title of [#60](https://github.com/gganssle/chip_chat/issues/60) and
it is the requirement, not a remark. If `prompts/system-v1.md` were emptied,
Cilantro would become useless and **neither launch gate would fail**.

**Visitor isolation.** No tool signature accepts a visitor identifier. That
absence *is* the mechanism — there is no argument for the model to get wrong and
no field an injected instruction can populate. `surface.ARGUMENT_NAMES` is
derived by walking every schema at every depth, so a parameter added next year
appears in it without anyone remembering; `BoundArguments` validates in
`__post_init__`, so no construction path yields a call carrying a field the spec
never declared; and `tools.dispatch` binds through the surface before any tool
body runs, so this is the live path and not a test fixture. Below all of it, row
access policies ([#43](https://github.com/gganssle/chip_chat/issues/43)) and the
connection pool ([#44](https://github.com/gganssle/chip_chat/issues/44)) enforce
it in Snowflake.

**Confirmation before writes.** Every write tool takes the id of something the
visitor has already been shown, and there is no field on any of them through
which a confirmation could be asserted — no `confirmed`, no `approved`, no
`force`. `OrderDesk` resolves the id against drafts minted for the bound session
and actually confirmed; the ops API
([#63](https://github.com/gganssle/chip_chat/issues/63)) is where that becomes
the real thing.

`tests/test_sabotage.py` demonstrates this rather than asserting it. It loads
`tests/prompts/system-sabotaged.md` — a prompt written to defeat both gates, in
the imperative, with retries under other field names — plays a model that obeys
it exactly, and shows every instruction producing a rejection instead of an
effect. That file is #60's third acceptance criterion; read it before changing
anything in `surface.py`.

---

## The prompt version

`v1+3f2a1b9c8d7e`. The revision is maintained by a person; the digest is the
SHA-256 of the prompt bytes and is maintained by nobody. Two runs whose
`chat.turn` spans carry the same version ran the same bytes, whatever the
revision says — which is what makes "attribute this score change to that prompt"
a true statement in an Arize experiment rather than a hopeful one.

That only works if the prompt is invariant, so it is. **Two system messages, not
one:** `SYSTEM_PROMPT` is the versioned text — the five lanes, the citation rule,
the allergen refusal, the two-step write, retrieved-content-is-data — and
`RUNTIME_CONTEXT` is everything that varies, which today is the persona, the
three-item menu, and which of the eleven tools are actually registered. A digest
that moved because a visitor happened to be called Sam would identify nothing.

The value reaches `chat.turn` from `api/app.py`, as
`chip_chat.prompt.version`; `otel/README.md` is the schema of record for it.

---

## Tool descriptions are load-bearing, and that is deliberate

Tool-selection accuracy is the metric the whole five-lane architecture exists to
get right. A model chooses between `search_menu_knowledge` and
`ask_account_question` by reading their descriptions, not by reading the prompt's
lane section — so the descriptions in `surface.py` are written to separate the
confusable pairs on their own.

```bash
make verify-tools        # twelve cases through the configured chat deployment
make verify-tools-bare   # the same cases with no system prompt at all
uv run python -m chip_chat.agent.selection --deployment gpt-4.1-mini
```

Twelve cases, six of them sitting on a boundary two tools share. Measured
26 August 2026:

| Run | Score |
| --- | --- |
| `gpt-5-mini` (the configured chat deployment), with the prompt | **8/12** |
| `gpt-4.1-mini`, with the prompt | **10/12** |
| `gpt-4.1-mini`, **no system prompt at all** | **11/12** |

Two things fall out of that table, and the second is the awkward one.

**The descriptions carry lane selection on their own.** The best run is the one
with no system prompt in it. That is #60's fourth criterion answered in the
direction it asked for — selection works without prompt gymnastics, and deleting
the prompt does not take the lanes with it.

**The configured chat deployment is the weak link, not the surface.** Same tools,
same prompt, same cases: `gpt-5-mini` loses two full cases to `gpt-4.1-mini`, and
its misses are strange rather than close — it reaches for `get_points_balance` on
a photo upload and on *"remember that I never want cheese"*. Not an API-version
artefact; `2025-04-01-preview` scores the same. Swapping the chat lane is an
environment variable by construction, but doing it would put both lanes on one
deployment and give up the separate TPM pools that
[`docs/phase-0-verification.md`](../docs/phase-0-verification.md) chose two models
for. That trade belongs to whoever owns the model estate: bead `cc-6n5`.

The one miss common to every run is *"redeem my free guac"* going to
`get_points_balance` instead of `redeem_points`, even with both descriptions
naming the boundary explicitly. Worth another attempt before Phase 9 makes it a
number.

---

## Two capabilities are invented, and say so

`cancel_order` and `redeem_points` carry an `invention` note on their `ToolSpec`,
quoting [`docs/action-surface.md`](../docs/action-surface.md) §10 and naming what
removing them would cost.

`cancel_order` is the one to watch. Chipotle's published FAQ refuses cancellation
outright — a submitted order goes straight to the crew — and PRD **T1** requires
the action anyway, so the demo holds orders in a pending state of its own and
`CANCELLATION_REALITY` says out loud that the real product does not work this
way. Its exit path is a PRD change dropping T1's cancellation clause, and that
exit is cheap only while the tool stays separable: `order_id` appears nowhere
else in the surface, and a test keeps it that way. Removing the capability is
deleting one `ToolSpec` and one `OpsAction`.

---

## The thing that will bite: two service names, one trace

A hosted agent's `service.name` is **forced to the agent resource's name**, and
`OTEL_SERVICE_NAME` is ignored. So one visitor turn emits spans under two service
names, split exactly where the process boundary is:

```
chip-chat-api      chat.turn
chip-chat-api      ├─ guard.budget_check
chip-chat-api      ├─ guard.content_safety
                   │     ── traceparent + baggage cross the wire ──
chip-chat-agent    ├─ agent.step
chip-chat-agent    │  ├─ llm.completion
chip-chat-agent    │  └─ tool.<tool_name>
chip-chat-agent    │     └─ retriever.search / vision.describe / ops.<action> / …
chip-chat-api      └─ render.response
```

Two consequences, and neither is optional.

**W3C trace context must propagate**, or the tree above is two unrelated traces.
A split trace is not a degraded trace: it destroys the parent/child structure
every Phase 9 trajectory and tool-selection evaluation attaches to, so those evals
would score nothing rather than score badly. The mechanism is
`chip_chat.otel.propagation`, and both ends of it raise rather than emit half a
turn — see [`otel/README.md`](../otel/README.md#across-the-app-to-agent-boundary).

**Anything filtering on `service.name` must expect both values.** A dashboard,
alert or eval trace query written against one name shows half a turn and looks
healthy doing it. `chip_chat.otel.service.turn_service_names()` returns the pair,
so consumers take the list from one place instead of remembering.

---

## Building the image

From the **repository root** — the agent is a uv workspace member and its
lockfile lives at the root, so `agent/` is not a usable build context:

```bash
make agent-image                  # docker build -f agent/Dockerfile -t chip-chat-agent:dev .
make agent-image-check            # build, then ask the image what it is
```

`check` is what to run against a freshly built image before registering a version
around it:

```
chip-chat agent image · package 0.0.0
  service.name    chip-chat-agent
  the other one   chip-chat-api
  environment     local
  otlp            http://localhost:6006/v1/traces
  app insights    (platform-injected)
  carrier         (none in the environment)
```

The exporter variables are **not** baked into the image. They are immutable per
agent version, they differ between backends, and baking them in would make the
Phase 8 repoint an image rebuild instead of a new version.

### Pushing it

```bash
make infra-output | grep container_registry          # what the registry is called
az acr login --name <registry>
make agent-image-push ACR_LOGIN_SERVER=<registry>.azurecr.io
```

The registry is created by Terraform (`infra/terraform/registry.tf`), its admin
account is disabled, and the push right is a role assignment on your own identity
rather than a password. CI does the same thing over OIDC federation — see
[`.github/workflows/agent-image.yml`](../.github/workflows/agent-image.yml).

---

## Proving the boundary

The claim is *one visitor turn produces one connected trace spanning both service
names*. There are three ways to check it, in increasing order of how much they
prove.

```bash
make test                       # 1. the assertion, in otel/tests/test_propagation.py
make dev && make trace-boundary # 2. one turn, two providers, read it in the backend
make dev && make agent-image-boundary   # 3. the agent half in the REAL container
```

The third runs the app half in your shell and the agent half as
`docker run … chip-chat-agent:dev agent-half`, with the carrier handed over in
`TRACEPARENT` / `TRACESTATE` / `BAGGAGE`. Two operating-system processes, two
tracer providers, two service names — and one trace, or it is broken.

What you should see, whichever way you look:

```
chat.turn
  guard.budget_check
  guard.content_safety
  agent.step
    llm.completion
    tool.search_menu_knowledge
      retriever.search
  agent.step
    llm.completion
  render.response
```

A `chat.turn` with three children and an orphaned `agent.step` beside it is the
split trace. The carrier did not reach the agent.

---

## The agent version

A hosted agent is a *versioned* resource: you register a version — an image
reference plus an environment — and Foundry runs that. The environment variables
are **immutable per agent version**.

```bash
make agent-version ACR_LOGIN_SERVER=<registry>.azurecr.io    # print the manifest
uv run python -m chip_chat.agent.version register --image <registry>/chip-chat-agent@sha256:…
```

```json
{
  "name": "chip-chat-agent",
  "kind": "hosted",
  "container": { "image": "…/chip-chat-agent@sha256:…" },
  "environment_variables": [
    { "name": "CHIP_CHAT_AGENT_NAME", "value": "chip-chat-agent" },
    { "name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "…" },
    { "name": "OTEL_EXPORTER_OTLP_HEADERS",
      "value": "${{connections.otel-secrets.credentials.otlp_headers}}" },
    { "name": "OTEL_EXPORTER_OTLP_PROTOCOL", "value": "http/protobuf" }
  ]
}
```

Three properties are enforced rather than documented:

- **The OTLP headers are always a connection reference, never a value.** They
  carry an API key and a space id for the hosted backend, and the supported way
  to supply one is a CustomKeys connection on the project. There is no code path
  that puts a literal there, so the manifest is safe to print, paste and commit.
- **A moving tag is refused, and `register` requires a digest.** An agent version
  pointing at `:latest` is not a version, and a trace it produced could not be
  attributed to a build.
- **An empty `OTEL_EXPORTER_OTLP_ENDPOINT` is refused.** Registering without it
  yields an agent that exports to Application Insights only, and correcting that
  costs a whole version.

This is also the shape of the Phase 8 switch
([#78](https://github.com/gganssle/chip_chat/issues/78)): register a second
version whose *only* diff is those variables and the connection behind them. No
instrumentation code changes — which is the claim actually worth proving, and the
one the RFC now states.

**On `register` specifically.** The call is written from the published Agents
data-plane shape and is not exercised by this repository's tests; it prints the
service's answer verbatim so the first run tells you the truth rather than a
wrapper's summary of it. `render` is the part with logic in it, and the part that
is tested. Same honesty `threads.py` applies to the retention probe.

---

## Verifying the model deployments

Unchanged from Phase 0, and not part of `make ci` because it costs tokens and
needs a logged-in human:

```bash
make verify-models        # both lanes
make verify-chat
make verify-vision
make verify-tools         # and lane selection, above
```

See [`docs/phase-0-verification.md`](../docs/phase-0-verification.md) for what
those runs established, including why the vision check's image is 768 pixels.

---

## Verified

2026-08-26. `make agent-image` produced a 59 MB image on Docker 29.5.2; the
boundary run with the agent half in that container landed **one** trace of ten
spans in Phoenix `version-20.3.0`, carrying `chip-chat-api` and
`chip-chat-agent`, nested as RFC-001 §09 describes.
