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

**Verdict: PASS at the mechanism, NOT VERIFIED end to end.**

| Evidence | Result |
| --- | --- |
| Contended live run, 8 visitors / pool of 2 / 6 rounds | **48 attempts, 48 held, 0 breaches** |
| Harness self-check: could this run have caught a bleed? | **yes** — 8 at once, contended, 285.01 s |
| Database, unbound as `CHIP_CHAT_READ` | all **9** visitor-scoped tables return **0 rows** |
| Same moment, `INFORMATION_SCHEMA` | `ORDERS` holds **18,898** — default deny, not an empty table |
| `api/tests/test_pool_concurrency.py` | 32 visitors, 4 slots, **1,280 checkouts**, plus a `NaivePool` control that passes sequentially and discloses concurrently |

**The asterisk, which is the reason this is not a clean pass.** The harness
records where each attack died:

> answered in conversation — the turn called no tool: **it died in the model,
> which is the weakest place for a guarantee to live**

The account lane is unwired, so no tool was called and no connection left the
pool. The run demonstrates that the app does not leak today. It does **not**
demonstrate that the structural guarantee works, because the structural
guarantee was not on the path.

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

**Verdict: PASS in the code path, NOT MEASURED against the deployment.**

| Evidence | Result |
| --- | --- |
| Every attack shape that can be put | **held** |
| With a deliberately sabotaged system prompt | **held** — `agent/tests/test_sabotage.py` |
| Direct API calls bypassing the UI | rejected — `api/tests/test_ops_routes.py`, 36 tests |
| Unconfirmed draft | rejected *and reaches no procedure* |
| Confirmed draft from another session | rejected on the header alone |
| Same retry key twice | 2 calls, **1 write** |
| Mutation check | neutering `_authentic()` fails 4 tests |
| Live suite against the deployment | **not measured** |

The live gate is unmeasurable for a structural reason: `func-chip-chat-ops-4cy39i`
is Running with **zero functions deployed** — `POST /api/place_order` returns
404. There is nothing deployed to attack. Two probes are additionally unscored
because `redeem_points` is offered to no model.

---

## 2. The five headline targets

| Metric | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Task completion on the golden set | ≥ 85% | 14.7% (shipped prompt) | **short** |
| Tool-selection accuracy | ≥ 95% | **68.8%** offline / **42.9%** live | **short** |
| Groundedness of food and policy claims | ≥ 0.95 | **71.4%** | **short** |
| Menu claims made without a citation | 0 | **not measured** | **unmeasured** |
| Photo → order, component-level F1 | ≥ 0.80 | not measured live | **unmeasured** |

Two qualifications, both of which cut against the numbers rather than for them:

- Both live runs were degraded by **429s on the shared `gpt-5-mini` deployment**
  while other agents ran evals concurrently. `scored` is 13 and 10 of 34, and
  every baseline document prints that column beside its rate. These are an upper
  bound on quality under contention, not a clean measurement.
- Uncited menu claims is unmeasured because **no deployment builds a
  `ResponseEnvelope`** (bead `cc-bap`). That is wiring, not model behaviour, and
  it means K2 cannot currently be scored at all.

**Over-refusal was measured alongside under-refusal**, as #75 insists: **4
over-refusals against 1 under-refusal.** That asymmetry is the direction a
cautious system drifts, and measuring only under-refusal would have produced a
system that hedges everything and scores well.

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
| Golden set clears its targets in an experiment, **result recorded** | **fail** | Recorded (`eval/experiments/results/shipped.json`), does not clear |
| Online evals and cost monitors **live before the URL is shared** | **fail** | Built (`chip_chat.eval.online`, 6 monitors) but not live — blocked on the AX purchase |
| Daily spend ceiling tested by **actually tripping it** | **pass, offline** | `eval/tests/test_spend_ceiling_tripped.py` — real uvicorn, real TCP, ceiling reached by talking, zero tokens asserted twice. Not tripped on the public deployment (2 M tokens; it would take the demo down) |
| A stranger completes three tasks **without narration** | **not attempted** | §4.1 makes this unwise today |
| Unaffiliated notice on entry and persistent in the header | **pass** | Verified live: banner is `position: sticky` and non-dismissible; `x-robots-tag: noindex, nofollow`; `robots.txt` `Disallow: /`; no logo, wordmark or brand colour |

---

## 4. Blockers, in the order they matter

### 4.1 The deployed app contradicts itself — **the one to fix first**

All five lanes report `not_wired` at `GET /healthz/lanes`. The opening message
reads the assigned persona; `get_points_balance` reads
`chip_chat.agent.hardcoded.ACCOUNT`. Observed live in one session:

> **Opening:** "…a regular at AL Town 1 Mall, 397 points…"
> **"How many points do I have?"** → "…home store Ballard, 1,340 points…"

Two stores, two balances, two usual orders, one conversation. Each half is
correct on its own. Together they are the thing the opening message exists to
prevent. `docs/public-demo.md` §9 argues — correctly — that the three fixes
available from the app tier are all worse than the bug, and that the real fix is
a Snowflake connection factory (`cc-lpy4`), which closes both halves at once.

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

### 4.5 The Snowflake rebuild path is untested and the trial expires 2026-09-24

The day-30 plan is *rebuild on demand*. `make snowflake-rebuild` has never been
run, because running it would destroy the synthetic population irrecoverably —
the landing zone it was generated from is not in the repository. This is a
deliberate non-action with a real deadline attached.

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
4. **Purchase Arize AX Free** and repoint. Online evals go live, which is a
   stated launch criterion. Cheap and proven.
5. **Re-run the gate-1 contended suite** once (1) lands, so the pass is earned by
   the row access policy rather than by the model declining.
6. **Re-measure the five targets** without 429 contention, on a dedicated
   deployment or serialised.
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
