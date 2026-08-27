# Tracing locally

Phoenix runs in the local stack from week one, and the reason is in the build
plan: *tracing is not a Phase 9 deliverable you add once things work — it is how
you find out why they don't.* Phoenix is Apache-2 and self-hosted, so the whole
cost of having it from day one is one container.

The loop, in full:

```bash
make dev        # start Phoenix, wait for it, send one instrumented session
open http://localhost:6006
make trace      # send another session whenever you want one
make dev-down   # stop it and throw the traces away
```

`make dev` is the only command a fresh clone needs. It brings the stack up, waits
for the container to report healthy rather than sleeping and hoping, and then
sends three turns through it, so the UI has something in it the first time you
open it.

## What you need

Docker, running. Everything else comes from `uv`, which `make` already drives.

```bash
docker version        # a Server section means the daemon is up
```

On a Mac that means Docker Desktop is started. `make dev` fails with *"failed to
connect to the docker API"* if it is not, which is the whole diagnosis.

## Reading the span tree

Open <http://localhost:6006>, pick the `default` project, and you are looking at
three traces. `make trace` prints the session id it used — paste it into the
filter box to find your own run again after a few of them:

```
session smoke-88274916607d
```

Each trace is one `chat.turn`. The third is the most interesting, because it goes
all the way from a photograph to a confirmed write:

```
chat.turn                          the visitor's message in, the reply out
├─ guard.budget_check              which ceiling was checked, and how close it is
├─ guard.content_safety            image moderation, before inference
├─ agent.step                      round trip 0
│  ├─ llm.completion               model, tokens, finish reason
│  └─ tool.match_meal_from_photo   what the whole lane cost
│     ├─ vision.describe           the photograph, its slots, and its tokens
│     └─ matcher.resolve           what those slots resolved to in the catalogue
├─ agent.step                      round trip 1 — propose
│  ├─ llm.completion
│  └─ tool.propose_order
├─ agent.step                      round trip 2 — write
│  ├─ llm.completion
│  └─ tool.place_order
│     └─ ops.place_order           draft id and confirmation state
└─ render.response                 what the visitor actually saw
```

