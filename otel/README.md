# `otel/` — the span schema of record

This package is the one shared library in the monorepo. Every other package may
import it; it imports none of them, and the import-linter contract in the root
`pyproject.toml` enforces that structurally.

Its job is not "some tracing helpers". Its job is a **schema**. RFC-001 §09 fixes
the span tree a turn emits, Phase 9's evaluations and every dashboard axis attach
to those names, and a rename is therefore a breaking change to consumers that
live outside this repository. Instrumenting last is one of the seven failure
modes the build plan calls out by name; this package exists so that it cannot
happen here.

**If you are about to add a span, change a name, or add an attribute, this file
and `src/chip_chat/otel/schema.py` are the two places that have to agree.**

## The tree

```
chat.turn                    root span, one per visitor message
├─ guard.budget_check        synchronous; may terminate the turn
├─ guard.content_safety
├─ agent.step                one per model round trip
│  ├─ llm.completion         tokens, model, finish reason
│  └─ tool.<tool_name>       one per call, arguments recorded
│     ├─ retriever.search    documents + scores
│     ├─ db.cortex_analyst   generated SQL + row count
│     ├─ vision.describe     image ref + structured output
│     ├─ matcher.resolve     slot confidences + resolved SKUs
│     └─ ops.<action>        draft id + confirmation state
└─ render.response
```

Nesting is enforced, not documented. Each helper checks its position before it
opens a span and raises `SpanSchemaError` if a call site would have produced a
tree the RFC does not describe. `llm.completion` outside an `agent.step` is a
failed test, not a slightly odd trace.

### `tool.<tool_name>`

One span per call, from the eleven tools of RFC-001 §06. The parameter is a
`ToolName`, never a string, so a typo is a failed import rather than a span
nobody's dashboard is watching.

| Span | Tool |
| --- | --- |
| `tool.search_menu_knowledge` | AI Search over the harvested corpus |
| `tool.ask_account_question` | Cortex Analyst |
| `tool.get_points_balance` | Snowflake |
| `tool.get_usual_order` | Gold mart |
| `tool.get_recommendations` | Gold mart |
| `tool.match_meal_from_photo` | Vision + matcher |
| `tool.propose_order` | App |
| `tool.place_order` | Ops API (write, confirmed) |
| `tool.cancel_order` | Ops API (write, confirmed) |
| `tool.redeem_points` | Ops API (write, confirmed) |
| `tool.update_preferences` | Ops API (write, confirmed) |

### `ops.<action>`

The four writes, each nested inside its tool span: `ops.place_order`,
`ops.cancel_order`, `ops.redeem_points`, `ops.update_preferences`.

Confirmation is enforced by the ops API rather than by the prompt, so
`chip_chat.ops.confirmation_state` is the attribute an eval reads to catch an
agent that tried to write against something the visitor never confirmed. A
`rejected` state also sets the span status to error, because that is a
launch-gate violation and it should not look like a success.

## Using it

```python
from chip_chat.otel import (
    OpsAction,
    ToolName,
    agent_step,
    budget_check,
    chat_turn,
    llm_completion,
    ops_write,
    render_response,
    retriever_search,
    tool_call,
)

with chat_turn(session_id=sid, turn_index=n, message=text) as turn:
    with budget_check() as guard:
        guard.record_budget(scope="session", tokens_used=used, tokens_limit=cap)
        guard.allow()

    with agent_step(index=0):
        with llm_completion(model="gpt-4o", provider="azure") as llm:
            llm.record_usage(prompt_tokens=812, completion_tokens=64)
            llm.record_finish_reason("tool_calls")

        with tool_call(ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": q}) as tool:
            with retriever_search(query=q) as search:
                search.record_documents(documents)
            tool.record_result(passages)

    with render_response() as render:
        render.record_output(reply)
    turn.record_output(reply)
```

