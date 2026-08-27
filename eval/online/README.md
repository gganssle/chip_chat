# `eval/online` — monitors against real traffic, and what they cost

Issue [#76](https://github.com/gganssle/chip_chat/issues/76). Every other set in
`eval/` scores questions somebody wrote down. This one scores questions nobody
wrote down.

```bash
make online-check    # free: the policy, the monitors, the judges' share of the cap
make online-drill    # free: every feared condition, produced, and what caught it
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
for:

- **Allergen and dietary turns.** PRD §10 makes this the subject where a
  confident wrong answer is a safety problem rather than an accuracy one, and a
  fifth of a safety property is not a safety property. The screen that identifies
  them is a keyword sweep and is wrong in both directions; over-sampling is the
  correct direction to be wrong in, and nothing *scores* off the flag.
- **Turns a deterministic monitor already fired on.** Something cheap has said
  this one is interesting; the judge is what says how.
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

A **capture** is one JSON file: `{"turns": [{"message", "reply", "spans": [...]}]}`,
each span a flat object with the fields of
`chip_chat.eval.trajectory.trees.TraceSpan`. That is deliberately the *reader's*
shape rather than any backend's — #74's module docstring already says it: a second
adapter is a function, and a second reader would be a second implementation of the
metric. An Arize adapter, a Phoenix adapter and a file are three functions
producing one shape.

`make experiment-baseline` writes a capture as a side effect of a run, which is
the cheapest real span tree there is.

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
What has not happened is the monitor seeing it *in production*, because: the deployed app's
`OTEL_EXPORTER_OTLP_ENDPOINT` is empty until [#78](https://github.com/gganssle/chip_chat/issues/78)
lands, so production spans reach Application Insights and no OTLP backend. PRD §12
puts online evals live *before the URL is shared*, and that ordering is the right
one — this package is what makes it possible to keep, and the AX purchase is what
it is waiting on. See [`docs/arize-switch.md`](../../docs/arize-switch.md).