That is [RFC-001 section 09](rfc-001.md#observability) exactly, and it is asserted
in `otel/tests/test_smoke.py` rather than left to the eye. **If what you see in
Phoenix disagrees with the RFC, the bug is in `otel/` and belongs fixed there** —
not patched around in whatever you were instrumenting at the time.

Two things about the photo lane specifically. `vision.describe` is an **LLM
span** — Phoenix renders it as a model call with the photograph attached, and it
carries token counts like any other, because the photo lane is the expensive
one. And both of its halves sit under **one** `tool.match_meal_from_photo`: run
stage 4 and stage 5 as separate tool calls and you get two well-formed traces
that no longer answer *what did the lane make of this photograph* in one place.

Adding up the tokens: sum `llm.token_count.total` across the LLM spans and you
have exactly what the providers charged for the turn. `chip_chat.tokens.total`
on `chat.turn` says the same number without the walk, which is what Application
Insights needs — it searches attributes and does not walk trace trees. The two
vocabularies are separate on purpose; `otel/README.md` explains why.

Three things worth noticing while you are in there:

- **Phoenix labels the spans by kind** — LLM, retriever, tool, guardrail, agent,
  chain. That labelling is what makes Phoenix and Arize read a trace as an agent
  run instead of an anonymous pile of work, and it comes from the OpenInference
  attributes `otel/` sets. It is not something Phoenix inferred from the names.
- **Every span carries the session id and turn index**, not just the root. A bug
  report arrives with a session id at best, and searching attributes is far
  easier than walking trace trees.
- **`vision.describe` is an LLM span and `matcher.resolve` is a chain.** The
  vision model is scored as a model call; the matcher is deterministic and is
  not. The difference is in the schema on purpose.

## What the demo session is

`chip_chat.otel.smoke` builds three turns out of nothing — no model is called, no
service is contacted, every value in it is invented. Between them they emit every
span name in the schema, which is what makes them useful for wiring up a backend:
you see every span shape the backend will ever have to render, rather than the
subset that happens to be on the path you were debugging.

```bash
make trace                                   # to the local stack
CHIP_CHAT_OTEL_CONSOLE=1 uv run python -m chip_chat.otel.smoke   # to your terminal
```

It is a fixture of the schema, not a preview of the product. The agent, its tools
and the turns they really run arrive in [#16](https://github.com/gganssle/chip_chat/issues/16)
and [#64](https://github.com/gganssle/chip_chat/issues/64), and none of them
import it.

## One turn, two processes, one trace

Decision D8 made the agent a hosted agent — our container, run by Foundry — so
the tree above is emitted either side of a process boundary and under **two**
`service.name` values. The app emits `chat.turn`, the guards and
`render.response`; the agent container emits everything between.

```bash
make trace-boundary          # two tracer providers in this process
make agent-image-boundary    # the agent half in the REAL container
```

Both send one turn, and both should produce **one** trace in Phoenix:

```
chat.turn                          chip-chat-api
├─ guard.budget_check              chip-chat-api
├─ guard.content_safety            chip-chat-api
├─ agent.step                      chip-chat-agent
│  ├─ llm.completion               chip-chat-agent
│  └─ tool.search_menu_knowledge   chip-chat-agent
│     └─ retriever.search          chip-chat-agent
├─ agent.step                      chip-chat-agent
│  └─ llm.completion               chip-chat-agent
└─ render.response                 chip-chat-api
```

**If you see two traces** — a `chat.turn` with three children, and an orphaned
`agent.step` beside it — the W3C trace context did not cross. That is the failure
this exists to catch, and it is worth catching here rather than in Phase 9: a
split trace is not a degraded trace, it destroys the parent/child structure every
trajectory and tool-selection evaluation attaches to. The command prints the
headers that were on the wire, which is the first thing to look at.

That evaluation now exists — `chip_chat.eval.trajectory`, issue #74 — and it is
built to survive this rather than to be fooled by it: a turn that arrives as two
traces is scored as **unscored**, counted as a split trace, and named with this
page's command beside it. So a broken boundary shows up as a hole in the report
rather than as a plausible number over half a tree.

`chip_chat.eval.grounding` (issue #75) does the same and has one more reason to.
It reads the `retriever.search` documents off the trace so that a groundedness
judge scores against what the turn really retrieved — and when the turn splits,
the retrieval is in one trace and the response is in the other, so nothing can
show that the passages belong to the answer. Not a degraded score: no score.

`make agent-image-boundary` is the stronger of the two. It builds the image and
runs the agent half as `docker run … chip-chat-agent:dev agent-half`, handing the
carrier over in `TRACEPARENT` / `TRACESTATE` / `BAGGAGE`. Two operating-system
processes, and still one trace — or the boundary does not work.

While you are in Phoenix, notice that **`service.name` changes in the middle of
the trace and changes back**. Anything you later filter or group on that
attribute has to expect both values; `chip_chat.otel.service.turn_service_names()`
returns the pair so nothing has to remember them. `otel/README.md` has the
mechanism and `agent/README.md` has the container.

## Tracing your own code

Configure once at start-up and then use the span helpers. The configuration call
reads the environment; it takes no endpoint argument and there is nothing to pass
it:

```python
from chip_chat.otel import TelemetryConfig, chat_turn, configure_tracing

configure_tracing(TelemetryConfig.from_env("api"))

with chat_turn(session_id=sid, turn_index=n, message=text) as turn:
    ...
```

`otel/README.md` is the schema of record and shows the rest of the helpers. Two
things that will otherwise cost you twenty minutes:

- **Flush before the process exits.** Spans go out through a batch processor, so
  a short-lived script that exits immediately looks exactly like a backend that is
  not listening. `shutdown_tracing()` flushes; `chip_chat.otel.smoke.main` calls it
  in a `finally`.
- **No exporter configured is a valid state.** Spans are still built and still
  schema-checked, they simply go nowhere. That is what tests want and what a
  script run without `OTEL_EXPORTER_OTLP_ENDPOINT` gets. `make trace` refuses to
  run in that state, because a smoke test that quietly exported nowhere is worse
  than a failure.

## Phoenix is configuration, not code

Nothing in this repository imports Phoenix, names Phoenix, or branches on whether
Phoenix is what is listening. `otel/exporters.py` has one OTLP slot, and which
product answers on it is none of its business — there is a test that fails if a
product name ever appears in that file.

That is decision [D6](rfc-001.md#openinference-over-otel-dual-export-phoenix-then-arize-ax),
and it has to be *true* rather than intended, because
[#78](https://github.com/gganssle/chip_chat/issues/78) has to be able to show
later that repointing at Arize AX was a configuration change and nothing else:

| Variable | Local (what `make dev` sets) | Arize AX |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:6006` | the AX endpoint |
| `OTEL_EXPORTER_OTLP_HEADERS` | unset | `api_key=…,space_id=…` |

Running `make trace` against the new endpoint and seeing the same tree is how
that claim gets checked. Nothing else changes — not a line of the exporter, not a
span name, not a call site.

The endpoint has a default and no more than that. `PHOENIX_PORT` moves the
container's published port and the endpoint follows it, and an exported
`OTEL_EXPORTER_OTLP_ENDPOINT` beats both:

```bash
make dev PHOENIX_PORT=6007                                    # move the stack
make dev PHOENIX_GRPC_PORT=4318                               # move only the gRPC port
OTEL_EXPORTER_OTLP_ENDPOINT=https://elsewhere make trace      # send elsewhere
```

Because none of this is read from a file, `.env` plays no part unless you export
it yourself — see [`.env.example`](../.env.example), which says so.

## Traces do not survive a restart

Deliberately. There is no volume in `compose.yaml`, so Phoenix's database lives
inside the container and `make dev-down` takes it with it. This is a dev loop, not
a store: the traces worth keeping are the ones from the deployed app, and those go
to Arize AX and Application Insights.

If you want a clean slate mid-session, `make dev-down && make dev` is it.

## When it does not work

| Symptom | What it is |
| --- | --- |
| `failed to connect to the docker API` | The daemon is not running. Start Docker Desktop. |
| `Bind for 0.0.0.0:6006 failed: port is already allocated` | Something else has 6006 — often an older Phoenix. `docker ps`, then `make dev-down`, or `make dev PHOENIX_PORT=6007`. |
| The same, on 4317 | An OpenTelemetry collector, usually. The exporter here speaks HTTP and does not need that port: `make dev PHOENIX_GRPC_PORT=4318`. |
| `make dev` hangs at `Container chip-chat-phoenix Waiting` | The health check never passed. `make dev-logs` says why; the first run also has to pull the image, which is 1.5GB. |
| "No exporter is configured" | `OTEL_EXPORTER_OTLP_ENDPOINT` is empty. `make trace` sets it; a bare `python -m chip_chat.otel.smoke` does not. |
| `make trace` succeeds, Phoenix stays empty | Check the project selector — everything lands in `default` — and then that the endpoint is the one the stack is on. The exporter logs a connection failure to stderr; it does not fail the process. |
| The tree is not the one above | `otel/` is wrong, or your call sites are. Run `make test`; `otel/tests/test_smoke.py` is the same assertion without a container in the way. |
| `make trace-boundary` produces **two** traces | The trace context did not cross. The command prints the headers it sent; if `traceparent` is missing, nothing opened a span before injection. `otel/tests/test_propagation.py` is the same assertion without a backend in the way. |
| `make agent-image-boundary` fails with "the agent command exited …" | The container did not run. `docker run --rm chip-chat-agent:dev check` on its own says what the image thinks it is. |

## Verified

2026-08-26, on Docker 29.5.2 with Compose v5.1.4 and
`arizephoenix/phoenix:version-20.3.0`. `make dev` brought the container to healthy
and sent three turns; all thirty-six spans arrived, and the three trees read back
out of Phoenix's API match RFC-001 section 09 node for node.

The boundary, the same day and on the same stack: the app half in a shell and the
agent half in the built `chip-chat-agent` image. Phoenix returned **one** trace of
ten spans carrying both `chip-chat-api` and `chip-chat-agent`, nested as above.