No tracer is exported from this package. That is deliberate: a tracer is a
free-form span-name factory, and handing one to a call site is how the schema
would quietly stop being one. Attributes are set through the recorder each helper
yields, so the same holds for the attribute namespace.

Anything genuinely outside the schema goes through `set_metadata(**values)`,
which lands in OpenInference's `metadata` key — the one place free-form data
belongs, and out of the namespace the evals are built on.

## Attributes

Three sources, in strict order of precedence.

**1. OpenInference.** These are what make Arize and Phoenix read a span as an LLM
call, a retrieval or a tool invocation rather than as an anonymous unit of work.
Where OpenInference defines a name, we use it and offer no alternative:
`openinference.span.kind`, `llm.model_name`, `llm.token_count.*`,
`llm.finish_reason`, `llm.input_messages.*`, `llm.tools.*`, `tool.name`,
`tool.parameters`, `retrieval.documents.*`, `session.id`, `user.id`,
`input.value`, `output.value`, `metadata`, `tag.tags`.

**2. OpenTelemetry's database conventions**, for `db.cortex_analyst`:
`db.system`, `db.query.text`, `db.response.returned_rows`. OpenInference has
nothing to say about SQL and these names already exist.

**3. `chip_chat.*`**, for the handful of facts neither standard covers — turn
index, guard outcomes, budget scope, matcher slot confidences, ops confirmation
state, and the system prompt version. All namespaced, so a backend can tell at a
glance which attributes are portable and which are ours.

Every span in a turn carries `session.id`, `chip_chat.turn.index` and (when
bound) `chip_chat.persona.id` and `chip_chat.demo.id` — not only the root.
Application Insights searches attributes far more comfortably than it walks trace
trees, and "it did something weird" arrives with a session id at best.

### `chip_chat.prompt.version`

On `chat.turn` and nowhere else, because it is a property of the turn rather than
of the identity stamped on every span. Pass it as `chat_turn(...,
prompt_version=definition.prompt_version)`; the value comes off the prompt that
was actually loaded, so it cannot drift from the text the model was given.

Its shape is `v1+3f2a1b9c8d7e` — a maintained revision and an unmaintained digest
of the prompt bytes. An Arize experiment groups on it to attribute a score change
to a specific prompt, and the digest is what makes *specific* true when someone
edits the text without bumping the revision. See
`agent/src/chip_chat/agent/prompt.py`.

### Two token vocabularies, and why

**`llm.token_count.*` belongs to model calls.** Every span the schema types as an
LLM — `llm.completion` *and* `vision.describe` — records the counts the provider
reported, through `record_usage`. The counts are carried off the response and
never estimated: a number this package invented would still add up, and the sum
would mean nothing.

**`chip_chat.tokens.*` belongs to spans that merely contain model calls** —
`chat.turn`, `agent.step`, `tool.<tool_name>` — through `record_token_rollup`.

Keeping them apart is load-bearing rather than tidy. Sum `llm.token_count.*`
across a trace and you get exactly what the providers charged for that turn;
merge the two and every ancestor is counted a second time, the figure silently
doubles, and nothing about the dashboard looks wrong.

```python
spans.assert_token_counts_sum(TokenUsage(prompt_tokens=2_436, completion_tokens=192))
```

is that property in executable form, and it fails on both halves: an LLM span
that recorded no counts at all, and counts that are present but disagree with
what the provider said. `api/tests/test_turn_trace.py` runs it over a real turn.

The rollups exist because Application Insights does not walk trace trees. "What
did this conversation cost" and "what does the photo lane cost per call" are one
attribute lookup with them and a tree walk without.

### The photograph on `vision.describe`

The image ref rides twice, deliberately. `chip_chat.vision.image_ref` is what an
operator greps. OpenInference's message-contents layout —
`llm.input_messages.0.message.contents.0.message_content.image.image.url` — is
what makes Phoenix and Arize render the span as a vision call with an image
attached rather than as an LLM span carrying an opaque string. Searchable and
legible are different jobs.

