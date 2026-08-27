# What one conversation costs, all in

Issue [#87] asks for the full unit economics of one conversation across four
platforms, measured against PRD §05's target of **under $0.05 per
conversation**. The system design's claim is that this dashboard "falls out of
[the span schema] almost free … which is a genuinely rare thing to be able to
quote", and half of that is true: the token half does fall out, because
[#64](https://github.com/gganssle/chip_chat/issues/64) put the counts on the
spans and `chip_chat.tokens.*` exists precisely so that "what did this
conversation cost" is one attribute lookup rather than a tree walk. The other
half — credits, DBUs, gateway-hours — arrives from four different billing
surfaces on four different clocks, and reconciling them is most of the work in
this document.

**The headline, and it is not the one the plan expected.** The models are the
cheapest thing here. One median conversation costs about **1.6 cents** of model
tokens, comfortably inside the target. One *account question* costs **$0.20**,
which is four times the entire per-conversation budget on its own. And the
standing infrastructure that computes the nightly marts — a NAT gateway, a
public IP, a container registry — costs about **$41.50 a month whether anybody
talks to Cilantro or not**, which at any plausible demo volume dwarfs both.

Every number below is measured. Where a number is an estimate or a projection it
says so in the same sentence, and where a number could not be measured at all
there is a section explaining why rather than a plausible figure standing in for
it.

---

## 1. The snapshot, and the four places numbers come from

Everything here was taken on **2026-08-27, at 21:45 UTC**, against the deployed
estate. It is a snapshot rather than a steady state: the app went onto revision
`0000018` at 21:15 UTC the same day, several agents were running evals against it
in parallel, and every `llm.completion` span in Application Insights is from that
one day. Numbers taken tomorrow will differ. What should not differ is the
*shape*, and the shape is the finding.

| Surface | What it knows | How late it is | Read it with |
| --- | --- | --- | --- |
| **Application Insights** spans | tokens, by turn, by lane, by session | seconds | `az monitor app-insights query --app appi-chip-chat` |
| **Azure Cost Management** | dollars, by meter | **8–24 hours** | `az rest … /providers/Microsoft.CostManagement/query` |
| **Snowflake `ACCOUNT_USAGE`** | credits, by service type and warehouse | up to 2 hours | `snow sql -c chipchat` |
| **Snowflake resource monitors** | credits, by warehouse | real time | `SHOW RESOURCE MONITORS` |

The lags are not a footnote. Section 3 shows the meter and the spans disagreeing
by a factor of about two on the same day's model traffic, and the disagreement is
entirely the lag — which is the strongest argument this project has for putting
the token counts on the spans in the first place.

---

## 2. The whole Azure bill, month to date

This is the ground truth for the Azure half. Subscription
`c8b63a71-218d-4d4c-991c-b963ed2fd1f0`, month to date on 2026-08-27, grouped by
service, every line:

| Service | USD | Share |
| --- | ---: | ---: |
| Azure Databricks | 2.4229 | 40.2% |
| **NAT Gateway** | **1.4803** | **24.6%** |
| Virtual Machines | 1.2920 | 21.5% |
| Storage | 0.3493 | 5.8% |
| Container Registry | 0.2202 | 3.7% |
| Virtual Network | 0.1559 | 2.6% |
| **Foundry Models** | **0.0941** | **1.6%** |
| Bandwidth | 0.0064 | 0.1% |
| Key Vault | 0.0004 | 0.0% |
| Azure Cognitive Search | 0.0000 | — |
| Log Analytics | 0.0000 | — |
| Azure App Service | 0.0000 | — |
| Azure Monitor | 0.0000 | — |
| Foundry Tools | 0.0000 | — |
| **Total** | **6.0214** | |

Four things in that table are worth stopping on.

**The models are 1.6% of the bill.** Every guardrail in this repository that
worries about model spend — the inline cap, capacity 10 on both deployments, the
five-step loop ceiling, `max_completion_tokens = 2000` — is guarding nine cents.
This is not an argument for removing any of them: an unauthenticated public
endpoint attached to a subscription is a different risk profile and the cap is
there for the tail, not the mean. But it does mean the *mean* has never been the
problem, and a cost review that only looked at tokens would have found nothing.

**The lakehouse is 89% of it.** Databricks DBUs ($2.42) plus the VMs those DBUs
run on ($1.29) plus the NAT gateway ($1.48) plus its public IP ($0.16) is $5.35 of
$6.02. Section 6 separates the part that scales with work from the part that
bills for existing.

**Three services cost exactly zero, and each is a decision.** Azure Cognitive
Search bills `$0.0000` because [#10](https://github.com/gganssle/chip_chat/issues/10)
put it on the Free tier — the meter reads `Free Unit`, and Basic would be
$73.73/month. Azure App Service bills `$0.0000` because the ops API is on an FC1
Flex Consumption plan that scales to zero. Container Apps does not appear in the
table *at all*, because `min_replicas = 0` and the platform's monthly free grant
has covered everything the demo has done. Log Analytics is zero under a 1 GB/day
ingestion cap. **Two of these zeroes have a correctness consequence and are
argued in section 8.**

**`Bandwidth / Standard Data Transfer Out - Free` bills $0.0000, on the bill.**
Section 7 is about egress, and this row is the punchline arriving early.

---

## 3. What one turn costs, measured

### 3.1 The token shape of a turn

From `chat.turn` spans in `appi-chip-chat`, grouped by the set of `tool.*` spans
underneath each one. Ninety-three turns across thirteen sessions; the `tokens`
column is how many of those turns carried a `chip_chat.tokens.total` rollup.

| What the turn did | turns | tokens | prompt | completion | total | median | p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| no tool call | 66 | 43 | 3,978 | 675 | 4,603 | 34.6 s | 53.3 s |
| `get_points_balance` + `get_usual_order` | 10 | 10 | 6,290 | 1,895 | 8,246 | 45.5 s | 67.3 s |
| `propose_order` | 10 | 9 | 6,977 | 361 | 7,383 | 32.3 s | 57.0 s |
| `search_menu_knowledge` | 6 | 4 | 7,380 | 1,303 | 8,732 | 61.2 s | 105.5 s |
| `get_points_balance` | 1 | 1 | 8,293 | 1,752 | 10,045 | 46.9 s | 46.9 s |

Token columns are medians. The gap between `turns` and `tokens` is exactly the
turns that failed: 93 turns, 26 without a rollup, and 26 `openai.RateLimitError`
turns in the same window. **A turn either completed and carried its token count,
or it 429'd and carried none.** That is the reconciliation property
[#64](https://github.com/gganssle/chip_chat/issues/64) asked for, holding on real
deployed traffic rather than in a test.

Two caveats that matter more than the numbers. The turn latencies here are an
**upper bound taken under contention** — several agents were running evals
against the same `gpt-5-mini` GlobalStandard deployment at capacity 10, the 429s
are theirs and mine both, and `docs/deployment.md` §6.3 makes the same
qualification about the same deployment. And **`/healthz/lanes` reports all five
lanes `not_wired` on this revision**: the tool spans above come from
`agent/src/chip_chat/agent/tools.py`'s in-memory fallbacks, not from AI Search,
Cortex Analyst or the gold marts. The *token* shape of a turn is therefore real
and the *tool* shape is a floor — a real knowledge turn carries retrieved chunks
in its prompt and will be larger.

### 3.2 Turning tokens into dollars

Prices from `docs/phase-0-verification.md`, taken off the Azure Retail Prices API
for East US 2 on 2026-08-26, per 1M tokens:

| | input | cached input | output |
| --- | ---: | ---: | ---: |
| `gpt-5-mini` (chat) | $0.25 | $0.025 | $2.00 |
| `gpt-4.1-mini` (vision) | $0.40 | $0.10 | $1.60 |
| `text-embedding-3-small` | **not recorded anywhere in this repository** | | |

At list price, with no cache credit, one turn of each shape:

| What the turn did | model cost |
| --- | ---: |
| no tool call | $0.00234 |
| `propose_order` | $0.00247 |
| `search_menu_knowledge` | $0.00445 |
| `get_points_balance` + `get_usual_order` | $0.00536 |
| `get_points_balance` | $0.00558 |

**Output tokens are 15% of the volume and 71% of the model bill.** The meter
makes this exact: of $0.0941 of Foundry Models spend, $0.0665 is `GPT 5 Mini
outpt` and $0.0169 is every kind of input on both chat models put together.
`gpt-5-mini` is a reasoning model and its output tokens include reasoning tokens,
which is why a turn that decides not to call a tool still emits 675 of them. If
anybody ever wants the model bill smaller, the lever is the completion, not the
prompt.

**And 68% of billed input tokens were cache hits.** Reversing the meter at the
published rates: $0.013969 of uncached input is 55,876 tokens, $0.002909 of
cached input at a tenth the price is 116,360 tokens, so 116,360 of 172,236 input
tokens — **67.6%** — billed at $0.025 rather than $0.25. The system prompt and the
eleven tool schemas are identical on every turn and long enough to cache, which
is the whole mechanism. It is worth about a third off the input half.

### 3.3 What each lane adds on top of its tokens

| Lane | Beyond the model | Cost | Measured? |
| --- | --- | ---: | --- |
| **Knowledge** | one hybrid query + one semantic rerank | **$0** | yes — Free tier, meter reads `Free Unit` |
| **Account** | one Cortex Analyst message | **$0.20** | yes — 64 requests, §4 |
| | the SQL it then runs (225 ms X-Small) | $0.0002 | yes — `snowflake-semantic-view.md` §4 |
| **Personalization** | one mart query on the serving warehouse | ~$0.0002 | **no** — assumed equal to the account lane's SQL |
| **Action** | one FC1 Functions invocation + one stored procedure | **$0** | yes — App Service meter reads $0.0000 |
| **Vision** | Content Safety image screen (F0) | **$0** | yes — free tier, 5,000/month |
| | one `gpt-4.1-mini` describe, 990 in / 8 out | $0.00041 | from `make verify-vision`, not from a deployed turn |
| | the deterministic matcher | $0 | it is arithmetic |

`otel/spans.py` says of the vision recorder that "the photo lane is the expensive
one — an image is worth a few hundred prompt tokens — so a cost dashboard that
omitted it would be wrong in the direction that matters most." **On these prices
that is not true, and the sentence should be read as a warning about
completeness rather than a claim about magnitude.** A photo turn's own model call
costs about four hundredths of a cent, a fifth of the text turn wrapped around
it. The lane that is wrong in the direction that matters is the account lane, by
a factor of five hundred.

---

## 4. The account lane, which is the whole finding

**Cortex Analyst bills 67 credits per 1,000 messages.** Not read off a price
list — confirmed twice, on two different hours of the same day, against
`SNOWFLAKE.ACCOUNT_USAGE.CORTEX_ANALYST_USAGE_HISTORY`:

| hour (America/Los_Angeles) | requests | credits | credits each |
| --- | ---: | ---: | ---: |
| 03:00–04:00 | 47 | 3.149 | **0.06700** |
| 12:00–13:00 | 17 | 1.139 | **0.06700** |
| | **64** | **4.288** | |

At Enterprise's roughly $3 a credit that is **$0.20 per account question**, and
the two buckets agree to five decimal places, which is what makes it a rate
rather than an average.

Put beside PRD §05's target:

```
target, one whole conversation                     $0.05
one account question                               $0.20      4.0x
one median conversation, model tokens only         $0.0156
one median conversation + one account question     $0.216      4.3x
one median conversation + two account questions    $0.416      8.3x
```

**A single account question is four times the budget for the entire
conversation it appears in.** This is the most important number in the cost
work, it was found in Phase 4 rather than in Phase 9, and
`docs/decisions/snowflake-region.md` is where it was first written down — as a
cost finding deliberately handed to this issue rather than argued there.

Three things follow that are easy to get wrong.

**It is inference, not compute.** The SQL Cortex Analyst writes executes on
`CHIP_CHAT_SERVING_WH` in a median of 225 ms, which is 0.0000625 credits. The
question costs **three thousand times what the answer costs**. Every instinct in
`snowflake/sql/` about idleness — sixty-second auto-suspend, query acceleration
off, a statement timeout per lane — bounds the cheap end of that ratio.

**No resource monitor bounds it.** Resource monitors count virtual warehouse
credits. Cortex Analyst is serverless, so it is invisible to all three monitors,
including the account-wide cap section 8 records setting. On this account,
Cortex Analyst is **4.288 of 10.274 credits — 42% of everything the trial has
spent — from sixty-four requests.**

**It is not the region's fault, and it is not fixable by moving.** 67 credits per
1,000 messages is Snowflake's published rate for Cortex Analyst everywhere. The
cross-region hop documented in `docs/decisions/snowflake-region.md` costs
*latency* — a 2,973 ms median and 3,934 ms p95 of Analyst service time, server
reported. It does not cost extra credits. Rebuilding the account in a native
region would fix the latency and change this number not at all.

### What to do about it

Recorded here so the next person does not re-derive the options.

- **Accept the target is wrong for this lane, the way the latency target was.**
  `docs/decisions/snowflake-region.md` already split the latency target by lane
  rather than weakening it everywhere, on the grounds that only one lane pays.
  Exactly the same argument applies here and the same shape of answer is
  available: under $0.05 for knowledge, action, vision and personalization
  conversations; a named, larger number for a conversation that asks the account
  lane something. **This is the recommendation.** The PRD's single blended target
  was set before anybody knew one lane was four hundred times another.
- **Cache.** Two visitors asking "how many points do I have?" are two Analyst
  messages today. A deterministic fast path for the handful of questions that
  recur would remove most of them — `get_points_balance` already is one, which is
  why it is a separate tool with no Analyst call behind it. Widening that set is
  the cheapest available fix and it is a correctness improvement too.
- **Bound it inline.** The spend cap in `api/guard.py` counts tokens. It does not
  know that one tool call costs 0.067 credits. A per-session ceiling on Analyst
  messages would be the same shape of guardrail as the token ceiling and would sit
  in the same place. Not built; not filed either, until somebody decides between
  this and the previous bullet.

**Do not** solve it by making the model reluctant to call the tool. That is a
prompt instruction standing in for a control, which is the thing this repository
says not to do about writes and should not start doing about spend.

---

## 5. What one conversation costs

A conversation is a session. Thirteen of them exist on the deployed app,
**median 8 turns, longest 13**, against the per-session cap of 40 turns and
120,000 tokens in `api/limits.py`.

| Per session | prompt | completion | model cost, list | at the observed 68% cache rate |
| --- | ---: | ---: | ---: | ---: |
| **median** | 24,540 | 4,728 | **$0.0156** | **$0.0119** |
| p95 | 53,318 | 8,366 | $0.0301 | $0.0233 |

**On tokens alone the target is met, at the median and at p95.** $0.0156 against
$0.05 is 31% of budget, and the p95 conversation — the visitor who stays for
thirteen turns — is still only 60% of it. Nothing about the model layer needs
fixing.

The blended number, then, is entirely a question of what else the conversation
touched:

| One conversation that… | all in | vs $0.05 |
| --- | ---: | ---: |
| asks about the menu, orders something, looks at a photo | **$0.016** | **0.3x** ✅ |
| …and asks one account question | $0.216 | 4.3x ❌ |
| …and asks two | $0.416 | 8.3x ❌ |

**That is the answer to #87, and it is one number wide.** Four of the five lanes
are free relative to the model call that drives them. One lane is not.

### The number this table is not

Everything above is a **marginal** cost: what the next conversation adds. It is
not what a conversation costs the project, because the project pays $41.50 a
month before anybody types anything (section 6). Divide that standing cost by a
plausible public-demo volume and:

```
200 conversations/month:  $41.50 fixed  /  200  =  $0.208 per conversation
                        + $0.016 marginal
                        =  $0.224                             4.5x the target
```

**At any volume this demo will plausibly see, the fixed cost is the cost.** The
marginal number is the one worth optimising and the one PRD §05 is really asking
about; the fixed number is the one that shows up on the invoice. Both belong in
an honest answer, and conflating them — in either direction — makes both
meaningless.

---

## 6. The batch costs, kept separate

### 6.1 One nightly publish

Measured 2026-08-27, eleven tables and 108,157 rows from `dbw-chip-chat` in Azure
East US 2 to `hq72718.us-east-2.aws` — `docs/nightly-publish.md` §6 has the
derivation and the three settle-up queries.

| | measured | in dollars |
| --- | --- | ---: |
| Job wall clock | 176.7 s (51 s of it a cluster starting) | |
| Warehouse-active seconds | 112.5 | |
| Snowflake credits, **estimated** by `publish.warehouse_credits(112.5)` | 0.0312 | $0.09 |
| Snowflake credits, **billed** (177 s + 60 s auto-suspend = 237 s) | **≈0.066** | **$0.20** |
| DBUs (0.0491 cluster-hours at 1.224 DBU/hr) | ≈0.060 | $0.018 |
| VM (`Standard_F4ads_v7`, $0.343/hr) | 0.0491 hr | $0.017 |
| Bytes crossed | ≈1.53 MiB | see §7 |
| | | **≈$0.235 a night** |

The gap between 0.0312 estimated and 0.066 billed is the whole story of Snowflake
billing granularity: the estimator sums the eleven per-table active spans, and
Snowflake bills the *warehouse being awake*, including the sixty seconds of
auto-suspend it sits through afterwards. **A publish that touched one table would
cost nearly the same.** The estimator is not wrong; it answers a different
question, and both numbers are worth having beside each other.

At $0.235 a night that is **$7.05 a month**, and it spends about 3% of
`CHIP_CHAT_PUBLISH_MONITOR`'s two-credit daily quota.

### 6.2 The weekly re-harvest

Runs on a GitHub Actions runner, not on a cluster, and therefore costs **$0**.
`docs/corpus-freshness.md` records why: a Databricks job cluster would spend
~300 s of VM startup and a DBU-hour rate to do what a free runner does with an
HTTP client. One run is about two minutes forty, most of it the politeness gate
waiting.

### 6.3 The standing cost — the largest line in this document

| | rate | per month |
| --- | --- | ---: |
| NAT gateway in `rg-chip-chat-databricks-managed` | $0.045/hour | **$32.85** |
| its static public IP | $0.005/hour | $3.65 |
| Container Registry, Basic | | ~$5.00 |
| | | **$41.50** |

Both gateway charges are in the Databricks *managed* resource group, created with
the workspace, deleted only with the workspace, and billing every hour whether or
not a cluster exists. Month to date they are $1.4478 of `Standard Gateway` — 24.6%
of the entire Azure bill — for a resource whose job is to give job clusters
outbound internet for a few minutes a night.

`infra/README.md` records the one available lever and why it has not been pulled:
`no_public_ip = false` would remove the NAT gateway, requires the workspace to be
replaced, and is a security downgrade. That trade is not obviously worth $36.50 a
month and it is not obviously not; it is written down rather than decided.

### 6.4 Eval runs

| | cost | how it was measured |
| --- | ---: | --- |
| One full golden-set run | ~0.7 credits ≈ **$2** | 10 of 34 cases route to `ask_account_question` |
| One `make snowflake-verify` | ~0.5 credits ≈ **$1.50**, 5 min 7 s | monitors read before and after; timed 2026-08-27 |
| `make retrieval-baseline` | 40 of the month's 1,000 free semantic requests | `Makefile:471` |
| `make search-verify` | 3 semantic requests + a few thousand embedding tokens | `Makefile:589` |
| Every other `*-check`, `adversarial`, `trajectory`, `grounding`, `dietary` target | **$0** | they never reach a model |

The two live suites — `make adversarial-live` and `make adversarial-writegate` —
spend the deployment's own tokens and therefore land in section 2's Foundry
Models line rather than here.

**The shape to watch is not a sweep, it is a loop.** At $2 a golden-set pass the
arithmetic only bites in the hundreds of passes. An agent retrying
`ask_account_question` in a tight loop spends the trial in an afternoon and
violates no setting in `snowflake/sql/`.

### 6.5 Online-eval judge spend

**There is no judge anywhere in the tree**, so the honest figure is zero and the
honest caveat is that it is zero because nothing has been built, not because
anything is cheap. [#72] and [#76] are where one lands.

`docs/red-team.md` records the structural problem waiting there, and it belongs
in this document too: `BudgetLedger` is an in-process, in-memory counter, and a
judge run by Arize against exported traces is **out of process by
construction**. Meeting #87's "online-eval judge spend included" criterion needs
the ledger to move somewhere shared *before* the judge arrives, not after. A
judge scoring every turn at, say, gpt-5-mini rates against a 5,400-token turn
would roughly double the model half of this document — which is still under a
cent, and still the cheapest thing on the bill.

---

## 7. Egress, now that the publish is cross-cloud

[#104] made the nightly publish cross-cloud — Databricks on Azure writing to
Snowflake on AWS — and the original #88 checklist predates that, so this is the
line that had to be added. The answer is a small surprise in three parts.

**Snowflake charges nothing.** Snowflake does not charge data ingress fees. The
receiving half of a 1.53 MiB cross-cloud write is free.

**Azure internet egress charges nothing either, at this volume.** Standard Data
Transfer Out is free up to 100 GB/month and $0.087/GB after. The nightly publish
contributes about **0.0002%** of that allowance. The bill agrees: `Bandwidth /
Standard Data Transfer Out - Free` is a line item reading exactly `$0.0000`.

**What charges is the NAT gateway, and mostly for existing.** Traffic from a
Databricks job cluster leaves through the managed NAT gateway, which bills
`Standard Data Processed` at $0.045/GB. Month to date that meter is **$0.0325**.
The same gateway's `Standard Gateway` meter — the hourly charge for the resource
being there at all — is **$1.4478**. **The gateway costs 44 times more to exist
than to carry everything this project has ever sent through it.**

So the cost of moving the marts across the ocean is about five cents per hundred
gigabytes, the eleven tables occupy about a fifth of a megabyte, and the entire
egress question is a rounding error sitting next to a fixed cost that is not.
Section 6.3 is where the money actually is.

---

## 8. Two zeroes that are not free

Both of these are cost decisions with a consequence that is not financial, and
they belong in a cost document precisely because a cost review would otherwise
record them as wins.

**Azure AI Search on the Free tier silently returns empty vector results under
load.** `docs/retrieval.md` §9 is the defect report: a vector query returns HTTP
200, no error, no warning, and `"value": []`, at a rate that rises from ~25% on a
rested service to ~85–90% after a few hundred queries. A hybrid query fuses two
rankers by reciprocal rank and has no field saying which ranker contributed, so
when the vector half returns nothing the application receives a well-formed
hybrid response that is silently lexical-only. **The $73.73/month that the Free
tier saves is a real saving and this is its real price.** The reranked arm that
production sends is unaffected in every measurement, so the blast radius is the
degrade path and the ablation's vector arms — but "we saved $73.73 and hybrid
retrieval is sometimes not hybrid" is the whole sentence, and half of it is not a
cost finding.

The Free tier's semantic ranker also caps at **1,000 requests a calendar month,
about 33 a day**, and past the cap a request *fails* rather than costing a
dollar. `make retrieval-baseline` spends 40 of them in one command.

**Content Safety and Document Intelligence are both on F0.** Content Safety F0 is
5,000 text records and 5,000 images a month at 5 requests per second, and every
inbound turn is screened before the model. At 5,000 records the demo would need
about 625 conversations of 8 turns in a month to exhaust it, which is well past
any volume this URL will see — but the failure mode past the cap is a **refused
turn**, because `chip_chat.api.moderation` is private to `SpendGate` and an
unreachable moderator refuses the turn with no model call rather than skipping
the screen. That is the correct behaviour and it means the free tier is, in the
limit, an availability ceiling on the whole app.

---

## 9. Reconciling the spans against the meter, and why they disagree

They disagree by roughly a factor of two, and the reason is worth writing down
because it will happen again to whoever checks this next.

| | tokens |
| --- | ---: |
| `llm.completion` spans, `gpt-5-mini`, all of 2026-08-27 | 360,456 prompt / 61,346 completion |
| implied by the meter at published rates | 172,236 input / 33,243 output |
| ratio | **0.48 / 0.54** |

**It is the lag, and it is provable.** Every `llm.completion` span in Application
Insights carries a `2026-08-27` timestamp — there is no earlier traffic at all —
and Azure Cost Management lags 8 to 24 hours. The meter has billed roughly half
of one day and has the other half still in flight. Nothing is missing from
either surface; they are looking at different amounts of the same afternoon.

Two smaller reconciliations that do close:

- **The 429s do not double-count.** 26 turns emitted no `chip_chat.tokens.*`
  rollup and 26 turns failed with `openai.RateLimitError`. A refused call is not
  billed and does not appear in either number.
- **`gpt-4.1-mini` and `text-embedding-3-small` appear on the meter and not in the
  spans.** $0.0104 and $0.0003 respectively, from local eval runs and from
  building the search index — not from the deployed app, which has never run the
  vision lane. The absence is correct.

**The practical consequence: use the spans for anything you need today and the
meter for anything you need to be right about.** The spans are complete within
seconds, carry a lane and a session, and cost nothing to query. The meter is
authoritative, arrives tomorrow, and knows about the fourteen services the spans
have never heard of.

---

## 10. The other cloud: Snowflake, at 10.3 credits

`SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY`, whole account, 2026-08-27:

| Service type | credits | ≈ USD |
| --- | ---: | ---: |
| `WAREHOUSE_METERING` | 5.9857 | $17.96 |
| **`AI_SERVICES`** (Cortex Analyst) | **4.2880** | **$12.86** |
| `AI_INFERENCE` | 0.0001 | — |
| `TELEMETRY_DATA_INGEST` | 0.0000 | — |
| **Total** | **10.2738** | **≈$30.82** |

And by warehouse:

| Warehouse | credits | monitored? |
| --- | ---: | --- |
| `CHIP_CHAT_SERVING_WH` | 2.7746 | yes — daily, 4 credits |
| **`COMPUTE_WH`** | **2.2428** | **no daily monitor** |
| `CHIP_CHAT_PUBLISH_WH` | 0.9554 | yes — daily, 2 credits |
| `CLOUD_SERVICES_ONLY` | 0.0129 | n/a |

**`COMPUTE_WH` has burned 22% of the account's credits and more than the publish
warehouse.** It is the warehouse Snowflake created at signup, `snowflake/sql/`
does not manage it because it did not create it, it auto-suspends after 600
seconds rather than 60, it has query acceleration on, and it is still the default
in the `snow` connection — so every ad-hoc `snow sql` without a `USE WAREHOUSE`
wakes the most expensive idle warehouse in the account for ten minutes. Until
today no monitor watched it. Section 11 is what was done about that.

**Snowflake is five times the Azure bill.** $30.82 against $6.02, on an account
three days old, and 42% of it is one serverless product invoked sixty-four times.

---

## 11. The trial clock

| | |
| --- | --- |
| Started | 2026-08-25 |
| **Expires** | **2026-09-24**, or $400 of credits, whichever comes first |
| Allowance | ~130 credits at Enterprise's roughly $3 each |
| **Spent** | **10.3 credits — 8%** |
| **Remaining** | **~120 credits, 28 days** |
| Account-wide cap | **`CHIP_CHAT_TRIAL_MONITOR`, 80 credits, set 2026-08-27** |

The cap is section 11 of `docs/runbook.md`'s concern operationally and section
8's of `docs/snowflake-account.md` architecturally; what belongs here is the
arithmetic behind the number 80, so a reader can redo it rather than take it:

```
remaining balance                                        ~120 credits
warehouse burn, 2026-08-25 to 08-27                          6 credits
                                                       ≈ 2 credits/day
projected legitimate warehouse burn, 28 more days        ~56 credits
cap                                                       80 credits
  → clears the projection by 43%
  → leaves ~40 credits if a runaway ever trips it, against the
    ~1 credit a rebuild costs
```

**And the cap does not count Cortex.** A resource monitor watches virtual
warehouse credits; Cortex Analyst is serverless. So the 42% of this account's
spend that is the account lane is outside every guardrail the account has, and
the only thing bounding it is that nothing calls it in a loop. That is a fact
about the current code, not a control.

---

## 12. What this document does not measure

Listed rather than estimated, because an estimate presented beside measurements
reads like one.

- **Arize AX at observed volume is $0, and the volume is real but the tier is
  not.** 641 spans and 78 exception records were emitted in one day of Phase 8
  and Phase 10 verification, against AX Free's 25,000 spans a month — about 2.5%
  of the allowance in a busy day, so roughly 39 such days fit. But the deployed
  app exports only to Application Insights (`var.otlp_endpoint` is `""`, [#78]),
  so nothing has ever been sent to Arize and the $0 is a projection from a span
  count, not a bill.
- **The personalization lane's mart query.** Assumed equal to the account lane's
  225 ms SQL because both run on `CHIP_CHAT_SERVING_WH`; never timed separately.
- **A vision turn end to end.** The `gpt-4.1-mini` figure is from
  `make verify-vision`, a single 990-token image. No photo has been through the
  deployed app.
- **A knowledge turn with real retrieved context in its prompt.** Section 3's
  `search_menu_knowledge` turns answered from three hardcoded menu items. A real
  chunk-carrying prompt will be larger and the number will move.
- **`text-embedding-3-small`'s price**, which is not recorded anywhere in this
  repository. The meter says the project has spent $0.000282 on it in total,
  which is why nobody has needed to look it up.
- **Databricks credits against the free-trial allowance**, as opposed to DBU
  dollars on the Azure bill. Both exist; only the second is read here.
- **A month.** Every figure in this document is from a three-day-old account and a
  six-hour-old revision. #88's "compared against the first real bill" cannot be
  satisfied until there is one.

---

## 13. The steady-state estimate, written down to be checked against a bill

Required by [#88], and offered as an estimate rather than a measurement:

| | per month |
| --- | ---: |
| NAT gateway + public IP | $36.50 |
| Container Registry, Basic | $5.00 |
| Nightly publish (Snowflake credits + DBUs + VM) | $7.05 |
| AI Search (Free), Content Safety (F0), Document Intelligence (F0) | $0.00 |
| Container Apps at `min_replicas = 0`, Functions on FC1 | $0.00 |
| Log Analytics under a 1 GB/day cap | ~$0.00 |
| Model tokens, 200 conversations | $3.12 |
| **Subtotal, Azure** | **$51.67** |
| Snowflake, 200 conversations with one account question each | ~13 credits ≈ $40.00 |
| Arize AX Free | $0.00 |
| **Total** | **≈$92** |

Against the $150/month budget in `infra/terraform/cost.tf`, whose 50% alert at
$75 was set on the expectation of a $30–60 steady state. **The estimate crosses
that alert, and the two things pushing it over are the account lane and a NAT
gateway** — neither of which was in the original arithmetic. The budget alert is
correctly calibrated to be surprising, and it would be surprised.

Month to date on 2026-08-27, three days in, actual Azure spend is **$6.02** and
`az consumption budget list` reports the budget's own `currentSpend` as **$3.24**
— the same lag from section 9, showing up in a second place.

---

## 14. The guardrail audit

[#88] is a verification pass rather than a design task: every guardrail in the
plan is easy to configure and easy to quietly lose to a later change, so each one
is checked here against the *running system* rather than against the intention.
Everything below was read out of the deployed estate on **2026-08-27**, with the
command that read it, so a reader can redo any line rather than trust it.

| Guardrail | Observed | Verdict |
| --- | --- | --- |
| **AI Search** on the tier decided in #10 | `srch-chip-chat-4cy39i`, sku **`free`**, 1 replica, 1 partition; meter reads `Free Unit`, $0.0000 | ✅ Basic's $73.73/month unspent — **but see §8** |
| **Databricks**: no all-purpose cluster left running | every cluster `TERMINATED`; every one is `job-*-single-node` or `dlt-execution-*`; `cluster_source` is `JOB` or `PIPELINE` throughout, never `UI` or `API` | ✅ clean |
| **Databricks**: single-node, auto-terminate at 10 minutes | `databricks_autotermination_minutes = 10`, `Standard_F4ads_v7` single node | ✅ |
| **Snowflake**: X-Small, 60-second auto-suspend | both `CHIP_CHAT_*_WH` are `X-Small`, `auto_suspend = 60`, `enable_query_acceleration = false`; suspension watched three times at 63/66/68 s | ✅ |
| **Container Apps**: minimum replicas zero | `properties.template.scale.minReplicas = 0`, max 1, 0.25 vCPU. The evidence gathered before this pass read `null` and needed confirming; the deployed value is **0** | ✅ confirmed |
| **Blob uploads** expire after 24 h | policy `expire-uploads`, enabled, `blockBlob`, prefix `uploads/`, **`daysAfterCreationGreaterThan: 1.0`** | ✅ configured; expiry **not yet observed** — `make infra-check-uploads` is the way |
| **Model deployments** deliberately low | `gpt-4.1-mini`, `gpt-5-mini`, `text-embedding-3-small`, all `GlobalStandard`, all **capacity 10**; Terraform *refuses* any `Provisioned*` SKU | ✅ — and the 429s in §3 are this working |
| **Budget alerts** | `chip-chat-monthly`, $150.0, Monthly, four notifications, `currentSpend` $3.24 | ⚠️ present; **never observed to fire** (`cc-05h`) |
| **Inline cap verified independently** | #85; `guard.budget_check` median **0 ms** over 77 deployed spans | ✅ |
| **Arize**: Phoenix through dev, AX only when justified | AX not purchased; `var.otlp_endpoint = ""`; #78 open | ✅ |
| **Snowflake egress** — *added by this pass; #104 made the publish cross-cloud after the original list was written* | Snowflake ingress $0; Azure internet egress $0 under 100 GB; NAT gateway `Standard Data Processed` **$0.0325** MTD against `Standard Gateway` **$1.4478** | ✅ priced — see §7 |

### The three things that were not right, and what was done

**1. Both resource monitors notified nobody.** `SHOW RESOURCE MONITORS` returned
`notify_users: ""` on `CHIP_CHAT_SERVING_MONITOR` and
`CHIP_CHAT_PUBLISH_MONITOR`, so every `DO NOTIFY` trigger in
`snowflake/sql/05_resource_monitors.sql` had been firing into nothing since the
monitors were created. This is not drift — `snowflake/sql/` deliberately does not
set `NOTIFY_USERS`, because a checked-in file cannot know the operator's
Snowflake user name and re-asserting the list on every apply would revoke
whoever had been added since. It is the once-by-hand step
`docs/snowflake-account.md` §8 item 8 describes, and nobody had done it.

**Fixed**, and the fix is provable rather than hopeful: `DESC USER GRAM` reports
`IS_EMAIL_VERIFIED = true` against `grahamganssle@gmail.com`, which is the
condition Snowflake requires before a `DO NOTIFY` trigger actually mails
anybody — an unverified address would have left the guardrail looking configured
and still delivering nothing.

```sql
ALTER RESOURCE MONITOR CHIP_CHAT_SERVING_MONITOR SET NOTIFY_USERS = ('GRAM');
ALTER RESOURCE MONITOR CHIP_CHAT_PUBLISH_MONITOR SET NOTIFY_USERS = ('GRAM');
```

Both now report `NOTIFY reaches GRAM` in `make snowflake-verify`, and the setting
survives `make snowflake-apply` because the apply's `ALTER` re-asserts only the
quota and the triggers. It does **not** survive `make snowflake-rebuild`.

*The check that would have caught it already existed* — `verify.py` prints the
recipient list as evidence on every monitor check, so an empty list was visible
rather than assumed. It was visible and unread, which is a different failure and
one no check fixes.

**2. There was no account-wide credit cap.** Only the two daily warehouse
monitors existed. Their quotas add to 6 credits a day, and 28 days of that is
168 credits against a remaining balance of ~120 — so the daily ceilings catch a
loop but are not a budget, and `snowflake/sql/optional/trial_credit_cap.sql` has
been sitting unrun since it was written. It matters more than it looks: an
account-level monitor is **the only one that counts `COMPUTE_WH`**, which §10
shows has burned 22% of this trial from ad-hoc `snow sql` sessions.

**Fixed.** `make snowflake-cap QUOTA=80` — 14.9 s — creating
`CHIP_CHAT_TRIAL_MONITOR` at 80 credits, `FREQUENCY = NEVER`, notify at
50/75/90%, suspend at 100%, suspend-immediate at 110%. Section 11 has the
arithmetic behind 80. The `make snowflake-verify` run immediately after it read
**100/101**, with the check that had been failing by name — *"the trial has a
total credit cap, not just a daily one"* — now reporting
`CHIP_CHAT_TRIAL_MONITOR is set on the account: quota 80 credits, 0.23 used,
frequency NEVER, suspends at [100]`.

**3. `make snowflake-verify` was failing on a fact that is not drift.** The
remaining failure after the two fixes above was
`CHIP_CHAT_SERVING_MONITOR caps CHIP_CHAT_SERVING_WH at 4 credits a day`, with
the detail `notify=[100, 50, 80]`. The monitor has exactly the three triggers
`account.py` asks for; `SHOW RESOURCE MONITORS` simply does not return them in
ascending order, and `_percentages()` compared an ordered tuple against a
constant written ascending — its docstring even asserted the format as
`50%,80%,100%`.

**Fixed** in `snowflake/src/chip_chat/snowflake/verify.py`: `_percentages()` now
sorts. A trigger set is a set, two monitors that notify at the same three
thresholds are the same guardrail, and **a check that fails on something that is
not drift is worse than no check** — because the next person reads `100/101` and
stops reading, which is exactly what happened to finding 1.

**After all three, `make snowflake-verify --no-watch` reports `101/101 checks
passed`**, run 2026-08-27, with all five `#88` checks green and both monitors
reporting `NOTIFY reaches GRAM`. That is the evidence for this section, and it
costs about half a credit and five minutes seven seconds to reproduce.

The run *before* any of the three fixes was never captured — the first verify of
the afternoon was made after the `NOTIFY_USERS` change and the cap, and read
`100/101`. Both of the failing checks are in the count of 101, so the untouched
account would have read `99/101`. That is arithmetic rather than an observation
and is written here as one. (An empty `NOTIFY_USERS` never affected the count; it
was reported as *detail* on a check that was otherwise passing, which is
precisely how it stayed unnoticed.)

### The checks this pass added

**`test_trigger_percentages_compare_as_a_set_and_not_as_a_list`**, in
`snowflake/tests/test_credit_cap.py`, is finding 3's regression test: it feeds
`_percentages()` the exact string the live account returned and holds the result
against the constant the monitor check compares with, so a retune of the
thresholds moves the test rather than leaving it passing about nothing. It runs
in `make ci` and needs no account.

Beyond that: `make snowflake-verify` already covers the Snowflake half
thoroughly, and
`make infra-check-uploads` covers the blob half. What had no check at all was the
thing this document is: **the reconciliation between what the spans say a
conversation cost and what the meters say the project was billed.** §9 does it by
hand and §1 records the commands. It is not automated, and the reason is honest
rather than principled — a target that queries Cost Management would have to be
run by a human with `az login`, and `Makefile:168` already argues that a gate
which costs money and needs a logged-in human is not a gate.

### The two guardrails that are still open

- **The budget alert has never fired.** $150/month, four notifications
  configured, `currentSpend` $3.24 — nothing has crossed 50%, so nothing has been
  proven to deliver. `cc-05h` records that the test notification was never sent.
  §13 estimates a steady state that *would* cross it, so this stops being
  theoretical the moment the demo runs for a month.
- **Nothing bounds Cortex Analyst.** Resource monitors count virtual warehouse
  credits and Cortex is serverless, so the single most expensive thing in this
  system is outside all three monitors and outside `api/guard.py`'s token ledger.
  §4 records the three options and recommends one.

---

[#72]: https://github.com/gganssle/chip_chat/issues/72
[#76]: https://github.com/gganssle/chip_chat/issues/76
[#78]: https://github.com/gganssle/chip_chat/issues/78
[#87]: https://github.com/gganssle/chip_chat/issues/87
[#88]: https://github.com/gganssle/chip_chat/issues/88
[#104]: https://github.com/gganssle/chip_chat/issues/104
