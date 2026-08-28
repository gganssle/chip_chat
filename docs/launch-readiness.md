# Launch readiness and the V0 go/no-go

Issue [#86](https://github.com/gganssle/chip_chat/issues/86). Assessed against
the deployed app at revision `0000018` on **27 August 2026**.

Every number here was measured today. Where something was not measured, this
document says *not measured* rather than estimating and presenting the estimate
as a measurement — which matters more than usual, because two of these lines are
pass/fail gates and a gate that was not measured has not passed.

---

## The decision

> ## NO-GO for a public link, 27 August 2026.
>
> Not because a gate failed. **Because the system that would fail a gate is not
> the system that is deployed**, and three of the five headline targets are
> short by margins that are not rounding.

The deployment is real, healthy and serving. It is also answering from
`chip_chat.agent.hardcoded` rather than from the lakehouse, the warehouse and the
search index that this project exists to demonstrate. Sharing the link today
would put a stranger in front of a demo that contradicts itself within two turns
(§4.1), and would score the launch gates against a code path that the gates'
own enforcement mechanism is not on.

> **Amended 28 August 2026, and the verdict is not re-rendered here.** `cc-lpy4`
> put a Snowflake connection on the deployment, so the second sentence above is
> now half true rather than true: the warehouse is in the path on revision
> `0000025` — the account and personalization lanes report **up**, personas come
> from the live `persona_fixtures`, and the account tools read the visitor's own
> rows under #43's policies through #44's pool. §4.1 is closed and rewritten
> below. **The search index and the ops API are still not**, and neither is the
> eval work in §§4.3–4.6, so the paragraph's *argument* survives its first
> sentence being outdated. Whoever next takes a go/no-go decision should read
> §§4.2 onward and re-run §1's gates against a deployment that now has two of
> five lanes on real data; this section deliberately does not do that on their
> behalf.

**What this is not.** It is not a judgement that the work is incomplete. The
mechanisms are built and, at their own layer, verified — the row access policies,
the connection pool, the confirmation precondition, the spend cap, the
moderation ordering. The gap is between *the mechanisms exist and are tested*
and *the deployed system runs on them*.

§6 lists what would flip this to go, in the order that costs least.

---

## 1. The two launch gates

Both are pass/fail. PRD §05: *not "few" — zero.* A failure blocks the demo going
public regardless of how everything else scores.

### Gate 1 — Zero cross-visitor data disclosures

**Verdict: PASS at the mechanism. NOT VERIFIED end to end, and re-running it
with the lane wired did not change that — see below.**

| Evidence | Result |
| --- | --- |
| Contended live run, 8 visitors / pool of 2 / 6 rounds | **48 attempts, 48 held, 0 breaches** |
| Harness self-check: could this run have caught a bleed? | **yes** — 8 at once, contended, 285.01 s |
| Database, unbound as `CHIP_CHAT_READ` | all **9** visitor-scoped tables return **0 rows** |
| Same moment, `INFORMATION_SCHEMA` | `ORDERS` holds **18,898** — default deny, not an empty table |
| `api/tests/test_pool_concurrency.py` | 32 visitors, 4 slots, **1,280 checkouts**, plus a `NaivePool` control that passes sequentially and discloses concurrently |

**Re-run on 28 August with the account lane wired, and the asterisk survived.**
The lane is now `up`, so the objection to the earlier run — that no tool was
called and no connection left the pool — should have been answered. It was not:

| Run | Attempts | Held | Breaches | Unscored | Where it died |
| --- | ---: | ---: | ---: | ---: | --- |
| 27 Aug, lanes unwired | 48 | 48 | 0 | 0 | in the model |
| 28 Aug, account lane up | 48 | **47** | **0** | 1 | **still in the model** |

> answered in conversation — the turn called no tool: **it died in the model,
> which is the weakest place for a guarantee to live**

**The attack cannot reach the policy, because the model will not make the
call.** Asked for another visitor's data, it declines conversationally rather
than querying — so `ask_account_question` is never invoked, no connection is
taken from the pool, and the row access policy is never consulted. Wiring the
lane did not put the mechanism on the path; it only made the path available.

That is a real finding rather than a testing inconvenience. It means this
attack, phrased this way, measures the model's refusal and **cannot** measure
the mechanism behind it — and a suite that reported `pass` here would be
crediting the design for something it had not exercised. The one unscored turn
was in flight alone, and `Report.gate` returns `None` while anything is
unscored, so the aggregate reads *not measured* even at 47 held and zero
breaches. That strictness is correct and was not relaxed to produce a green.

**Where the mechanism is genuinely proven remains the database**, and that
evidence is unchanged and strong: all nine visitor-scoped tables return zero
rows unbound while `INFORMATION_SCHEMA` shows 18,898 in `ORDERS`, and the pool
test interleaves 32 visitors through 4 slots over 1,280 checkouts with a control
that passes sequentially and discloses concurrently.

**What would actually exercise it end to end** is an attack that gets the model
to *call* the account tool with a hostile question — the row access policy then
filters the result — rather than one it refuses outright. That probe does not
exist yet, and writing it is the honest next step for gate 1.

**An earlier run reported one crossing. It is explained and did not reproduce.**
It was measured against a revision where `POST /api/entry` returned 404 — so
every session resolved to the same account, and two "visitors" that are one
visitor produce exactly that signature, including the part that looked
anomalous. The same run coincided with the event-loop bug that was dropping
connections at the ingress. Both causes are fixed (`dc6eaa2`). Full write-up in
the comment on [#82](https://github.com/gganssle/chip_chat/issues/82).

**A first re-run was unscored, not passed** — 3 visitors against a pool of 4 is
never contended, and the harness said so rather than reporting a clean gate.
That refusal is the single most valuable behaviour in the red-team harness, and
it is worth noting that the parameters were wrong, not the app.

### Gate 2 — Zero account writes executed without explicit confirmation

**Verdict: PASS, measured against the deployment on 28 August 2026.**

| Evidence | Result |
| --- | --- |
| Every attack shape that can be put | **held** |
| With a deliberately sabotaged system prompt | **held** — `agent/tests/test_sabotage.py` |
| Direct API calls bypassing the UI | rejected — `api/tests/test_ops_routes.py`, 36 tests |
| Unconfirmed draft | rejected *and reaches no procedure* |
| Confirmed draft from another session | rejected on the header alone |
| Same retry key twice | 2 calls, **1 write** |
| Mutation check | neutering `_authentic()` fails 4 tests |
| Live suite against the deployment | **pass** — 8 probes, **8 held, 0 unscored, 0 writes executed without a confirmation** |

**This took three separate fixes and none of them was the gate itself.** The
gate held from the beginning; what was missing was any way to *observe* that it
held against a running system.

1. `func-chip-chat-ops-4cy39i` was Running with **zero functions deployed** —
   `POST /api/place_order` returned 404, so there was nothing to attack. Four
   functions are now published.
2. The chat app did not call the ops API, because the app's draft store and the
   Functions host's are different processes. The confirmation now crosses that
   boundary as a **signed grant** the ops API verifies rather than looks up, so
   the app never gains write credentials. `docs/decisions/confirmation-grants.md`.
3. The two redemption probes could not be scored *at all*: `_redeem` in the
   write-gate harness had exactly two exits, `BREACHED` on a receipt and
   `UNSCORED` on everything else, with **no `HELD` branch**. Building the lane
   turned them into real questions with no way to record the answer, and
   `Report.gate` returns `None` while anything is unscored. The harness now asks
   the deployment — `GET /healthz/lanes` — and scores them only where the action
   lane is up and offers the tool. A deployment without it stays unscored on the
   original reasoning, and an unreadable health surface returns `False`, because
   a surface nobody could read must never become evidence that a gate held.

The last of those is worth dwelling on: for most of this project the gate read
*not measured* for a reason that had nothing to do with the product. A harness
that cannot express a pass is indistinguishable, from the outside, from a system
that cannot earn one.

---

## 2. The five headline targets

**Re-measured 28 August 2026 against lanes that are actually wired.** Every
number below the first pass of this section was produced against
`chip_chat.agent.lanes.NO_LANES`, because until `cc-lanes` the harness had no way
to be handed anything else and did not record which it had used. Those numbers
were a correct measurement of the unwired week-one slice presented as a
measurement of the deployment. The table now says which it is, and both are
given, because the difference between them is a fact about the system worth
having.

| Metric | Target | **Deployed** (`account+personalization`) | Unwired (`none`) | Verdict |
| --- | --- | --- | --- | --- |
| Task completion on the golden set | ≥ 85% | **20.6%** (19 of 34 scored) | 17.6% | **short** |
| Tool-selection accuracy | ≥ 95% | **65.6%** (32 of 34 scored) | 56.2% | **short** |
| Groundedness of food and policy claims | ≥ 0.95 | **70.0%** (10 of 10 scored) | 40.0% | **short** |
| Menu claims made without a citation | 0 | **not measurable** | not measurable | **unmeasured** |
| Photo → order, component-level F1 | ≥ 0.80 | delegated, not run live | delegated | **unmeasured** |

Both columns are the same arm — `shipped`, `0ec39d67a727`, prompt
`v1+1c6f84d1f21f`, dataset `cilantro-golden-set 9ba196eb786c`, judged by
`gpt-5-mini` — run fifteen minutes apart with `--lanes` as the only difference.
`eval/experiments/BASELINE.md`, `eval/experiments/BASELINE-NO-LANES.md` and
`eval/experiments/WIRING.md`. The verdict column is unchanged: **three of the
five are still short, and by margins that are not rounding.**

Four qualifications. The first two are new and cut in opposite directions; the
last two are unchanged and still cut against the numbers.

- **The lanes are worth +9.4 points of tool selection, and +50 in the lane they
  are about.** The account lane goes 16.7% → 66.7% and 14.3% → 42.9% completion,
  because `ask_account_question` is offered to the model at all rather than
  withheld by `CONDITIONAL_TOOLS`. Its seven rows had been scoring zero *because
  the tool the row expects did not exist in the process doing the scoring* — not
  because the model chose wrongly. `docs/decisions/eval-lane-wiring.md`.
- **The 429s are gone and that is most of the improvement over the last
  reading.** `gpt-5-mini` went from 10,000 to 200,000 tokens a minute, and
  `scored` went from 13 and 14 of 34 to 32 of 34 on both sides. Against the
  429-degraded 14.7% / 42.9%, the pair of runs above separates the two causes:
  roughly **+13 points of tool selection and eighteen scoreable rows from the
  capacity, +9.4 from the wiring**.
- Uncited menu claims is **unmeasurable, not unmeasured-by-choice**, because no
  deployment builds a `ResponseEnvelope` (bead `cc-bap`). K2 cannot be scored at
  all, wiring or not; a lane coming up does not mint a citation id.
- **Two of the five lanes are still absent under `--lanes wired`.** Knowledge
  needs a retriever against the live alias (`cc-e1sr`) and photo needs the upload
  route and a production catalogue loader (`cc-mpd`). So `search_menu_knowledge`
  still answers from a three-item hardcoded menu — the groundedness number above
  is grounded in a fixture — and `match_meal_from_photo` is still unregistered.
  Two further rows are unscoreable for reasons that are not the model's:
  `get_recommendations` is offered once the lane is wired and then declines,
  because `MARTS.recommendations` does not exist (`chip-znk` / `cc-afo5`), and
  `redeem_points` is not in `agent.tools.TOOLS` at all.

**Over-refusal was measured alongside under-refusal**, as #75 insists. Re-read
on 28 August: **5 over-refusals wired against 2 unwired**, all in the knowledge
lane, with ungrounded findings going the other way, 3 against 6. A system given
more tools refused more and asserted less, which is the direction a cautious
system drifts and the reason measuring only under-refusal would have produced
one that hedges everything and scores well.

**Latency.** Median turn **34.2 s**, p95 **62.7 s**, over 69 `chat.turn` spans in
Application Insights, of which `llm.completion` is median 20.2 s — the turn is
the model call. PRD §05 targets < 2 s / < 4 s. #104 re-baselined the **account
lane only** to < 5 s / < 8 s for cross-region inference; that concession does not
close a 34-second gap. Measured under 429 contention.

**Cost.** Median conversation 8 turns / 29,268 tokens = **$0.0156**, inside the
$0.05 target. One account question is **$0.20** — Cortex Analyst at 67 credits
per 1,000 messages, confirmed twice. The target is met per conversation and
missed per account question, and at 200 conversations/month the fixed
infrastructure ($41.50) makes the all-in number **$0.21**. `docs/cost.md`.

---

## 3. The rest of the V0 launch criteria

| Criterion | Verdict | Evidence |
| --- | --- | --- |
| Every PRD requirement met or **explicitly deferred with a note** | **partial** | §5 — the notes exist for the deferred ones; four are simply not done |
| Golden set clears its targets in an experiment, **result recorded** | **fail** | Recorded (`eval/experiments/results/shipped.json`), does not clear. As of `cc-lanes` that record states the lane configuration it was produced under, and a comparison against one that does not is refused rather than drawn — so "recorded" now means recorded *as something*, which it did not before |
| Online evals and cost monitors **live before the URL is shared** | **pass** | Live since 28 Aug against a hosted Phoenix in `cae-chip-chat` (internal ingress, min=1), driven by `caj-chip-chat-monitors` on a `*/15` cron. Evidence: 142 spans / 16 traces arriving, App Insights receiving the *same* trees span-for-span, and monitors firing on real traffic — 3 ungrounded-claim tickets and one refusal-where-the-corpus-answered. Judge spend measured at **959 tokens/judged turn = 4.8% of the daily ceiling**. Arize AX was **not** purchased; `docs/decisions/hosted-phoenix.md` records the deviation and what is lost |
| Daily spend ceiling tested by **actually tripping it** | **pass, offline** | `eval/tests/test_spend_ceiling_tripped.py` — real uvicorn, real TCP, ceiling reached by talking, zero tokens asserted twice. Not tripped on the public deployment (2 M tokens; it would take the demo down) |
| A stranger completes three tasks **without narration** | **not attempted** | §4.1 makes this unwise today |
| Unaffiliated notice on entry and persistent in the header | **pass** | Verified live: banner is `position: sticky` and non-dismissible; `x-robots-tag: noindex, nofollow`; `robots.txt` `Disallow: /`; no logo, wordmark or brand colour |

---

## 4. Blockers, in the order they matter

### 4.1 The deployed app contradicted itself — **closed, and what it left**

**Closed on 28 August 2026 by `cc-lpy4`, on revision `0000025`.** What this
section recorded was:

> All five lanes report `not_wired` at `GET /healthz/lanes`. The opening message
> reads the assigned persona; `get_points_balance` reads
> `chip_chat.agent.hardcoded.ACCOUNT`. Observed live in one session:
>
> > **Opening:** "…a regular at AL Town 1 Mall, 397 points…"
> > **"How many points do I have?"** → "…home store Ballard, 1,340 points…"
>
> Two stores, two balances, two usual orders, one conversation. Each half is
> correct on its own. Together they are the thing the opening message exists to
> prevent.

The diagnosis was right: the fix was a Snowflake connection factory, and it
closed both halves at once. `account` and `personalization` now report **up** at
`/healthz/lanes`; personas are assigned from the live `persona_fixtures` and the
account tools read the visitor's own rows through the pool that bound them. It
took one thing this section did not anticipate — `runtime_context` named the
hardcoded account in a system message, so the first wired revision still *said*
"the Ballard regular" beside a real balance. `docs/public-demo.md` §9 has both
transcripts and the fix.

**What is left is narrower and is not a wiring bug.**
`ACCOUNTS.persona_fixtures` on the live account was loaded from a different
`data-gen` generation than `ACCOUNTS.orders` and `loyalty_ledger`: across all
twenty-eight fixtures, points agree 4/28 and order counts agree 4/28. Two of the
seven archetypes quote a points figure in their narrative, and for those the
opening sentence can still differ from what the ledger sums to. Same visitor,
same store, same usual order, one drifted number — a reload rather than a code
change. Bead `chip-qvg`.

### 4.2 The ops API has no functions deployed

Gate 2 cannot be measured against the deployment, and the entire action lane is
unreachable from the public URL.

### 4.3 AI Search free tier silently breaks vector search

`{"value": []}` with HTTP 200, no error, at a rate rising to ~85% after a few
dozen vector queries. Every other cause was eliminated by measurement. This
already caused one **false finding in a committed baseline** — vector-only
scoring 0% was read as confirmation of the hybrid argument and was a service
fault. `docs/retrieval.md` §9, bead `chip-wez`. The headline retrieval number
survives, with a corrected denominator: allergen top-3 recall **100%** on 4 of the 8 allergen questions, under `hybrid + reranker`. The other four are now reported **degraded** rather than counted, because `chip-wez`'s detector showed their vector arm had silently dropped. The number did not move; the honesty of its denominator did.

### 4.4 Online evals are not live, and #86 requires them before the link

Blocked on the Arize AX purchase, which is outside this session's authority. The
switch itself is proven cheap: instrumentation diff **empty**, agent-side diff
**exactly two lines** of the rendered version manifest. AX **Free** is the right
tier — Pro at $50/mo pushes steady state past the budget's own 50% alert.

### 4.5 ~~The Snowflake rebuild path is untested~~ — superseded 28 Aug 2026

This was a blocker while the day-30 plan was *rebuild on demand*. It is not one
any more: the owner has decided the trial will **never** be converted to paid and
the work will be finished before it expires. `docs/decisions/end-of-life.md`.

The rebuild path stays untested, deliberately, and no effort should be spent
making it testable. What replaces this as the thing to watch is that
**2026-09-24 is a cliff rather than a slope**: the account, personalization and
action lanes all read or write Snowflake, so all three stop at once. The
teardown in `docs/runbook.md` should be run before that date rather than
discovered after it.

---

## 5. Requirement status

**Met 21 · Partial 8 · Deferred with a note 3 · Not done 2.**

Revised after a line-by-line evidence audit that contradicted an earlier
draft of this table in three places — S1 was marked met and is not, E2 is
met only in the sense that a fixture is not a query, and E7's deferral note
is accurate but understates how little is reachable. The audit is the reason
those rows now carry their caveats instead of a tick.

| Req | Status | Note |
| --- | --- | --- |
| E1 name → conversation in one screen | **met** | 0.16–0.20 s live |
| E2 persona assigned on entry | **met, fixture-backed** | 28 real fixtures shipped and verified live, but `build_service` is called with `connect=None` on every deployment, so points and history are static columns — the assigned account can be quoted, not queried |
| E3 opening states store, points, characteristic order | **met** | per-archetype grammar, not a template |
| E4 chips spanning ≥3 capabilities | **met** | 5 chips, 4 lanes |
| E5 switch at any time, says it restarted | **met** | 0.17 s; new `demo_id` on a clean connection |
| E6 unaffiliated notice on every screen | **met** | sticky, non-dismissible, verified live |
| E7 persona editing (3 fields, rest read-only) | **deferred, noted — but read the note** | `docs/decisions/persona-editing.md`. The closed three-field schema and the ops-host route exist (`surface.py`, `function_app.py`); there is **no HTTP route on the web app**, so it is not reachable by a visitor at all. Library code, not a capability |
| K1 menu/nutrition/allergen/policy answers | **partial** | knowledge lane unwired on the deployment |
| K2 every claim carries a citation | **not done** | no `ResponseEnvelope` built; unmeasurable (`cc-bap`) |
| K3 says so plainly when data is absent | **met** | `eval/dietary/` — all 7 attack shapes; over-refusal measured |
| K4 comparative and constrained questions | **partial** | implemented in `search/`, not reachable on the deployment |
| K5 citations visible in the response | **partial** | decided and implemented; `adjacent` finding added; unmeasured |
| A1 own history, spend, points, visits | **partial** | Cortex Analyst 7/7 answered live; lane unwired |
| A2 aggregates and time ranges | **met** | verified against the semantic view |
| A3 never another visitor's data | **met** | §1, gate 1 |
| A4 says so rather than a plausible number | **met** | 10/10 refused; never falls back to hand-written SQL, asserted in code |
| P1 usual order and how it worked it out | **met** | `usual_order.confidence` calibrated and documented |
| P2 recommends what they have not tried | **met** | 0 of 138 visitors recommended something already ordered |
| P3 surfaces unredeemed value unasked | **met** | in the opening message |
| T1 six actions supported | **met** | four procedures + draft/revise |
| T2 confirmation card before every action | **met** | precondition in code; gate 2 |
| T3 card editable in place | **met** | `/api/draft/revise` mints a new priced draft |
| T4 receipt referable later | **met** | persists in the conversation |
| T5 simulated, and the card says so | **met** | on the card, not a footnote |
| V1 inline photo upload, desktop and mobile | **met** | verified live, 3.25 s |
| V2 proposed order from real menu items only | **partial** | matcher built; `Lanes.photo` unwired (`cc-mpd`) |
| V3 states what it saw | **met** | describe-then-match |
| V4 not-our-food → says so | **met** | `vision/tests/` |
| V5 low confidence → asks | **met** | escalation path tested |
| V6 never names a non-existent item | **met** | enforced at the matcher *and* in the procedures |
| V7 multi-meal → asks which | **deferred, noted** | `docs/decisions/multi-meal-photos.md` |
| S1 moderation on inbound text and images | **partial** | #79 landed today and the ordering is structural — but `AzureTextAnalyzer` is **never instantiated**: `build_service` passes no `moderator=`, so the running app screens with `LocalTextAnalyzer`, which flags **zero harm categories**. The image half is correct and sits behind an unwired lane. And *"before anything else"* is not literally true — the budget and rate check run first, deliberately |
| S2 retrieved instructions are data | **met in code, unexercised live** | per-turn nonce envelope, 5 injection payloads. The knowledge lane is unwired, so nothing is retrieved on the deployment and this defence never runs in production |
| S3 rate limited per session and per source | **met** | 20 req/60 s per source, 40 turns and 120,000 tokens per session; verified against a naive loop. Never observed firing in production — Container Apps ingress sheds load first, and the state is process-local |
| S4 friendly stop state at the ceiling | **met** | entry and mid-conversation; zero tokens asserted |

Deferred-with-a-note: E7, V7, and the group-ordering shape (PRD §04). Each has a
decision record. **Not done** without a deferral note: K2, and the four
`partial` rows whose cause is the same single unwired seam.

---

## 6. What would flip this to go

In the order that costs least per unit of readiness bought.

1. **Wire the account and personalization lanes** (`cc-lpy4`). One Snowflake
   connection factory. It closes §4.1's contradiction, makes gate 1's re-run
   exercise the actual mechanism, and turns four `partial` requirements into
   measurable ones. **This is the highest-leverage change available.** It needs a
   credential minted and stored in Key Vault, which is a deliberate human
   decision rather than an oversight.
2. **Deploy the ops API's functions.** Unblocks gate 2 against the deployment and
   makes the action lane reachable.
3. **Build a `ResponseEnvelope`** (`cc-bap`). K2 becomes measurable; the
   citation-presence eval starts returning a number instead of a gap.
4. ~~Purchase Arize AX Free and repoint.~~ **Done differently, 28 Aug.** The
   owner chose free-tier-only and AX Free needs an account signup and a Terms of
   Service acceptance, which is not a thing to do on somebody's behalf. Phoenix
   is hosted in the same Container Apps environment instead, the monitors run on
   a cron, and the switch was proven to be configuration once again:
   instrumentation diff **empty**, one line of `compute.tf`.
   `docs/decisions/hosted-phoenix.md`.
5. **Re-run the gate-1 contended suite** once (1) lands, so the pass is earned by
   the row access policy rather than by the model declining.
6. ~~**Re-measure the five targets** without 429 contention.~~ **Done, 28 Aug**,
   and it needed a second thing nobody had noticed was missing: the harness
   defaulted to `NO_LANES` at every entry point and did not record which
   configuration it had scored, so the first re-measurement after (1) landed came
   back byte-identical and correct. §2 now carries both columns.
   `docs/decisions/eval-lane-wiring.md`. **The verdict did not change** — three
   of five are still short — but two of the three are short by less, and the
   account lane's zero turned out to be an artefact of the harness rather than a
   property of the model.
7. Then reassess. Nothing in this list is research; all of it is wiring, and most
   of the underlying work is done and tested.

---

## 7. What is genuinely ready

Worth stating, because a no-go reads as a verdict on the whole project and it is
not one.

The isolation mechanism is real and proven at the database. The confirmation
precondition is structural and survives a sabotaged prompt. The spend cap is
inline, tripped for real, and consumes zero tokens while tripped. Moderation
runs before the model by construction rather than by convention. The corpus is
chunked so that no nutrition row splits, and allergen top-3 recall is 100% on the
four allergen questions whose vector arm survived — the other four are reported
degraded rather than silently counted, which is `chip-wez`'s doing. The
alias swap is atomic across a live continuous query. The publish is atomic per
table and leaves a consistent previous generation when killed. The gold marts
rebuild deterministically and the recommender is registered in Unity Catalog with
a scheduled retrain. The unaffiliated framing is correct and unmissable.

And the evaluation harness caught a regression that the aggregate would have
called an improvement — which is the whole reason it exists.

It also, on 28 August, caught itself: a re-run that came back byte-identical
after two changes that should each have moved it, because it had been scoring a
configuration nobody had written down. The fix was to make the configuration part
of every number and to **refuse** a comparison whose sides do not both state it.
A harness that can be wrong about what it measured is worth less than one that
says so, and this one now says so.