A **reference** in both places, never the bytes and never a data URI. RFC-001
section 07; a trace is not an image store.

### `demo_id` is not an identity input

`demo_id` is the row-level security key inside Snowflake. On a span it is an
**opaque correlation attribute and nothing else**: it exists so a bug report can
become a trace. Never read it back off a span to make an authorisation decision.
No helper in this package takes a decision from it, and none should.

## Two backends, one instrumentation

Application Insights answers *is the service healthy*. Phoenix — later Arize AX —
answers *is the agent behaving*. They are not overlapping purchases, and both
consume OpenTelemetry spans carrying OpenInference conventions, so there is one
instrumentation and a fan-out.

Decision D6 says the agent-observability vendor is a configuration value. That
claim has to be true rather than intended, so `exporters.py` names no vendor at
all: there is one OTLP slot and one Application Insights slot, and which product
answers on the OTLP endpoint is none of this package's business. Moving from
Phoenix to Arize AX changes an endpoint and a header. `test_export_configuration.py`
asserts it, including a test that fails if a product name ever appears in
`exporters.py`.

| Variable | Meaning |
| --- | --- |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Full traces URL, used verbatim |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Base URL; `/v1/traces` is appended |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | `k=v,k=v`; falls back to the row below |
| `OTEL_EXPORTER_OTLP_HEADERS` | `k=v,k=v` — where an API key or space id goes |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Enables the App Insights exporter |
| `CHIP_CHAT_ENVIRONMENT` | `deployment.environment`; defaults to `local` |
| `CHIP_CHAT_OTEL_CONSOLE` | Truthy adds a console exporter |

Every slot is optional. A configuration with no exporters is valid and useful:
spans are still built and still schema-checked, they simply go nowhere.

## Across the app-to-agent boundary

Decision D8 put the agent in its own container, so the tree above is emitted by
**two processes**, under **two `service.name` values**:

```
chip-chat-api      chat.turn, guard.budget_check, guard.content_safety, render.response
chip-chat-agent    agent.step, llm.completion, tool.*, and every child of a tool
```

Nothing joins those halves by itself — a second process that simply starts
tracing starts a *second trace*. `chip_chat.otel.propagation` is what joins them,
and both ends of it raise rather than emit half a turn, because a split trace is
not a degraded trace: the parent/child structure is what Phase 9's trajectory and
tool-selection evaluations read, and two unrelated traces do not score badly,
they score nothing.

```python
from chip_chat.otel import continue_turn, turn_context_headers

# In the app, inside the open chat.turn:
headers = turn_context_headers()
response = http.post(agent_url, json=payload, headers=dict(headers))

# In the agent container, on the way in:
with continue_turn(request.headers):
    with agent_step(index=0):
        ...
```

