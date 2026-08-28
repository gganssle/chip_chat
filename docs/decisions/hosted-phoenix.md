# Decision: Phoenix is hosted here, Arize AX is not bought, and what that costs

**Issues:** [#76](https://github.com/gganssle/chip_chat/issues/76) (online evals live), [#78](https://github.com/gganssle/chip_chat/issues/78) (repoint the exporter) · **Decided:** 28 August 2026, by the repository owner
**Deviates from:** [`observability-backend.md`](observability-backend.md), which chose Arize AX for the public phase
**Does not change:** RFC-001 [D6](../rfc-001.md#openinference-over-otel-dual-export-phoenix-then-arize-ax) — one instrumentation, the backend a value in the environment. That claim is what made this deviation cost nothing in code.

---

## What was planned, and what was decided instead

The plan was **Arize AX for the public phase**. `observability-backend.md` argues
it at length and the argument is still good: AX brings managed online-eval
scheduling, managed monitoring and alerting, and a dataset and experiment UI, and
its Free tier is \$0 at 25,000 spans a month — comfortably above what a shared
link produces. `arize-switch.md` did everything up to the transaction and
recorded the exact diff the switch would be.

It was never bought. **AX Free is free but not free of a signup**: it wants an
account created at arize.com with an email address, a password and a Terms of
Service acceptance, and that is a contract entered on somebody's behalf rather
than a resource provisioned in their subscription. The repository owner's
instruction for this session was **free tier only, never converting to paid, and
no third-party signup**.

So: **Phoenix, self-hosted, in the Container Apps environment the app already
runs in.** It is the same vendor's open-source backend, it consumes the same
OpenInference spans over the same OTLP wire, `compose.yaml` has been running the
same pinned image locally since week one, and it costs a container. The
alternative to a decision here was not "wait for the purchase" — it was PRD §12's
launch criterion, *online evals and cost monitors are live before the URL is
shared*, staying at **fail** indefinitely.

**This is a deviation and is recorded as one.** `observability-backend.md`
explicitly considered and *rejected* self-hosted Phoenix through the public
phase. That record is not wrong and is not superseded; its two objections are
real and are answered — partly, and at a price — under *What is lost* below.

---

## What is lost

The honest section, and the reason this record exists rather than a commit
message saying "point the exporter at Phoenix".

**AX's managed online-eval scheduling.** AX runs evaluators against your spans
on its own schedule, in its own infrastructure, with its own retries and its own
UI for reading the results over time. Self-hosted Phoenix does not, and nothing
in this repository was going to grow one. What replaces it is a **Container Apps
job on a cron expression** running `python -m chip_chat.eval.online` every
fifteen minutes — which is a scheduler, and is genuinely enough, but it is a
scheduler somebody here now owns. When it stops running, nobody is paged; the
symptom is a job whose last successful execution is old, and noticing that is a
thing a human has to do. AX would have made that its problem.

**AX's dataset and experiment UI.** `eval/dataset` builds a dataset and
`chip_chat.eval.dataset.store` uploads it *through the Arize SDK*, to a space
that does not exist. `make dataset-upload` is therefore a command with nowhere
to go, and the experiment comparisons in `eval/experiments/results/*.json` stay
files in a repository rather than runs you can put side by side in a browser.
Phoenix has datasets and experiments of its own and they are not wired up; doing
so would be a second piece of work, not a smaller version of this one.

**Managed alerting.** AX would route a finding to a channel. Here, the route is
the scheduled job's **exit status** — `--fail-on page` fails the execution when a
cross-visitor disclosure signal fires, which is visible in
`az containerapp job execution list` and is something Azure Monitor can be made
to alert on. That is a real route and it is one step from a real alert; it is not
a delivered alert today, and saying it is would be the kind of claim this
document exists to avoid.

**The objection `observability-backend.md` raised, which stands.** *"It puts the
observability plane's uptime on the same container platform as the thing being
observed. A monitoring system that goes down with the product is not a monitoring
system."* That is exactly what has been built, and the mitigation is partial:
**Application Insights is still receiving every span**, out of the same tracer
provider, so the platform-level question — *is the service up* — is answered by
something that does not share a Container Apps environment with the app. What is
shared is the *agent-behaviour* half. If `cae-chip-chat` has a bad day, the
monitors go quiet at the same moment the thing they watch does.

**Trace history.** AX Free's fifteen-day retention was a constraint written into
the plan and was considered acceptable. What is here is shorter and less
predictable: the traces live in the Phoenix container and a restart takes them.
The archive is Application Insights, which has all the same spans for thirty days
in a shape you cannot read as a tree. The argument for that trade, and the
measurement behind it, are in *Persistence* below — it is the one place where
what was built is materially thinner than what AX would have given, and it is not
a thing to discover from an empty screen.

---

## The four things that had to be got right, and what was chosen

### 1. The version is the one `compose.yaml` uses

`arizephoenix/phoenix:version-20.3.0`, in both places, and
`infra/tests/test_local_stack.py::test_the_deployed_backend_is_the_same_version_as_the_dev_loops`
fails if they ever drift. A dev loop and a deployment that disagree about the
backend's version are worse than either alone: every difference you then see
between a local span tree and a production one has two candidate causes instead
of one, and the second is invisible. Bump both in one commit or neither.

### 2. Persistence: traces are ephemeral, and this is the measurement that decided it

**The decision is that traces do not survive a restart, and it is a decision
rather than a default.** The obvious thing was built first, deployed, and found
not to work, which is why this section is long: the next person to look at an
empty Phoenix after a restart will reach for exactly the thing that was tried.

Phoenix keeps a SQLite database under `PHOENIX_WORKING_DIR`. `compose.yaml`
deliberately has no volume — issue #15 decided a dev loop is not a store — and
carrying that default into Azure looked like the quiet version of a real failure:
a container restart empties the backend, the monitors keep running, and "online
evals against live traffic" becomes a claim that stopped being true without
anything reporting it.

So a dedicated storage account, a 100 GiB SMB file share and a mount at
`/mnt/phoenix` were built, with `max_replicas = 1` for a single writer and
`nobrl` in the mount options for SQLite's byte-range locks. **It fails, and the
way it fails is worth recording precisely:**

1. The first container starts, migrates, serves, and takes 34 real production
   spans. It creates `phoenix.db`, `phoenix.db-wal` and `phoenix.db-shm` on the
   share. So far it looks like it works.
2. Container Apps rolls a new replica **before** terminating the old one — there
   is no "recreate" strategy for a container app — so for about a minute two
   Phoenix processes hold the same SQLite file. `max_replicas = 1` does not
   prevent this. It bounds the steady state, not the transition, and that
   distinction is the whole bug.
3. The second process dies with
   `sqlean.dbapi2.OperationalError: unable to open database file` and
   crash-loops. SQLite's WAL mode coordinates readers and writers through the
   `-shm` file's shared memory, and CIFS cannot provide the mmap semantics that
   needs.
4. **It does not recover.** A clean scale-to-zero-and-back into a brand new
   revision failed identically, because the `-shm` left on the share is now
   neither openable by SQLite nor deletable through the Azure Files API, which
   answers `DeletePending` — *"marked for deletion by an SMB client"*.

The share therefore bought exactly one process lifetime of persistence and a
backend that could not be restarted, which is strictly worse than no persistence
at all: it looks durable right up until the first time you need it to be.

**The two ways out, and why the cheap one is also the right one.**
Phoenix's supported production database is PostgreSQL, and an Azure Database for
PostgreSQL Flexible Server on the smallest burstable tier is about **\$16 a
month** — on an estate whose steady state already crosses its own budget alert —
and, because `cae-chip-chat` is consumption-only with no VNet, it would need a
public endpoint with a password and an "allow Azure services" firewall rule. That
is a database holding visitors' messages, reachable from every Azure IP, bought
to solve a problem the estate does not actually have. Because:

> **Application Insights already holds every one of these spans.** Same tracer
> provider, same span ids, same trace ids, thirty days of retention, and it was
> verified rather than assumed — see *Evidence*. Phoenix is the agent-shaped view
> and the monitors' rolling window; App Insights is the archive.

So `PHOENIX_WORKING_DIR` is container-local, the storage account is gone, and the
estate keeps its property that no resource in it has shared key access enabled.

**What is actually lost, stated plainly.** A restart empties the span-tree UI,
and the monitor job's next run sees only the traffic since the restart. The
monitors resume within fifteen minutes and nothing about *online evals are live*
stops being true — the loop reads a rolling twenty-minute window, not a history.
What a human loses is the ability to open last week's conversation as a **tree**;
they can still open it in App Insights as a list of spans under one
`operation_Id`, which is the same data in a worse shape. That is the cost, in
full, and if it ever stops being acceptable the fix is the \$16 PostgreSQL server
and this paragraph is the argument that would have to be overturned.

**`max_replicas = 1` stays**, for a different reason now: a second replica would
be a second, separate database behind one ingress — half the traces in each, and
a monitor reading whichever one the load balancer chose.

### 3. Ingress: internal only

Production traces carry what a stranger typed, what the model said back, and the
passages the retriever returned. A publicly readable Phoenix is a public
transcript of every conversation the demo has ever had, and `docs/prd.md`'s
whole posture about visitors' messages does not survive one.

So `external_enabled = false`. The FQDN
`ca-chip-chat-phoenix.internal.<env-domain>` resolves only inside
`cae-chip-chat`, which is exactly the set of things with business there: the chat
app, which writes, and the monitor job, which reads. HTTP rather than HTTPS on
that hop, deliberately — nothing on it leaves the managed environment's own
network, and the alternative is an OTLP client trusting a certificate for an
`.internal.` name.

**The alternative was external ingress with Phoenix's own authentication**
(`PHOENIX_ENABLE_AUTH` and a secret), which would let the owner open the UI in a
browser from anywhere. It was rejected because it trades a network boundary for a
password on an internet-facing service holding visitors' messages, and because
the thing it buys is available without it.

#### Reading the traces

The UI is not gone, it is inside. Two ways in, neither of which opens a port:

```bash
# Ask the container questions directly. Needs a TTY.
az containerapp exec -g rg-chip-chat -n ca-chip-chat-phoenix \
  --command "python -c \"import urllib.request;print(urllib.request.urlopen('http://localhost:6006/v1/projects/default/spans?limit=5').read()[:2000])\""

# Or read them the way the monitors do, from a shell that can reach the
# environment — which is any Container Apps job in it.
az containerapp job start -g rg-chip-chat -n caj-chip-chat-monitors
```

For a browser, the supported move is to flip ingress to external **with an IP
restriction to your own address**, look, and flip it back with `terraform apply`.
That is a deliberate two-command act with an audit trail, which is the right
shape for something that exposes a transcript.

### 4. Minimum replicas is one, and that is the line item

Everything else in this estate scales to zero, and `compute.tf` argues for it
well: an idle replica bills for nothing served. That argument does not survive
contact with a span collector. **A batch of spans arriving at a backend that is
scaled to zero is dropped, quietly** — the exporter is not a request anybody is
waiting on, so there is nothing to wake the replica and nothing to report the
loss. The first symptom would be a monitor that has not fired in a week because
it saw no traffic rather than because there was none, which is the exact failure
mode this whole package exists to make impossible.

So `min_replicas = 1`, always on, and the cost of that is below.

---

## What it costs

**Estimated, not measured.** These resources are hours old; the first real number
will come from the cost export and belongs in `docs/cost.md` §13 when it does.
The arithmetic is here so it can be argued with.

| | per month | how it was got |
| --- | ---: | --- |
| Phoenix replica, idle rate | **\$11.66** | 0.5 vCPU and 1 GiB for 2,592,000 s at the consumption plan's idle rates (\$0.000003 per vCPU-s and per GiB-s) |
| Phoenix replica, active rate | **\$38.88** | the same seconds at the active vCPU rate (\$0.000024) — the ceiling if the platform never counts it idle |
| Storage | **\$0.00** | there is none; see *Persistence*, and the \$16 PostgreSQL server that is the alternative |
| Monitors job | **≈ \$0** | 2,880 executions a month at roughly a minute of 0.5 vCPU each ≈ 86,000 vCPU-seconds, inside the subscription's 180,000 vCPU-second monthly free grant |
| Judge tokens | **inside the daily ceiling** | 5.3% of a 2,000,000-token day at 20% sampling and 1,057 tokens per judged turn — *measured*, on the first live run |
| **Phoenix and the monitors, total** | **\$12 – \$40** | the range is the idle question and nothing else |

**The honest reading of that range** is that nobody should quote the bottom of
it. Phoenix runs background tasks and serves its own UI, so whether Container
Apps ever classes the replica as idle is an empirical question this record cannot
answer. Budget against \$40 and be pleased.

### What it does to the estate's budget

`docs/cost.md` §13 estimates steady state at **≈\$92/month** against the
\$150 budget in `infra/terraform/cost.tf`, whose first alert is at \$75 — an
alert that document already says *"would be surprised"*. Adding \$12–40 puts
steady state at **\$104–132**. The 50% alert was already going to fire; this
moves the **80% alert at \$120 into range as well**, and at the top of the range
the \$150 budget itself has about \$18 of headroom.

That is the price of the launch criterion and it should be stated as a price
rather than discovered on a bill. There is exactly one way down and it is
unattractive: turn `phoenix_enabled` off between demos, and lose the traces and
the monitors with it. **The replica is the whole bill, and the replica is the
thing that must not be asleep** — which is why `min_replicas = 1` is the line
item and not a tuning parameter. Cutting the container to 0.25 vCPU would halve
the vCPU half of the range; whether Phoenix is comfortable there has not been
measured and this record will not pretend it has.

Against the alternative it replaces: **AX Free would have been \$0** for 25,000
spans a month. That is the plainest statement of what this decision costs — it is
not that self-hosting is cheaper, it is that self-hosting needs no signup. One
conversation is roughly fifty spans, so 25,000 spans is about 450 conversations a
month; this estate is paying \$12–40 to avoid an account creation, and to keep
its traces on its own subscription. Both halves of that are the owner's call and
this record is where the trade is written down rather than assumed.

---

## The diff, split the way #78 asks for it

### Instrumentation code: none

```console
$ git diff --stat origin/main -- otel/ agent/src/chip_chat/agent/
(no output)
```

Nothing under `otel/` changed, and that is not a claim about this particular
change — it is a property the tree is held to on every build.
`otel/tests/test_export_configuration.py::test_the_exporter_code_names_no_vendor`
parses `exporters.py`, `config.py` and `tracing.py` with `ast` and fails if
`phoenix` or `arize` appears in any identifier or runtime string. It still
passes, with production spans landing in Phoenix.

Worth being precise about what that establishes, because this is the second time
the claim has been cashed and the first time it was cashed against a *deployed*
backend: the instrumentation genuinely does not know which backend answered.
`build_span_exporters` is still a list comprehension over configured slots, and
moving from "nowhere" to "a Phoenix in Container Apps" changed the value of
`config.otlp_endpoint`. That is the sentence D6 was bought for and it survived
contact a second time.

### Configuration: one environment variable

```diff
  # infra/terraform/compute.tf, in local.web_env
- var.otlp_endpoint == "" ? {} : { OTEL_EXPORTER_OTLP_ENDPOINT = var.otlp_endpoint },
+ local.otlp_endpoint == "" ? {} : { OTEL_EXPORTER_OTLP_ENDPOINT = local.otlp_endpoint },
```

`local.otlp_endpoint` is defined in `observability.tf` and resolves to the
Phoenix app's own internal address, computed from the resource so it cannot go
stale. `var.otlp_endpoint` survives as the override, so the AX switch
`arize-switch.md` documents is still one variable away and nothing above had to
be deleted to make room for this.

The deployed value:

```console
$ az containerapp revision show -g rg-chip-chat -n ca-chip-chat-web \
    --revision ca-chip-chat-web--0000020 \
    --query "properties.template.containers[0].env[?contains(name,'OTEL')]"
[{"name": "OTEL_EXPORTER_OTLP_ENDPOINT",
  "value": "http://ca-chip-chat-phoenix.internal.whitesea-eea6e4c0.eastus2.azurecontainerapps.io"}]
```

**Two variables the brief expected and this deliberately does not set**, because
in this repository an unread setting is worse than an absent one:

- `OTEL_EXPORTER_OTLP_HEADERS` — a self-hosted backend on an internal address
  needs no API key. `compute.tf`'s comment that there is *"not a secret among
  them"* is still true, and the Key Vault-backed Container Apps secret that
  `arize-switch.md` describes as the one gap remains ungapped and unneeded.
- `OTEL_EXPORTER_OTLP_PROTOCOL` — `chip_chat.otel.config` never reads it. It is
  an **agent-tier** variable; the FastAPI tier's exporter is OTLP over HTTP
  unconditionally. `observability-backend.md` calls this out as one of its two
  asymmetries and setting it here would make the map look like it configures
  something it does not.

### New infrastructure

`infra/terraform/observability.tf`: the Phoenix container app and the scheduled
monitor job. `infra/terraform/variables.tf` gains `phoenix_enabled`,
`phoenix_image`, `monitors_image_tag`, `monitors_cron` and `monitors_args`.
`infra/terraform/outputs.tf` gains `otlp_endpoint`, `phoenix_app_name` and
`monitors_job_name`. There is no storage; see *Persistence*.

### New code, and where the vendor's name is allowed to appear

`eval/src/chip_chat/eval/online/phoenix.py` — the trace source #76 never had.
`eval/online/README.md` already said what shape it would be: *"an Arize adapter,
a Phoenix adapter and a file are three functions producing one shape"*. It is one
function, it names the vendor, and that is correct: the rule against naming a
backend lives in `otel/` and exists so the *instrumentation* can move. An adapter
is the opposite thing — it exists to know one backend's row shape, and the design
is that the knowing is confined to it. Everything above `LiveTurn` is the code
the offline evals already use.

`eval/Dockerfile` — a third image, because the app image is
`uv sync --package chip-chat-api` and `chip-chat-eval` is deliberately not among
its dependencies.

---

## Evidence

Everything below was run against the deployed estate on 28 August 2026.

### Real turns through the live app, arriving as complete span trees

Three turns driven through
`https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io`, read
back out of Phoenix's REST API: **34 spans, 3 traces, 0 split**.

```
chat.turn  [CHAIN]
  guard.budget_check  [GUARDRAIL]
  guard.content_safety  [GUARDRAIL]
  agent.step  [AGENT]
    llm.completion  [LLM]
    tool.search_menu_knowledge  [TOOL]
      retriever.search  [RETRIEVER]
  agent.step  [AGENT]
    llm.completion  [LLM]
    tool.get_points_balance  [TOOL]
    tool.get_usual_order  [TOOL]
  agent.step  [AGENT]
    llm.completion  [LLM]
  render.response  [CHAIN]
```

RFC-001 §09 node for node, with the span kinds Phoenix reads an agent run from,
and the root carrying `session.id`, `chip_chat.demo.id`, `chip_chat.tokens.total`
and the OpenInference `input.value` / `output.value`. Each turn is **one** trace,
which is the thing `make trace-boundary` exists to catch and the thing every
trajectory and grounding number depends on.

### Application Insights, still receiving the same trees

```console
$ az monitor app-insights query --app appi-chip-chat -g rg-chip-chat \
    --analytics-query "union dependencies, requests | where timestamp > ago(60m)
                       | summarize spans=count() by operation_Id"
f0b40401375a88b3b29f1af95021ace4   14
cf24f787fe124a1c37edbf2093d02c0c   10
fb1053f97561cfb4632b15428892ceea   10
```

The **same three trace ids** and the **same three span counts** Phoenix returned
(14, 10, 10). This is the fan-out asserted end to end rather than in a unit test:
one tracer provider, two exporters, identical span ids in both backends. Losing
App Insights would have taken the latency and cost measurements with it, and it
was not lost.

### Traces do not survive a restart, and that was found rather than assumed

The Phoenix revision was restarted with those three traces in it. The replacement
replica crash-looped on
`sqlean.dbapi2.OperationalError: unable to open database file`, the original kept
serving, and a subsequent clean scale-to-zero-and-back into a new revision failed
the same way — at which point the `-shm` file on the share could not be deleted
either (`DeletePending`, *"marked for deletion by an SMB client"*). The full
sequence is under *Persistence* above; the storage account has been removed and
the working directory is container-local.

### A monitor firing on a real trace

The scheduled job, run against the twenty minutes of traffic above:

```
3 turn(s), 3 judged, 0 unreadable
  [dashboard] latency_or_cost_breach f0b40401…: the turn took 23127 ms against a 6000 ms target
  [dashboard] latency_or_cost_breach fb1053f9…: the turn took 11824 ms against a 6000 ms target
  [ticket]    ungrounded_menu_claim_judged cf24f787…: the judge found a claim the turn's
              2 retrieved passage(s) do not support
  [dashboard] refusal_where_the_corpus_answered cf24f787…: the reply declined while holding
              2 retrieved passage(s)
  [dashboard] latency_or_cost_breach cf24f787…: the turn took 52641 ms against a 6000 ms target
budget: Ceiling 2,000,000 tokens/day. At 20% sampling and 1,057 tokens per judged turn,
        500 turns cost the judges 105,700 tokens — 5.3% of the day's ceiling.
```

Three findings in that output are worth more than the fact that it ran.

---

## Three things the first live run found

### The over-refusal monitor caught the thing nobody predicted

#76's demo criterion is *"at least one monitor has caught something that was not
predicted"*, and this is it. The third turn asked how many calories are in the
steak burrito. There is no steak burrito on the published menu, so the model
declined — **while holding two retrieved passages**, and while volunteering the
Barbacoa Burrito's calorie count from one of them. `refusal_where_the_corpus_
answered` fired, and so did `ungrounded_menu_claim_judged` on the same trace.

That is not a monitor confirming a fixture. Nobody wrote that question, the
condition arrived out of a real deployment answering a real request, and the
monitor found it within fifteen minutes of the turn happening. It is also the
same shape `eval/grounding/BASELINE.md` records four of offline — which is the
comparison the online loop was built to make possible.

### Every turn breaches the latency target, by a lot

11.8 s, 23.1 s and 52.6 s against a 6,000 ms target. Not a tail: **all three**.
The monitor is doing exactly its job and the finding belongs to the product
rather than to observability — but it is worth saying plainly, because the
latency target in `chip_chat.eval.online.monitors.LATENCY_CEILING_MS` is PRD
§05's, and the deployed app is currently between two and nine times over it on
every turn served.

### The 20% sampling rate had been switched off by accident

This one was a bug and is fixed. `SamplingPolicy` judges every turn *a
deterministic monitor already fired on* — a good rule, written on the assumption
that deterministic alerts are rare. In production the latency monitor fires on
**every** turn, so "judge anything that fired" became "judge everything": the
first run judged 3 of 3, at a realised rate of 100% against a policy of 20%, and
the budget line printed 5.3% of the daily ceiling while the loop was on course to
spend five times that.

The fix is `Monitor.escalates`, false on `latency_or_cost_breach` alone. It is a
narrowing with an argument rather than a threshold raised until the noise
stopped: the judge answers exactly two questions — was this claim supported by
what the turn retrieved, and did the reply decline — and neither is a thing you
learn about a slow turn. The breach still fires and still routes to the
dashboard, where it means something as a rate. `eval/tests/test_online_sampling.py::test_a_slow_turn_alone_does_not_buy_a_judge`
is the regression.

**This is the argument for online evals in one paragraph.** The policy was
reasoned about carefully, documented at length, unit tested, and wrong in a way
that only fifteen minutes of real traffic could show — because the thing that
broke it was a property of the deployment (the app is slow) rather than of the
code.

---

## What this record does not decide

- **Whether trace history is ever bought.** *Persistence* decides that it is not,
  today, and names the \$16 PostgreSQL server that would buy it. What is not
  decided is the threshold at which somebody should.
- **Whether the datasets and experiments halves get wired to Phoenix.**
  `make dataset-upload` still targets an Arize space that does not exist.
- **Whether the exit status becomes a real alert.** An Azure Monitor rule over
  the job's failed executions is a few lines and is not written.
- **Whether the latency target or the app moves.** The monitor says the app is
  between two and nine times over PRD §05. That is somebody's product decision.
- **Whether AX is ever bought.** `arize-switch.md` still holds the procedure and
  the diff, and `var.otlp_endpoint` is still the one variable it needs.

## References

`observability-backend.md` for the plan this deviates from and the argument
against self-hosting through the public phase. `arize-switch.md` for the switch
that is still one variable away. `docs/local-tracing.md` for the local half and
why the two backends are the same version. `eval/online/README.md` for the
monitors, the sampling argument and the budget arithmetic. RFC-001 D6 and §09.
PRD §12, launch criteria.
