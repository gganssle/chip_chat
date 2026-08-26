# `agent/` — the hosted agent

Cilantro's agent is a **hosted agent**: our container, run by Foundry Agent
Service. [`docs/decisions/foundry-agent-shape.md`](../docs/decisions/foundry-agent-shape.md)
records why, and is blunt about the bill:

> And we now ship a container. That is a real build cost, not a paper one: an
> image, a registry, and a deployment step that has to exist before the Phase 7
> demo rather than after it.

This directory is that. The agent's loop and its eleven tools arrive with
[#60](https://github.com/gganssle/chip_chat/issues/60); what exists here is the
image, the version manifest, and the piece of the observability plane that only
a container boundary makes necessary.

| Module | What it is |
| --- | --- |
| `foundry.py` | Where the models are, and which deployment answers for which lane |
| `container.py` | The image's entrypoint: `check` and `agent-half` |
| `version.py` | The hosted agent version manifest, and its registration |
| `verify.py` | Phase 0: prove the chat and vision deployments answer |
| `threads.py` | The thread-retention probe for [#11](https://github.com/gganssle/chip_chat/issues/11) |

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
```

See [`docs/phase-0-verification.md`](../docs/phase-0-verification.md) for what
those runs established, including why the vision check's image is 768 pixels.

---

## Verified

2026-08-26. `make agent-image` produced a 59 MB image on Docker 29.5.2; the
boundary run with the agent half in that container landed **one** trace of ten
spans in Phoenix `version-20.3.0`, carrying `chip-chat-api` and
`chip-chat-agent`, nested as RFC-001 §09 describes.
