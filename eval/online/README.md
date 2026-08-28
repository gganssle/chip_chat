# `eval/online` — monitors against real traffic, and what they cost

Issue [#76](https://github.com/gganssle/chip_chat/issues/76). Every other set in
`eval/` scores questions somebody wrote down. This one scores questions nobody
wrote down.

```bash
make online-check    # free: the policy, the monitors, the judges' share of the cap
make online-drill    # free: every feared condition, produced, and what caught it
make monitors-run    # the deployed loop, against real traffic, right now
```

## The difference that shapes everything else

An offline eval knows what the right answer was. An online eval does not. There
is no expected lane on a stranger's question, no labelled refusal direction, no
ground truth of any kind — so a monitor here may only fire on something that is
wrong **on its face**.

That is not a limitation to be worked around. It is what stops this from being a
system that reports a rate nobody can act on. Every monitor below either reads a
fact off a span or asks a judge one question about prose, and each one names a
turn a human can open.

## The six monitors, and the sentence from #76 each implements

| Monitor | Severity | Needs a model | The fear |
| --- | --- | --- | --- |
| `cross_visitor_disclosure` | **page** | no | *A cross-visitor disclosure signal of any kind, which should be impossible and therefore alarming.* |
| `ungrounded_menu_claim` | ticket | no | *An ungrounded menu claim* — the deterministic half: a claim on a turn that retrieved nothing, a claim with no citation, a minted passage id. |
| `ungrounded_menu_claim_judged` | ticket | yes | The same fear, where the passages exist and do not support the claim. |
| `refusal_where_the_corpus_answered` | dashboard | yes | *A refusal where the corpus plainly had the answer.* |
| `photo_match_without_confident_sku` | dashboard | no | *A photo match with no confident SKU — the matcher escalating or, worse, not escalating when it should.* |
| `latency_or_cost_breach` | dashboard | no | *Latency and cost per conversation breaching their targets.* |

**Four of the six need no model, and that is a design decision rather than a
saving.** They run on **every** turn rather than on the sampled fifth. A
cross-visitor disclosure monitor sampling 20% of traffic misses four disclosures
in five, and launch gate one would then be a gate that catches one breach in five.

**Severity is a routing decision, stated here rather than left to whoever wires
the alert.** A disclosure signal pages. A launch gate breached opens an issue.
Everything else accumulates on a dashboard, because it means something as a rate
rather than as an instance. This package does not *deliver* alerts — the route is
somebody's action group or webhook, and putting a delivery mechanism in an eval
package makes the eval untestable and the delivery unowned.

## Sampling: 20%, and three classes that ignore it

A judged turn costs two model calls — groundedness and the refusal — against
roughly six hundred prompt tokens each, because the passages go into the prompt.
Judging everything makes the judges a visible fraction of the daily ceiling;
judging a fifth makes them a rounding error, and a fifth of a few hundred turns is
still enough to see a *systematic* failure.

It is not enough to catch a rare one, which is what the always-sampled classes are
for. One of them had to be narrowed the first time this ran against production,
and the narrowing is described under *"Turns a deterministic monitor already fired
on"* below — read it, because it is the difference between the 20% above being a
rate and being a decoration:

- **Allergen and dietary turns.** PRD §10 makes this the subject where a
  confident wrong answer is a safety problem rather than an accuracy one, and a
  fifth of a safety property is not a safety property. The screen that identifies
  them is a keyword sweep and is wrong in both directions; over-sampling is the
  correct direction to be wrong in, and nothing *scores* off the flag.
- **Turns an *escalating* deterministic monitor already fired on.** Something
  cheap has said this one is interesting; the judge is what says how.

  **"Escalating" is not "fired", and production is what taught the difference.**
  The first live run judged three turns out of three, at a realised rate of 100%
  against a policy of 20%, because `latency_or_cost_breach` fires on every turn
  the deployed app serves — between two and eleven times over a 6,000 ms target,
  every time — and "judge anything that fired" is therefore "judge everything".
  The budget line below then reports the judges at 5% of the daily ceiling while
  the loop is on course to spend five times that. After the fix the same loop
  reports `'flagged': 0, 'not_sampled': 6` on ten turns.

  `Monitor.escalates` is the fix and it is false on exactly one monitor. The
  judge answers two questions — was this claim supported by what the turn
  retrieved, and did the reply decline — and neither is a thing you learn about a
  slow turn. The breach still fires and still routes to the dashboard, where it
  means something as a rate. This is a narrowing with an argument, not a
  threshold raised until the noise stopped.
- **Turns that made a claim and retrieved nothing.** The floor under
  groundedness, free to detect, and the shape most likely to be the thing #76
  fears first.

**The decision is a hash of the trace id, not a die roll.** *Why was this turn not
judged* is a question somebody asks about exactly the turn that mattered, and a
sampler you cannot re-run over yesterday's traces cannot answer it. SHA-256 rather
than Python's `hash`, because that one is salted per process.

## Judge spend, inside the cap rather than beside it

#76's last acceptance criterion names a precise failure: RFC-001's global daily
token ceiling is enforced **inline, in the request path**, in front of every model
call — and an online judge is a model call that does not go through the request
path. A cap with a second bill outside it is not a cap.

`chip_chat.eval.online.budget` does the arithmetic and **enforces nothing**, and
saying so plainly is the point: an eval package that started refusing things would
be a second gate that can disagree with the first. It reads
`CHIP_CHAT_DAILY_TOKEN_CEILING` — the same variable the request path reads — and
never defaults it. Where it is unset, `make online-check` **exits non-zero**,
because a monitoring loop whose spend is unaccounted is the hole, and a check that
shrugged at it would be the criterion satisfied by a paragraph.

The per-turn cost is **measured, not estimated**. A judged run prints what it
spent; pass it back in as `MEASURED_TOKENS`. Two measurements exist and the gap
between them is the useful part:

| Run | Judged turns | Tokens each | Share of a 2,000,000-token day at 20% |
| --- | ---: | ---: | ---: |
| The grounding eval against the golden set | 19 calls | **916** | 4.6% |
| The online loop over 20 captured production traces | 10 turns | **103** | 0.5% |

The difference is the passages. A judged turn's prompt carries the passages the
turn retrieved, so a turn that retrieved eight chunks of a nutrition page costs
several times what a turn that retrieved a one-line hardcoded menu summary does.
**Budget against the larger number**, because the day the knowledge lane is
wired every judged turn moves from the second row to the first — and note that
even the pessimistic figure is under five percent, which is the answer to *did
the judges just cost us the demo*.

That is arithmetic on a measurement rather than a projection, and the projection
is what left the hole in the first place.

## What a drill is, and is not

`make online-drill` builds each feared condition by hand — a turn that claims with
nothing retrieved, a photo match that resolved nothing and did not escalate, a
reply that declines with two passages in hand, a span carrying a second visitor's
identifier, a turn over both ceilings — runs them through the real monitors, and
reports which one caught which. It exits non-zero if any monitor fails to fire on
its own condition.

**It is a fixture and would be a fraud as evidence about the product.** It tells
you the detector works. It tells you nothing about how often the condition occurs,
and its output must never be read as a rate. What it establishes is the thing that
is otherwise unestablished and matters more than a rate: when the condition
happens in production, something fires.

The disclosure drill deliberately constructs the thing launch gate one exists to
make impossible — a span naming a second visitor's rows. That is legitimate in a
fixture and nowhere else: nothing in it reaches a tool signature, an endpoint or a
request path, and the identifier never leaves the process.

## Where the turns come from

Two sources, and nothing downstream can tell which one it was.

**The deployed backend**, which is what the scheduled job reads:

```bash
python -m chip_chat.eval.online --phoenix "$OTEL_EXPORTER_OTLP_ENDPOINT" \
  --lookback-minutes 20 --judge --fail-on page
```

`chip_chat.eval.online.phoenix` is that adapter and it is one function, as this
section always said it would be: *an Arize adapter, a Phoenix adapter and a file
are three functions producing one shape*. It names a vendor, which is correct —
the rule against naming a backend lives in `otel/` so that the *instrumentation*
can move, and an adapter is the opposite thing. Three shape differences are its
whole job, and each is a silent wrong answer if got wrong: attributes arrive
nested and the readers look up dotted keys; times arrive as ISO strings and the
latency monitor divides nanoseconds; and a span's end time is not an attribute in
any backend, so `signals.END_TIME` is the key the adapter has to write or every
turn reports a duration of zero and reads as fast enough.

**A capture**, which is one JSON file:
`{"turns": [{"message", "reply", "spans": [...]}]}`, each span a flat object with
the fields of `chip_chat.eval.trajectory.trees.TraceSpan`. That is deliberately
the *reader's* shape rather than any backend's — #74's module docstring already
says it: a second adapter is a function, and a second reader would be a second
implementation of the metric.

`make experiment-baseline` writes a capture as a side effect of a run, which is
the cheapest real span tree there is.

## Where the alerts go

Nowhere, from this package, on purpose — and the *caller* routes. For the
scheduled job the route is the exit status: `--fail-on page` exits 2 when a
disclosure signal fires, which fails the Container Apps job execution, which is
visible in `az containerapp job execution list` and is a thing Azure Monitor can
alert on without this repository knowing anything about Azure Monitor. Two rather
than one, so that a run which failed *because it found something* is
distinguishable from a run that could not read its input.

## Status

The monitors, the sampling policy, the budget arithmetic and the drill are live
and free to run, and the loop has been run over **20 real captured traces** from
a live `gpt-5-mini` experiment: 10 judged (5 claimed by the allergen class, 2 by
*claimed and retrieved nothing*, 3 by the rate), 0 unreadable, 0 alerts. Zero
alerts on twenty turns is a result rather than a silence, and the reason is
`cc-bap`: this deployment reports no claim class and no citations, so the
ungrounded-claim rule has nothing to read, and the refusal monitor fires only on
a decline *holding passages* — which none of the twenty was.

The condition monitor three exists for **has** been seen in real model output,
by the offline eval that shares its arithmetic: `eval/grounding/BASELINE.md`
records **four over-refusals** on a live judged run, one of them a comparative
calorie question declined while holding two passages that answered it. That is
monitor three's condition exactly, found on real prose and predicted by nobody.

**As of 28 August 2026 it has now happened in production too**, which is the
sentence this file was written waiting to be able to say. The loop has a live
trace source and a schedule: `chip_chat.eval.online.phoenix` reads a
self-hosted Phoenix in `cae-chip-chat`, and a Container Apps job runs
`--phoenix … --lookback-minutes 20 --judge --fail-on page` every fifteen minutes.
On its first run against real traffic:

- `refusal_where_the_corpus_answered` fired on a stranger-shaped question — *how
  many calories are in the steak burrito* — that the deployed model declined
  **while holding two retrieved passages** and while volunteering a different
  item's calorie count from one of them. Nobody wrote that question and nobody
  predicted the condition. It fired again on a later run, on a reply that
  declined while holding **five**.
- `ungrounded_menu_claim_judged` opened three tickets on turns whose retrieved
  count was **zero** — the knowledge lane is `not_wired` on the deployed image,
  so the model answered menu questions from its weights and the judge said so.
- `latency_or_cost_breach` fired on **every one of ten** turns: 13.3, 19.7, 24.3,
  24.6, 30.9, 33.4, 42.1, 47.4, 64.6 and 66.0 seconds against a 6,000 ms target.
  A finding about the product rather than about observability, and the reason
  `Monitor.escalates` exists.
- The judges cost **959 tokens per judged turn**, measured, which is 4.8% of a
  2,000,000-token day at 20% sampling. Budget against that rather than against
  the 103-token figure in the table above; the passages are the difference and
  production carries them.

PRD §12 puts online evals live *before the URL is shared*, and that criterion is
met. It is met by a self-hosted backend rather than by the Arize AX purchase this
file used to be waiting on; what that trade cost is in
[`docs/decisions/hosted-phoenix.md`](../../docs/decisions/hosted-phoenix.md), and
[`docs/arize-switch.md`](../../docs/arize-switch.md) still holds the procedure for
the day AX is bought.
