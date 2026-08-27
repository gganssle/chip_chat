# Decision: Snowflake stays on AWS us-east-2, and the latency targets move

**Issue:** [#104](https://github.com/gganssle/chip_chat/issues/104) · **Decided:** 26 August 2026, by Graham · **Measured:** 27 August 2026
**Changes:** [#41](https://github.com/gganssle/chip_chat/issues/41) (the region constraint), [#45](https://github.com/gganssle/chip_chat/issues/45) (cross-region inference), PRD §05 (the turn-latency targets)
**Does not change:** Azure stays in East US 2

---

Every planning document in this repository assumed the serving layer would sit
in **Azure East US 2**, beside everything else. The trial was created on **AWS
us-east-2 (Ohio)**, account `GS74649` / locator `HQ72718`, on 2026-08-25. A
Snowflake account's region is fixed at signup and cannot be changed, so by the
time anybody noticed, the choice on the table was not "which region" but "this
account or a fresh trial".

The reason it matters is one product: **Cortex Analyst is not natively
available in AWS us-east-2.** Its native regions are `us-east-1`, `us-west-2`,
`eu-central-1`, `eu-west-1`, `ap-northeast-1` and `ap-southeast-2` on AWS, and
East US 2 and West Europe on Azure. Everywhere else reaches it through
**cross-region inference**, which processes the request in another region and
charges the round trip in latency.

## The decision

**Keep the trial. Enable cross-region inference. Re-baseline the latency
targets against a measurement rather than a guess.**

The alternative was a second signup on Azure East US 2, matching the design
exactly, at the cost of the 30-day clock and roughly $400 of credits already
started. That was declined. The reasoning recorded on #104 is the one worth
keeping: *discovering a missed target in Phase 9 is worse than conceding it in
Phase 4*, so the concession is made deliberately, in writing, with the old
numbers left beside the new ones.

`CORTEX_ENABLED_CROSS_REGION` is set to **`AWS_US`** on the account rather than
to `ANY`. Snowflake's own guidance is to target AWS US regions from an AWS US
account for the shortest hop, and `ANY` would allow a request from Ohio to be
answered from Frankfurt on a busy afternoon. `make snowflake-verify` fails if
the parameter is `DISABLED` or unreadable, because in this region a disabled
setting is not a slower account lane — it is an account lane that cannot answer
at all.

## What it costs, measured on 2026-08-27

Seventeen questions — the seven in `semantic.VERIFIED_QUERIES` and the ten in
`semantic.UNANSWERABLE` — asked of `/api/v2/cortex/analyst/message` against
`CHIP_CHAT.ACCOUNTS.ACCOUNT_LANE`, one call each, through the shipping
transport (`chip_chat.snowflake.cortex.HttpAnalystTransport`) and the shipping
decision function, from a laptop on domestic broadband with the warehouse warm.

| | round trip, ms | `analyst_latency_ms` |
| --- | --- | --- |
| min | 3,111 | 2,404 |
| **median** | **4,248** | **2,973** |
| p95 | 6,567 | 3,934 |
| max | 11,648 | 3,965 |

`analyst_latency_ms` is Snowflake's own figure for the time the Analyst service
spent, and it is the one that transfers to a deployment: the round trip carries
this laptop's leg as well. That leg was measured the same afternoon, over the
same pooled client and the same credential, as a `SELECT 1` on
`/api/v2/statements` — **median 316 ms**, n = 20. So a request from inside Azure
would still pay the ~3.0 s, and would pay a different and smaller number for
the trip to Ohio.

The SQL those calls produce executes on `CHIP_CHAT_SERVING_WH` in a median of
**225 ms** (p95 2,048 ms, n = 99 — `docs/snowflake-semantic-view.md` §4). The
account lane's two Snowflake hops are therefore roughly **3.0 s of inference
and 0.2 s of query**, and the ratio is the finding: this lane spends thirteen
times longer being told what SQL to run than it spends running it.

The model doing the inference reports itself as `claude-sonnet-4-6` on every
one of the seventeen responses. It is not running in Ohio.

### The number that could not be measured, and why it is not in the table

#104 asks for "the real per-query penalty" — the *difference* between
cross-region and native. **That difference cannot be isolated on this account,
and two attempts to isolate it are worth recording so nobody spends the
afternoon again.**

The first was the obvious control: ask Cortex Analyst the same question with
cross-region inference off. There is no such condition. Analyst has no native
path in us-east-2 at all; with `CORTEX_ENABLED_CROSS_REGION = DISABLED` the
account lane does not answer more slowly, it does not answer.

The second was to move the control to a cheaper Cortex surface —
`SNOWFLAKE.CORTEX.COMPLETE` with a fixed prompt against a model served in
us-east-2 and a model served from elsewhere, which would have priced the hop
without pricing Analyst's reasoning. Every model tried, native and not, returns
the same refusal: **`SNOWFLAKE.CORTEX.COMPLETE` is not available for trial
accounts.** The parameter was flipped to `DISABLED`, thirteen models probed,
and restored to `AWS_US` in the same run; the finding is the refusal, not the
latency.

So the re-baseline below is done against the **total**, not against a delta.
That is the conservative direction — a target set against the whole hop cannot
be missed because the hop turned out to be bigger than the part attributable to
geography — and it is the only direction the evidence supports.

## The re-baseline

PRD §05 specified a **median turn latency under 2 s** and a **95th percentile
under 4 s**, for every turn of every lane, measured in Application Insights.
Those numbers were set against a co-located, natively-supported serving layer.
The account lane's *first hop alone* now has a 2.97 s median, so the original
targets are not reachable for that lane and were never going to be reachable by
tuning.

The revision splits the target by lane rather than weakening it everywhere,
because only one lane pays this:

| | old | new |
| --- | --- | --- |
| Median turn latency — knowledge, action, vision, personalization | < 2 s | **< 2 s**, unchanged |
| p95 turn latency — knowledge, action, vision, personalization | < 4 s | **< 4 s**, unchanged |
| Median turn latency — **account lane** | < 2 s | **< 5 s** |
| p95 turn latency — **account lane** | < 4 s | **< 8 s** |

The arithmetic, so a reader can redo it rather than take it:

```
median  2.97 s Analyst  +  0.23 s SQL  +  1.80 s for the rest of the turn  =  5.0 s
p95     3.93 s Analyst  +  2.05 s SQL  +  2.02 s for the rest of the turn  =  8.0 s
```

"The rest of the turn" is the agent's own model call, the guards and the
render, and the budget left for it is the same budget the original 2 s / 4 s
targets implied for a whole turn. Nothing has been given away there. What has
been given away is exactly the Snowflake hop, which is exactly what the region
costs.

**Both numbers are provisional in one specific way**, and it is named here so
the next measurement is a confirmation rather than a surprise: every figure
above was taken from a laptop, not from `ca-chip-chat-web` in East US 2. The
Analyst service time transfers; the client leg does not. [#61] owns the tool
that will be timed end to end, and [#86]'s go/no-go scores against **these**
targets, not the originals.

## What this does not change

**The Azure region.** East US 2 remains correct for Foundry Agent Service's
full tool matrix and for complete Content Safety coverage, and nothing about
Snowflake's region touches either. `docs/service-inventory.md`'s region
recommendation still holds for everything except the sentence that extended it
to the Snowflake account.

**D2's split.** "Snowflake serves, Databricks computes" was a decision about the
clock, not about the cloud. Snowflake is still the governed low-latency store
relative to a lakehouse query. It is simply less low-latency than planned, and
by a number that is now written down.

**The isolation guarantee.** Cross-region inference sends the *question* out of
the region. It does not send rows: the SQL comes back and executes on
`CHIP_CHAT_SERVING_WH` under [#43]'s row access policies, on a session bound to
one visitor. All seventeen responses were checked for the obvious failure —
none of the generated SQL names `demo_id`, and `demo_id` is not in the semantic
view for it to name.

## The two costs that are not latency

**Cortex Analyst bills 67 credits per 1,000 messages**, confirmed rather than
read: `SNOWFLAKE.ACCOUNT_USAGE.CORTEX_ANALYST_USAGE_HISTORY` shows 47 requests
billed at 3.149 credits on 2026-08-27, which is 0.067 each — about **$0.20 a
question** at Enterprise's roughly $3 a credit. PRD §05's *cost per conversation
under $0.05* cannot survive a conversation that asks two account questions, and
that is a cost finding rather than a latency one. It belongs to [#88] and is
recorded in `docs/snowflake-account.md` §10 rather than argued here.

**The nightly publish is now cross-cloud** — Databricks in Azure East US 2
writing to Snowflake in AWS us-east-2. It is a batch off the hot path, so the
latency is irrelevant, but the egress is not free and is [#39]'s and [#88]'s to
price. Nothing in this decision measures it.

[#39]: https://github.com/gganssle/chip_chat/issues/39
[#41]: https://github.com/gganssle/chip_chat/issues/41
[#43]: https://github.com/gganssle/chip_chat/issues/43
[#45]: https://github.com/gganssle/chip_chat/issues/45
[#61]: https://github.com/gganssle/chip_chat/issues/61
[#86]: https://github.com/gganssle/chip_chat/issues/86
[#88]: https://github.com/gganssle/chip_chat/issues/88
[#104]: https://github.com/gganssle/chip_chat/issues/104