Two things travel and they are different. **W3C trace context** (`traceparent`,
`tracestate`) makes the agent's spans children of the app's `chat.turn`.
**Baggage** carries the turn's identity — `session.id`, `chip_chat.turn.index`,
`chip_chat.persona.id`, `chip_chat.demo.id` — under the same keys those values
are stamped on spans, so the promise above ("every span in a turn carries the
session id, not only the root") survives a process boundary.

The propagator is built explicitly rather than taken from
`opentelemetry.propagate`: the global one is assembled from `OTEL_PROPAGATORS`,
and an environment variable set for an unrelated reason should not be able to
drop half a turn.

`resume_turn` is the lower half of `continue_turn` — it restores the turn context
without touching the carrier — and it exists because the nesting check is a
context variable, and a context variable does not cross a process boundary. It is
not a thirteenth node of the schema and it opens no span.

### Both service names, in one place

Anything filtering or grouping on `service.name` must expect **both** values, or
it shows half a turn and looks healthy doing it. So the pair is enumerated rather
than remembered:

```python
from chip_chat.otel import turn_service_names

app, agent = turn_service_names()  # ("chip-chat-api", "chip-chat-agent")
```

The agent's name is read from `CHIP_CHAT_AGENT_NAME` rather than derived, because
it is not ours to derive: Foundry forces `service.name` to the agent *resource's*
name and ignores `OTEL_SERVICE_NAME`. The default is what the resource should be
called precisely so that the constraint costs nothing — but a dashboard built on
"should be" is the failure this variable exists to prevent.

### Seeing it

`chip_chat.otel.boundary` emits one turn the way the deployed system will:

```bash
make trace-boundary          # two providers in one process
make agent-image-boundary    # the agent half in the real container
```

Expect **one** trace of ten spans, with the service name changing in the middle
and changing back. `otel/tests/test_propagation.py` and
`otel/tests/test_boundary.py` are the same claim without a backend in the way.

### This package does not ride on Foundry's built-in tracing

Worth stating, because it is the first thing a reader wonders. The spans above
are emitted by *this* package, through its own `TracerProvider`, from whichever
process imports it. Where the agent's own tracing can be exported to is a
separate question — a Foundry prompt agent's traces reach Application Insights
and stop there, while a hosted agent can be pointed at a third-party OTLP
endpoint through environment variables — and it constrains the agent's shape
rather than this schema. Our `llm.completion` spans wrap the model call from the
outside and reach both backends either way.

The consequence is real but belongs downstream: under a prompt agent, Foundry's
own server-side spans would not appear alongside ours in Phoenix, so the trace
would hold our view of the model call and not Foundry's. That is an input to the
agent-shape decision in #16, not a reason to change a span name here.

Wire it up once, at start-up:

```python
from chip_chat.otel import TelemetryConfig, configure_tracing

configure_tracing(TelemetryConfig.from_env("api"))
```

Local development points `OTEL_EXPORTER_OTLP_ENDPOINT` at a Phoenix container.
`make dev` brings it up and sends a session through it;
[`docs/local-tracing.md`](../docs/local-tracing.md) is the loop.

## Testing your own spans

`chip_chat.otel.testing` ships with the package rather than living in
`otel/tests`, because the packages that arrive later need to make the same
assertions about their own turns — and a contract test is only a contract if both
sides can run it.

```python
from chip_chat.otel.testing import span_recorder

with span_recorder() as spans:
    run_one_turn()

assert (
    spans.tree_text()
    == "chat.turn\n  guard.budget_check\n  agent.step\n    llm.completion"
)
assert spans.attributes_of("llm.completion")["llm.token_count.total"] == 876
```

`tree_text()` is the assertion that fails when somebody renames a span, which is
the entire reason the schema is worth writing down.

## A session you can send anywhere

`chip_chat.otel.smoke` builds three turns out of nothing -- no model is called, no
service is contacted, every value in it is invented -- which between them emit
every span name above, nested as above.

```bash
make trace                                                       # to the local stack
CHIP_CHAT_OTEL_CONSOLE=1 uv run python -m chip_chat.otel.smoke   # to your terminal
```

It ships with the package for the same reason `testing.py` does. The schema is
consumed outside this repository, and pointing this module at a backend is how you
find out whether the collector, the exporter and the schema still agree. It is
also how decision D6 gets *checked* rather than asserted: repointing at Arize AX
(#78) should produce this same tree from the same code, and running it against the
new endpoint is the observation that says so.

## Not in scope

No agent, no tools, no product logic. This is the instrumentation library only --
`smoke.py` and `boundary.py` are fixtures of the schema and not exceptions to
that. #15 put Phoenix in the dev loop around it, #103 added the second process
the schema now spans, and #64 wired the whole of it through the real agent --
which is where the token vocabularies above and the multimodal photo span came
from, and where `api/tests/test_turn_trace.py` began asserting that the
application emits this tree rather than that the tree can be emitted.

`boundary.py` is the one file here that will shell out, and only when
`--agent-command` asks it to: that is how the demo runs the *real* agent
container as the second process instead of pretending with a second provider. It
is a CLI fixture, like `smoke.py`, not something a call site imports.
