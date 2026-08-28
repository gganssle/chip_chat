# The writeup

Issue [#91](https://github.com/gganssle/chip_chat/issues/91) — Phase 11. Written
27 August 2026, against revision `0000018` of the deployed app, the
`hq72718.us-east-2.aws` Snowflake trial, the `dbw-chip-chat` workspace and the
`corpus-20260827t053000z-2` release of the search index.

This document is the one the ticket says should outlive the demo: the
architecture as built rather than as planned, every decision and open question
accounted for, the numbers, and — the part the acceptance criterion actually
turns on — an honest account of what went wrong. *A writeup where everything
worked is not useful to anyone, including you.* Nothing here has been sanded
down, and where a number is bad it is printed at the same size as the good ones.

It assumes you have not read the rest of `docs/`. Where it summarises another
document it names it, because the summary is always the lossy version.

---

## 1. The shape of the thing, in one page

Cilantro is a public, unauthenticated chat agent over **real published Chipotle
menu data and entirely synthetic customer accounts**. A visitor types a name, is
assigned a loaded persona, and talks. Five capability lanes sit behind the one
conversational surface, and separating them is the whole architecture:

| Lane | The question it answers | What is behind it |
| --- | --- | --- |
| Knowledge | *"Is the barbacoa spicy? What's in a burrito bowl?"* | Hybrid RAG over Azure AI Search |
| Account | *"How many points do I have? What did I order last time?"* | Snowflake Cortex Analyst, NL→SQL |
| Action | *"Reorder my usual, but add extra guac."* | Azure Functions ops API → Snowflake stored procedures, behind a confirmation card |
| Personalization | *"What is my usual? What should I try next?"* | Databricks gold marts, computed nightly |
| Vision | *"Here's a photo of what my friend got — make me that."* | Foundry vision model → deterministic menu matcher → the action lane |

Two properties carry most of the risk and most of the value, and both survived
contact:

**Visitor identity is bound at the database session and is absent from every
tool signature.** Not validated, not injected by middleware — absent. There is
no argument for the model to get wrong and no field for an injected instruction
to populate. Enforcement lives in Snowflake row access policies keyed to a
session variable, which apply whatever SQL Cortex Analyst decides to write.

**The vision model describes ingredients and never names products.** A
deterministic matcher resolves a constrained slot vocabulary — generated from the
live catalogue at build time — onto real SKUs. Fabricating a menu item is
structurally impossible rather than statistically rare.

Everything else is in service of those two, or is plumbing.

---

## 2. The architecture as built, and where it diverges from the plan

The interesting part of this section is the difference, so here is the largest
piece of it first.

### 2.1 Five lanes were designed, built, tested — and none is wired on the deployment

`GET /healthz/lanes`, asked of the live URL on 27 August 2026:

```json
{"healthy": true, "down": [], "stale": [],
 "lanes": [{"lane": "knowledge",       "state": "not_wired"},
           {"lane": "account",         "state": "not_wired"},
           {"lane": "personalization", "state": "not_wired"},
           {"lane": "photo",           "state": "not_wired"},
           {"lane": "action",          "state": "not_wired",
            "detail": "no ops API configured; drafts are proposed and nothing is written"}]}
```

`healthy: true` beside five `not_wired` lanes is the correct answer and not a
contradiction. Nothing is broken; nothing is connected. `build_service` is called
with `NO_LANES` and `connect=None` on every deployment
(`api/src/chip_chat/api/app.py:631`), so `search_menu_knowledge`,
`get_points_balance` and `get_usual_order` answer out of
`chip_chat.agent.hardcoded` — a three-item menu and one account fixture — and
`ask_account_question`, `get_recommendations` and `match_meal_from_photo` are not
offered to the model at all, on the grounds that a hardcoded NL→SQL answer is
exactly the plausible number PRD A4 forbids.

The cost of this is not abstract. `docs/public-demo.md` §9 records it, observed
live in one session:

> **Opening message:** Hi Sam. You're a regular at AL Town 1 Mall, 397 points on
> the card, and 99% of 80 orders the same Steak Burrito with guacamole, white
> rice, black beans and cheese.
>
> **"How many points do I have?"** → You're signed in as the Ballard regular —
> home store Ballard, 1,340 points, and your usual is a chicken burrito bowl
> with a side of guac.

Two stores, two balances, two usual orders, one conversation. The opening message
is reading the assigned persona, which is correct. `get_points_balance` is
reading `hardcoded.ACCOUNT`, which is also correct *for a deployment with no
account lane* — and says so in its own result. **They are correct separately and
wrong together**, and the thing the opening message exists to prevent is exactly
what the pair of them produces.

This is one bead — `cc-lpy4`, a Snowflake connection factory — and it is the
highest-leverage change available anywhere in the tree. It closes the
contradiction, wires the account and personalization lanes, turns four `partial`
PRD requirements into measurable ones, and makes launch gate 1's contended re-run
exercise the row access policy rather than the model's reticence. It has not
landed because it needs a credential minted and put in Key Vault, which is a
human decision rather than an oversight.

**What to take from this.** The seam was *named* rather than discovered: `Lanes`
is a real type with a docstring that argues for its own emptiness, `build_service`
takes the factory as an argument, and `reads.SessionCheckout` is written to be
exactly `VisitorPool.for_session` with a test holding the two shapes together
even though nothing wires them. That is the right way to leave an unfinished
seam. It is still an unfinished seam, and a demo that is honest about being
unwired is still a demo that is unwired.

### 2.2 Where else the built system differs from RFC-001

| Planned | Built | Why |
| --- | --- | --- |
| `menu_items.base_price` | `item_prices(restaurant_id, item_id, …)` | Published prices vary by ~20% between stores — a Steak Burrito was $11.15 at one restaurant and $13.15 at another on the same afternoon. A single price column would have had to name one store's number and present it as *the* price. `docs/decisions/menu-pricing.md` |
| Seven persona archetypes | `persona_fixtures`, 28 shipped rows | An archetype cannot be assigned to a visitor. The opening message names facts about one particular customer, selected because their own history demonstrates their archetype's PRD measurement. `docs/decisions/persona-fixtures.md` |
| Four gold marts | Five — `recommendations` added | `get_recommendations` returns ranked items *with rationale*; `item_affinity` is three columns wide and carries no `demo_id`, so neither the scoping nor the sentence had anywhere to live |
| "A single Foundry agent" | A **hosted** agent, our container, run by Foundry | The 2026 Foundry release offers three shapes and only one of them can export OTel to a third party. D8, added mid-flight by #102 |
| Citations as a design intention | D9: citations are ids on a response envelope | Turning a target of *zero uncited claims* from a judgement into a rule |
| Median turn < 2 s, p95 < 4 s, every lane | Account lane re-baselined to < 5 s / < 8 s | Cortex Analyst reaches an `aws_us_east_2` account by cross-region inference. #104, `docs/decisions/snowflake-region.md` |
| Nightly publish, mechanism open | Snowflake connector, not Iceberg | 11 tables, 108,157 rows, 1.53 MiB compressed. The copy Iceberg avoids is negligible at this size and the risk lands on the wrong side of a deadline |

Two of those — D8 and D9 — are decisions the RFC did not contain when it was
written. That is worth saying plainly: **the RFC shipped with seven decisions and
the system has nine**, and both of the new ones were forced by facts about
services rather than by changes of mind. D8 in particular was raised by the
Phase 0 service inventory, which called it *"the single most consequential
finding in this document"*, and it is the clearest vindication of the plan's own
closing warning that service names and tiers move faster than the plan.

---

## 3. The decisions, revisited

The ticket asks whether each held and what would be done differently. Ordered as
RFC-001 §12 orders them.

### D1 — Single agent with tools, not a multi-agent system

**Held, and for a reason that changed.** The original rationale was a two-second
turn budget that does not accommodate extra model round trips. That budget is
gone — the measured median turn is 34.2 s — so the latency argument for D1 no
longer applies to the system that exists. The decision survives anyway on the two
other grounds, which turned out to be the load-bearing ones: a single prompt with
a single trace is dramatically easier to debug, and the golden set shows the
routing problem is *not* a decomposition problem. Tool-selection failures are
`no_tool` (10 of them), not `wrong_lane` (0) — the model does not confuse the
lanes, it declines to call anything. A router would not have fixed that.

**What I would do differently:** nothing structural, but the revisit condition
should be tightened. The RFC says revisit "when the write path becomes a separate
agent holding the only credentials that can call the ops API." The red team found
a sharper reason to want that boundary: the ops host authenticates a visitor by
reading `demo_id` off a request header
(`api/functions/function_app.py:431`) with no session lookup and no signature —
a bearer identity wearing D4's clothes. A separate write agent is one answer;
signing the header is a cheaper one, and neither is built.

### D2 — Snowflake serves, Databricks computes

**Held as an architecture and falsified as an economic claim.** §8 of this
document is the full argument, because the cost data has something to say about it
that deserves more than a line.

### D3 — Describe then match, rather than direct SKU prediction

**Held completely, and it is the cleanest decision in the project.** The vision
model returns a fixed slot schema whose enums are generated from the live
catalogue at build time; `notes` is the only free-text field and nothing
downstream parses it. A model cannot name a product that does not exist because
it is never asked to name a product.

The honest caveat is that **nothing has been measured**. `eval/photos/BASELINE.md`
reports 0 frames labeled against a need of 30, so PRD §05's *component-level
F1 ≥ 0.80* is `unverified — not unmet`, and the confidence floors in
`chip_chat.vision.matcher._DEFAULT_FLOORS` ship untuned, with their own docstring
saying so. The decision is sound and the implementation is untested against a
photograph. That combination is more common than it should be in this repository
and it is a sequencing failure, not a design one — see §10.

### D4 — Identity bound at the database session, absent from tool signatures

**Held, and it is the decision the whole system is worth building for.** The
mechanism is real: unbound as `CHIP_CHAT_READ`, all nine visitor-scoped tables
return zero rows while `INFORMATION_SCHEMA` reports 18,898 rows in `ORDERS` at the
same moment — default deny holding, not an empty table.

The RFC named the way it breaks — connection pooling — and the project engineered
against it deliberately. `api/tests/test_pool_concurrency.py` runs 32 visitors
through 4 slots over 1,280 checkouts, and ships a `NaivePool` control that passes
sequentially and discloses concurrently, which is the only way to know the test
could catch anything. That control is the single best piece of test design in the
tree.

**What I would do differently:** the guarantee has one hole nobody has closed and
it is a hole in the *measurement*, not the mechanism. The adversarial suite
detects a disclosure by planting a canary token. A model that says *"the other
person here ordered a burrito bowl"* has disclosed something real and the suite
will not see it. `eval/adversarial/BASELINE.md` says so in its own words: **a
clean first gate is evidence, never proof.** A second detector — a judge over the
reply asking whether it contains any fact about a visitor who is not this one — is
the missing half, and it is blocked on the same absent judge as everything else
(§5).

### D5 — Public with no authentication

**Held.** All data is synthetic, so the exposure is cost rather than disclosure,
and cost is bounded in the application. The spend ceiling was tested by actually
tripping it — `eval/tests/test_spend_ceiling_tripped.py` runs a real uvicorn over
real TCP, talks until the app stops, and asserts zero model tokens twice.

**What I would do differently:** the RFC says the fallback is invite codes if
abuse exceeds rate limiting. The real finding is smaller and more annoying. There
is **one working kill switch, not three**: the deployed `EnvironmentKillSwitch`
sits behind a five-second cache and flipping it is an app-setting change, which
creates a new Container Apps revision and takes about 40 seconds; the file-based
switch has no file share mounted. And the daily ceiling has never been tripped on
the public deployment, because tripping it means taking the demo down for the day
at 2,000,000 tokens. A kill switch you have not pulled is a kill switch you have
not tested, and the honest thing is to schedule a maintenance window and pull it.

### D6 — OpenInference over OTel, dual export, Phoenix then Arize AX

**Held, narrowly, and it is the decision that had to be reworded before it could
be proved.** §7.

### D7 — Real menu data, synthetic accounts

**Held, and it paid for itself twice.** It is what makes the knowledge lane worth
building and the action surface believable — `docs/action-surface.md` derives the
four write tools' arguments from Chipotle's published ordering flow and rewards
terms and marks every claim it could not source as invented, which is how
`cancel_order` came to be documented as an affordance the real product does not
offer customers at all. The receipt has to say so.

The boundary is kept in the schema rather than in a convention, and the pipeline
never blurs it. The one place it strains is the one the RFC did not anticipate:
`persona_fixtures` selects a real *synthetic* customer whose history demonstrates
an archetype, and the Lapsed Customer's threshold is read off the published
rewards terms — the price of the costliest published reward — rather than written
as a number, so a real-world fact moves a synthetic fixture. That is the boundary
working, not leaking, but it took a decision record to get right.

### D8 — Hosted agent on a basic-setup project, not a prompt agent

**Held, and it was necessary.** A prompt agent's tracing goes to Application
Insights and stays there. That does not weaken D6, it removes exactly the spans
the second backend exists to consume: `agent.step`, `llm.completion` and the
per-tool spans are the ones trajectory and tool-selection evals score. Choosing
Basic setup keeps threads in Microsoft-managed storage and avoids Blob + AI
Search + Cosmos DB at roughly $97/month against a $150 budget, buying data
residency over synthetic personas — a property this system cannot use.

**What I would do differently:** notice it in Phase 0 rather than in Phase 7.
The service inventory did notice it, which is the system working. But D8 arrived
after the RFC was written and after #78's acceptance criterion had already been
set against a claim that turned out to be unmeetable, and the fix was to reword
the ticket. That is a fine outcome; it is a better outcome if the verification
pass that produces it runs before the tickets are written.

### D9 — Citations are ids on the response envelope, rendered inline

**Correct and unbuilt, which is the worst of the four available states.**
`chip_chat.agent.envelope` exists, is tested, and **is imported by no caller**.
Nothing in `chip_chat.agent.loop` or `chip_chat.api.app` builds one, so a
citation id has nowhere to travel and `ChatReply` carries none. The consequence
is measured: PRD §05's *menu claims made without a citation = 0* is not failed,
it is **unmeasurable** — `eval/grounding/BASELINE.md` reports `cited` and
`minted` as 34 rows unscored, and the reason it gives is the wiring rather than
the model. Bead `cc-bap`.

The reasoning behind D9 is nonetheless the best reasoning in the RFC and I would
make the same call again. A source note the model types is a string a language
model produced; checking it means asking a second model whether the first one told
the truth. Ids that must match the `retriever.search` span reduce a groundedness
gate to a comparison. **What I would do differently is build the envelope before
building anything that would be scored by it** — the eval was written against a
payload the deployment does not produce, and an eval that cannot fail is not a
measurement.

---

## 4. Every open question, and how it was resolved

### PRD Q1 — Does a visitor's state persist between visits?

**Yes, via a signed cookie mapping to a durable `demo_visitors` row.** Issue #9.
The tension this question predicted — persisted sessions against a nightly reset —
is real and was resolved by *ageing sessions out on last-seen* rather than
truncating visitor-scoped tables. `docs/demo-reset.md` is the mechanism, and it
turned out to be far more intricate than "delete old rows": `last_active` is the
**greatest** of the visitor's own clock and every live row's timestamp, because a
visitor whose `last_seen` says last week and whose receipt says one minute ago is
a visitor who is still here.

### PRD Q2 — Can visitors edit their persona, or only switch fixtures?

**Both, and editing is exactly three fields:** display name, home store, stated
preferences. The mechanism is the answer: those three columns live on
`demo_visitors`, and no Databricks job reads that table — marts are computed from
`orders`, `order_items` and `loyalty_ledger` only. So a visitor cannot construct a
state the gold marts were computed against, *structurally* rather than by policy,
and a reviewer checks the property by confirming nothing under the medallion
pipeline selects from `demo_visitors`. Issue #59.

The honest footnote is in the launch-readiness table: E7 is marked *deferred with
a note*, and the note is worth reading, because the closed three-field schema and
the ops-host route both exist while **there is no HTTP route on the web app**. It
is library code, not a capability. A visitor cannot reach it.

### PRD Q3 — Do citations show inline or on demand?

**Inline presence, on-demand detail**, with allergen answers citing adjacently and
the harvest date visible without interaction. Issue #57, D9. The presentation
argument follows from the metric: on-demand citations would leave the headline
groundedness number scoring a field the visitor never opens, which measures a data
structure and reports it as trust. Unbuilt — see D9 above.

### PRD Q4 — Does V0 handle several meals in one photograph?

**It detects the case, says how many meals it saw, asks which one, and builds
none of them.** The stage-4 schema returns one slot set plus a `meals_visible`
count, so on a table of four bowls those slots describe the *photograph* rather
than any one meal; resolving them would produce a draft composed entirely of real
catalogue items that nobody in the picture is eating. Issue #58. Full multi-order
support is most of group ordering, which was already V1.

### RFC Q1 — Iceberg tables or the Snowflake connector for the nightly publish?

**The connector.** `docs/decisions/nightly-publish-mechanism.md`. Three things
settle it: the copy Iceberg avoids is 1.53 MiB once a night, the extra setup risk
lands on the scarce resource in an evenings-and-weekends build, and swapping the
mechanism later changes one job rather than the architecture. The decision record
states what is given up rather than dressing it up: **this project exists partly
to exercise these platforms, so declining to exercise one of them is a real loss
rather than a nominal one.**

### RFC Q2 — Does Foundry's thread storage suffice for conversation state?

**Mostly settled by D8, and the empirical half was measured in Phase 0.** The
split is: the Foundry thread owns message history; the app owns everything with a
security or cost role — order drafts, confirmation state, budget counters, persona
assignment, and `thread_id` itself, which is a column on `demo_visitors` because
PRD Q1 made visitor state outlive the visit. Phase 0 shipped a retention probe
rather than a claim (`docs/phase-0-verification.md`), which is the right shape for
a question whose answer is a vendor's operational behaviour.

### RFC Q3 — Is the semantic reranker available on the AI Search free tier at usable quota?

**Yes, at 1,000 semantic requests per month**, which is enough for the demo and
tight enough that a retrieval sweep is a budgeted event (`make retrieval-baseline`
spends 40 of them). The free tier was kept and Basic — $73.73/month — declined.

**And this is the answer that turned out to be wrong in a way nobody asked about.**
Q3 asked about the *reranker*. Nobody asked whether free-tier **vector search**
was reliable, and it is not: it returns `{"value": []}` with HTTP 200 under load.
§9.1 is the full account. The reranked arm production sends is unaffected, so the
answer to the question as asked still holds — but the question was too narrow, and
a Phase 0 verification that had probed the tier rather than the feature would have
found it five weeks earlier.

### RFC Q4 — How is the nightly demo reset scoped if visitor state persists?

**By ageing sessions out on `last_seen`, restoring from a per-visitor baseline
table, and — the part that was genuinely open — deciding that a visitor's live
rows survive until that visitor ages out.** The nightly *publish*, not the reset,
is what has to stop erasing live rows; that is bead `cc-fxf4` and it is not done.
The guess recorded in `docs/nightly-publish.md` — *"it is a demo sandbox and the
answer is probably yes, erase them"* — was the other way, and was wrong, because a
cookie means Sam comes back tomorrow to the order they placed today and a publish
that erases it overnight makes the persistence decision true only within a
calendar day.

---

## 5. The numbers

Every figure below was measured. Where something was not measured this section
says *not measured* rather than estimating and presenting the estimate as a
measurement, which matters more than usual because two of these lines are
pass/fail gates and a gate that was not measured has not passed.

### 5.1 The five headline targets

| Metric | Target | Measured | Verdict |
| --- | --- | --- | --- |
| Task completion on the golden set | ≥ 85% | **14.7%** (shipped prompt, live) | short by 70.3 points |
| Tool-selection accuracy | ≥ 95% | **68.8%** offline / **42.9%** live | short by 26.2 / 52.1 points |
| Groundedness of food and policy claims | ≥ 0.95 | **71.4%** | short by 23.6 points |
| Menu claims made without a citation | 0 | **not measured** | unmeasurable — `cc-bap` |
| Photo → order, component-level F1 | ≥ 0.80 | **not measured** | 0 of 30 frames labeled |

Two qualifications, both of which cut against the numbers rather than for them.
Both live runs were degraded by 429s on the shared `gpt-5-mini` deployment while
other work ran evals concurrently — `scored` is 13 and 10 of 34, and every
baseline document prints that column beside its rate, so these are an upper bound
on quality under contention rather than a clean measurement. And the golden set's
own *ceiling* run, with routing handed to the deployment by
`chip_chat.eval.golden.testing.RoutingOracle`, scores 21% task completion and 69%
tool selection — that is the plumbing at its best with the model told the answer,
so **most of the gap is missing wiring rather than model quality**.

### 5.2 What the tool-selection gap is made of, which matters more than its size

`eval/trajectory/BASELINE.md`, 34 rows, 32 scored:

| Failure shape | Count | What it means |
| --- | ---: | --- |
| `wrong_lane` | **0** | chose, and chose the other thing |
| `no_tool` | **10** | answered from what the model already knew |
| `extra_tools` | 0 | reached the lane and paid for more calls than the turn needed |
| `wrong_query` | 0 | right lane, wrong ask |

Zero `wrong_lane` is the finding. The five-lane architecture exists to make lane
selection the single question the system turns on, and the model does not confuse
the lanes — it fails to call anything, and a span tree cannot tell *the model
chose not to call* apart from *the tool was never registered*. The week-one slice
registers six of eleven tools. By lane: knowledge 100%, account 16.7%,
personalization 66.7%, action 57.1%, vision 0% (one row).

### 5.3 Groundedness, and the refusal in both directions

`eval/grounding/BASELINE.md`, judged by `gpt-5-mini`:

| | |
| --- | ---: |
| Groundedness | **71.4%** over 7 of 10 rows it was asked on (target ≥ 95%) |
| Claims with nothing retrieved | 4 — the floor under groundedness |
| **Over-refusals** | **4** |
| **Under-refusals** | **1** |
| Uncited menu claims | unmeasured, 34 rows unscored |

**Measuring over-refusal beside under-refusal is the most valuable thing #75
insisted on**, and it is the number I would most want a reader to take away.
Measuring only under-refusal produces a system that hedges everything and scores
beautifully. Four to one is the direction a cautious system drifts, and three of
the four over-refusals — *"how do points work"*, *"what happens if my order is
wrong when i pick it up"*, *"can i cancel an order after i've placed it"* — are
turns where the retriever returned **zero passages**, so the model declined
because it had nothing, which is the right behaviour attached to the wrong cause.
The fourth, *"which has fewer calories, the chicken bowl or the steak burrito"*,
declined with two passages in hand, and that one is a real over-refusal.

The single under-refusal is in the safety category, which is the worse of the two
places to put it: *"is the chicken halal"* was answered from two retrieved
passages, and the published data does not support an answer. Nothing about halal
certification is published. That is the exact shape `docs/decisions/allergen-boundary.md`
is written against — *nothing is derived, no advice, and what the record does not
say gets said plainly* — arriving on a dietary question outside the four allergens
that record names, asked in good faith by somebody for whom a plausible guess is
worse than no answer.

### 5.4 Retrieval

The demo bar, from #50 — *top-3 recall on your allergen questions, measured, with
numbers* — is the number that survived everything:

| Arm | allergen recall@3 |
| --- | ---: |
| keyword only | 100% |
| vector only | 83% |
| hybrid | 100% |
| **hybrid + reranker** | **100%, on all three sweeps** |

All-category `recall@3` across three live sweeps of the same 40 questions:

| Arm | sweep 1 | sweep 2 | sweep 3 |
| --- | ---: | ---: | ---: |
| keyword only | 84% | 84% | 84% |
| hybrid + reranker | 95% | 95% | 91% |
| hybrid | 53% | 84% | 84% |
| vector only | 41% | 7% | 83% |

The first two rows are a measurement. The second two are a service defect, and
§9.1 is what that cost.

The result that is *not* good and is stable across all three sweeps: **restraint
on the eight questions the corpus cannot answer measured 12% under the arm
production sends.** Seven of eight came back grounded, including *"which items
are safe for a peanut allergy"* against four published marks that do not include
peanut. `PROVISIONAL_RERANKER_FLOOR = 1.5` is too low and now has the measurement
its docstring was waiting for (`cc-sans`).

### 5.5 Latency

| | Measured | Target |
| --- | ---: | --- |
| Median turn | **34.2 s** | < 2 s (< 5 s, account lane, after #104) |
| p95 turn | **62.7 s** | < 4 s (< 8 s, account lane) |
| `llm.completion`, median | 20.2 s | — |
| Cortex Analyst service time | 2.97 s median / 3.93 s p95 | the reason #104 exists |
| SQL the Analyst writes, executed | 225 ms median | — |

Over 69 `chat.turn` spans in Application Insights, under 429 contention. **The
turn is the model call.** #104's re-baseline of the account lane to < 5 s / < 8 s
was an honest concession made in Phase 4 rather than discovered in Phase 9, and it
does not begin to close a 34-second gap. This target is not close to met and no
amount of re-baselining would make it met; what would is a dedicated deployment
and a serialised eval, and neither has been done.

### 5.6 Cost, all in

| | |
| --- | ---: |
| Median conversation — 8 turns, 29,268 tokens | **$0.0156** (target < $0.05) ✅ |
| p95 conversation | $0.0301 ✅ |
| **One account question** | **$0.20** — 4× the whole-conversation budget ❌ |
| Fixed infrastructure, monthly | **$41.50** — NAT gateway $32.85 + its public IP $3.65 + ACR $5.00 |
| All-in at 200 conversations/month | **$0.224 per conversation**, 4.5× the target |
| Whole Azure bill, month to date | $6.02 — of which **Foundry Models is 1.6%** |
| Whole Snowflake account | 10.27 credits ≈ **$30.82** — 5× the Azure bill |

Cortex Analyst bills **67 credits per 1,000 messages**, confirmed twice on two
different hours of the same day and agreeing to five decimal places, which is
what makes it a rate rather than an average. The SQL it writes runs for 0.0000625
credits, so **the question costs three thousand times what the answer costs**.
And no resource monitor bounds it: monitors count virtual warehouse credits and
Cortex Analyst is serverless, so 42% of everything this account has spent sits
outside every guardrail the account has. The only thing bounding it is that
nothing calls it in a loop, and that is a fact about the current code rather than
a control.

### 5.7 The gates

**Gate 1 — zero cross-visitor data disclosures.** Contended live run, 8 visitors
against a pool of 2, 6 rounds, 285.01 s: **48 attempts, 48 held, 0 breaches**, and
the harness's own self-check says *could this run have caught a bleed: yes*. The
asterisk is §9.7.

**Gate 2 — zero account writes without explicit confirmation.** Held on every
shape that can be put, including with a deliberately sabotaged system prompt
(`agent/tests/test_sabotage.py`) and against direct API calls that bypass the UI
(36 tests in `api/tests/test_ops_routes.py`). Not measured against the deployment,
for a structural reason: `func-chip-chat-ops-4cy39i` is Running with **zero
functions deployed**, so `POST /api/place_order` returns 404 and there is nothing
deployed to attack.

---

## 6. The seven traps, honestly assessed

`docs/system-design.md` closes with *"Seven ways this goes wrong"*, written before
any code existed. Predicting a trap and avoiding it are different achievements and
this section separates them.

### Trap 1 — Thin synthetic data

**Avoided, and it is the one the plan was most right about.** 500 customers, 18
months of orders composed only of real catalogue items, seven archetypes, and
`docs/synthetic-population-texture.md` is nineteen measured checks on whether the
population is actually thin — generated rather than written, which is the only way
that document is worth anything. The texture is real enough that
`persona_fixtures` can pick a customer whose own history demonstrates an archetype
and write the opening sentence from it, and that P2 can be verified as *0 of 138
visitors were recommended something they had already ordered*.

### Trap 2 — Letting the model pick the visitor

**Avoided, deliberately and structurally, and it is the best work in the
project.** No tool signature accepts a visitor identifier; the absence is the
enforcement mechanism. The RFC named the specific way this breaks —
session variables plus pooled connections — and the project engineered against it
rather than testing around it: set on checkout, cleared on return, 1,280 checkouts
through 4 slots from 32 visitors, with a `NaivePool` control that passes
sequentially and discloses concurrently.

Two residuals, both written down. The ops host reads identity off a request header
with no signature, which is a bearer identity standing where a session binding
should be. And the canary detector cannot see a disclosure that carries no
identifier.

### Trap 3 — Letting the vision model name menu items

**Avoided by construction** (D3), and untested against a photograph. See D3.

### Trap 4 — Text-to-SQL over a raw schema

**Avoided.** `docs/snowflake-semantic-view.md` curates five of the serving layer's
tables and two of `menu_items`' nine columns into the semantic view, and argues
for each of the nine tables left out. A4 — *say so rather than return a plausible
number* — measured 10/10 refused, and the lane's refusal to fall back to
hand-written SQL is asserted in code in three places rather than left to
behaviour.

The measured cost of doing this properly is §5.6's $0.20, which is not a failure
of the trap avoidance — it is the price of the product that avoids it.

### Trap 5 — Hand-waving allergens

**This is the one that deserves real scrutiny, because the answer is: avoided at
the layer the plan named, and walked into at the layer above it.**

At the data and chunking layers the work is exemplary.
`docs/decisions/allergen-absence.md` makes an absent allergen mark a *value* and
never a negative, so there is no encoding in which a missing row reads as
reassurance. `docs/corpus-chunking.md` §3 refuses fixed-window chunking with a
worked example of what a window does to a nutrition table — a chunk containing
**260**, which is Cheese's sodium, sitting immediately before the word Guacamole
under no heading at all, *"every ingredient of a confident wrong answer present,
and nothing downstream able to detect it"* — and then keeps a real fixed-window
chunker **in the test suite** so that
`test_fixed_window_chunking_splits_a_published_nutrition_row` fails across
nineteen window sizes and two overlaps. Keeping the bad chunker in the test file
rather than in the module, on the grounds that a module shipping one "for hard
documents" would eventually have it used on the hard documents, is the kind of
judgement this trap is asking for.

And then: **the allergen caveats are chunks of their own rather than a footer on
every menu chunk**, which is the arguable call, made deliberately, with its cost
stated at the time — *"an agent which fails to retrieve them answers without
them, and that failure is visible to an evaluation in a way a diluted embedding
would not be."*

The evaluation duly saw it. Restraint under the reranked arm is **12%**. Seven of
eight unanswerable questions came back grounded. *"Which items are safe for a
peanut allergy"* was answered against four published marks that do not include
peanut. The single under-refusal in the whole grounding run is *"is the chicken
halal"*. And `eval/dietary/BASELINE.md` — the red team written specifically for
this subject, 13 probes covering all seven of #84's attack shapes — reports **13
of 13 unsettled**, because the week-one slice serves no published allergen record
and no published caveats, so most of the set could not be asked at all.

So the honest verdict is this. The trap was predicted, the decision was made
correctly and written down at length, the data model was built to support it, the
chunker was built to support it and has a control test proving the control test
works — and **the property the whole edifice exists to produce has never been
measured on anything that could produce it.** The gate is not failed. It is
unmeasured, and a gate nobody measured has not passed. That is walking into the
trap with your eyes open, which is a different mistake from walking into it
blind and is not obviously a smaller one.

### Trap 6 — Evaluating last

**Avoided in form, walked into in substance.** The golden set was written in
Phase 2 as the plan says, is versioned (`cilantro-golden-set 9ba196eb786c`),
covers 19 PRD requirements directly and delegates 12 with **0 uncovered**, and
`chip_chat.eval.golden` ran from the week-one slice onward. `make ci` runs a
structural adversarial pass on every PR for free.

But the substance of "evaluate from the first ugly slice" is that the numbers
change your choices, and these numbers mostly could not. Task completion of 14.7%
is dominated by tools the slice does not register. Groundedness of 71.4% is
computed over 7 rows because the rest returned nothing. Every citation check is
unscored because no envelope is built. Every judged check is unscored because
**there is no judge anywhere in the tree** — `chip_chat.eval.golden.run.Judge` is
a seam and #72 is where a model lands behind it. `eval/photos` has no photographs.
An eval harness that reports `unscored` honestly instead of `failed` is much
better than one that lies, and this one is scrupulous about it. It is still an
eval harness that has mostly not evaluated the product.

### Trap 7 — Instrumenting last

**Avoided, cleanly, and it is the trap whose avoidance paid off most visibly.**
The span schema is fixed in RFC-001 §09 as a schema rather than a debugging
convenience, `chip_chat.tokens.*` rides on the spans, and the consequence is that
the entire token half of the cost document is one attribute lookup rather than a
tree walk. §7 is the other half of the payoff.

The one thing to note is that instrumenting early is what let the cost document
discover that **the meter and the spans disagree by a factor of about two on the
same day's traffic**, entirely because Azure Cost Management is 8–24 hours behind.
That disagreement is the strongest argument the project has for putting token
counts on the spans in the first place, and it is only visible because both were
looked at.

### The eighth trap, which the plan put in a section of its own

*"Cap the spend in code"* — budget alerts notify, they do not stop anything.
**Avoided in the request path and comprehensively falsified as a model of where
the money goes.**

The cap is real, inline, synchronous, in front of every model call, and was
tested by actually tripping it over real TCP. It works. And:

- **Foundry Models is 1.6% of the Azure bill.** Every guardrail in the repository
  that worries about model spend — the inline cap, capacity 10 on both
  deployments, the five-step loop ceiling, `max_completion_tokens = 2000` — is
  guarding nine cents.
- **A NAT gateway in the Databricks managed resource group is 24.6% of it**, and
  costs **44 times more to exist than to carry everything the project has ever
  sent through it**.
- **Snowflake is five times the entire Azure bill**, and 42% of that is one
  serverless product invoked sixty-four times, which no resource monitor can see.
- The warehouse that has burned 22% of the account's credits is `COMPUTE_WH` — the
  one Snowflake created at signup, which `snowflake/sql/` does not manage because
  it did not create it, which auto-suspends after 600 seconds rather than 60, and
  which is still the default in the `snow` connection. Every ad-hoc `snow sql`
  without a `USE WAREHOUSE` wakes the most expensive idle warehouse in the
  account for ten minutes.

None of that is an argument for removing the token cap: an unauthenticated public
endpoint is a different risk profile and the cap is there for the tail, not the
mean. It is an argument that **the guardrail was pointed at the mean, and the
mean was never the problem.** A cost review that only looked at tokens would have
found nothing at all.

---

## 7. The observability switch, and the size of its diff

The claim in the system design was that changing observability vendors is a
configuration change, *"which is worth demonstrating deliberately."* Issue #78
existed to demonstrate it. The honest account has three parts and the middle one
is the interesting one.

**The claim as originally written could not be met, and the ticket was reworded
rather than the result fudged.** Foundry hosted agents take their exporter
configuration from environment variables that are **immutable per agent version**,
so "repoint the exporter" is not editing a setting — it is cutting a new agent
version. #102 found this while deciding D8 and its fourth acceptance criterion is
literally *"#78's acceptance criterion reworded for agent-version immutability."*
That is the right order of operations: discover the constraint, restate the claim
to something true, then prove the true one. Restating it produced the sentence
worth remembering — **no instrumentation code changes; the FastAPI tier is an
environment variable and a restart, and the agent is a new agent version.**

**The instrumentation diff is empty, and it is empty as a property rather than as
a coincidence.**

```console
$ git diff --stat -- otel/ agent/src/chip_chat/agent/
(no output)
```

`otel/tests/test_export_configuration.py::test_the_exporter_code_names_no_vendor`
parses `exporters.py`, `config.py` and `tracing.py` with `ast`, collects every
identifier and every runtime string literal, and fails if `phoenix` or `arize`
appears in any of them — docstrings excluded on purpose, because prose explaining
why the vendor is absent from the code is not the vendor being present in it.
`build_span_exporters` has no `if backend == …` in it; it appends an OTLP
exporter when `config.otlp_endpoint` is non-empty and an Azure Monitor exporter
when the connection string is set, and moving from Phoenix to AX changes the value
of one of those. **That is the sentence D6 was bought for, and it survived
contact.**

**The agent-side diff is exactly two lines**, produced by rendering the version
manifest twice with everything else held constant:

```diff
       "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
-      "value": "http://phoenix.internal:6006"
+      "value": "https://otlp.arize.com/v1"
       "name": "OTEL_EXPORTER_OTLP_HEADERS",
-      "value": "${{connections.otel-secrets.credentials.otlp_headers}}"
+      "value": "${{connections.arize-ax.credentials.otlp_headers}}"
```

Not two files and not two functions — two values in a JSON document that is
itself generated from the environment.

**And two things are recorded rather than quietly patched, which is the point of
asking for the honest answer instead of the tidy one.** `OTEL_EXPORTER_OTLP_PROTOCOL`
stays `http/protobuf` because AX speaks it; it is hardcoded in
`chip_chat.agent.version` with no override, so **if AX had wanted gRPC this section
would have said "and one code change, which is a finding."** That is a near miss,
not a success. And there is no Terraform path for `OTEL_EXPORTER_OTLP_HEADERS` on
the FastAPI tier, which AX needs for `api_key` and `space_id` — about six lines of
`infra/` work, a Container Apps secret backed by a Key Vault entry `.env.example`
already points at. The instrumentation reads the header the moment something sets
it; the *delivery* of one of the two values is unfinished.

**What is not proved.** That AX receives complete span trees identical in shape to
Phoenix's, because AX has not been purchased —
`ArizeAi.ObservabilityEval` is `NotRegistered`, `proj-chip-chat` has zero
connections, and acquiring it is a transaction this work was not authorised to
make. So the claim is proved *narrowly*: the code does not change, the
configuration is two values per tier, and the shape of the change is known
exactly. It is not proved *end to end*. Both halves of that sentence belong in the
answer.

The tier decision is also worth recording because it is arithmetic rather than
preference: AX Free is $0 at 25,000 spans/month, and one busy day of Phase 8 and
Phase 10 verification emitted 641 spans — about 2.5% of a month's allowance, so
roughly 39 such days fit. AX Pro at $50/month would move steady state to $80–110
against a budget whose 50% alert sits at $75, which is the same as switching the
alert off.

---

## 8. Why Snowflake serves and Databricks computes

D2 is the decision the ticket says is *"worth more in your first month at Chipotle
than any single line of the code,"* so here it is written out properly, and then
tested against the cost data, which has something uncomfortable to say about it.

### 8.1 The argument

The split is **on the clock**, not on the data and not on the vendor.

Every conversational turn needs a store that can answer in milliseconds, under a
governance model that survives a language model writing the SQL. That is a narrow
and demanding set of requirements. It needs row-level enforcement that applies to
*any* query, because Cortex Analyst generates the query and no amount of prompt
discipline constrains what it generates — Snowflake row access policies keyed to a
session variable do exactly that, apply to every role with no owner exemption, and
are the reason `CHIP_CHAT_READ` reads zero rows from nine tables while
`INFORMATION_SCHEMA` reports 18,898. It needs a semantic layer a text-to-SQL
system can be pointed at, which is a curated artefact rather than a schema dump.
And it needs to cost nothing while idle, which an X-Small warehouse suspending
after sixty seconds does.

Nothing in that paragraph is batch work, and none of it is what a lakehouse is
good at.

The other clock is nightly, and everything on it is the opposite shape. Cleaning
and deduplicating a harvested web corpus. Chunking it so that a nutrition row
never splits. Conforming eighteen months of synthetic orders through
bronze → silver → gold. Computing `customer_360`, `usual_order`, `item_affinity`,
`spend_summary` and `recommendations` over the whole population. Training an
item-affinity recommender, tracking it in MLflow, registering it in Unity Catalog
behind a `@champion` alias that a run only takes by beating a popularity baseline
on *novel* hits. That is hours of compute over the full history, it is
version-controlled and lineage-tracked, and it must never happen while a visitor
is waiting.

So: **Databricks does the expensive overnight thinking and publishes the answers;
Snowflake serves the answers under a policy.** Eleven tables, 108,157 rows,
1.53 MiB compressed, once a night, alias-swapped atomically per table so a killed
publish leaves a consistent previous generation.

The property that makes this an architecture rather than a preference is that
**the split shows up in the conversation**. *"What is my usual?"* is a question
about a habit, and a habit is a statistical fact about eighteen months of
behaviour with a confidence attached to it. Computing it per turn would be slow
and would give a different answer depending on when you asked. Reading it from a
mart makes it fast, deterministic, explainable — `usual_order.confidence` is
calibrated and documented so a low value hedges honestly — and *stale in a way
the system can state*, which is why RFC-001 §10 says a failed Databricks job
serves stale marts **with their `derived_at` timestamp** rather than silently
serving stale data as fresh.

The inverse is equally load-bearing. *"How much did I spend this year?"* is not a
precomputable question, because the space of time ranges and dimensions a visitor
might ask about is unbounded. That one has to be a query, against a governed
store, at conversation latency. A mart cannot answer it and a lakehouse cannot
answer it fast enough under a row policy.

**Being able to say that sentence in an interview is the deliverable.** One of
these systems is a database with governance; the other is a compute engine with
lineage. Putting the per-turn read on the lakehouse means either giving up row
policies or accepting seconds of latency; putting the nightly medallion on the
warehouse means paying warehouse rates for ETL and giving up MLflow, Unity Catalog
lineage and the model registry. The split is not a compromise between two vendors
who both wanted in — it is the shape the workload already had.

### 8.2 What the cost data does to the argument

And now the uncomfortable part, which the ticket is right to insist on.

**Snowflake is five times the Azure bill.** $30.82 against $6.02, on an account
three days old. **The models are 1.6% of the Azure bill** — nine cents. **42% of
the Snowflake spend is Cortex Analyst**, from sixty-four requests, invisible to
every resource monitor the account has because monitors count virtual warehouse
credits and Analyst is serverless. And **89% of the Azure bill is the
lakehouse** — DBUs, the VMs they run on, and a NAT gateway created with the
workspace that bills $32.85/month whether or not a cluster ever starts.

So the honest reading of D2 against the meter is: **the serving half is the
expensive half, and the computing half is the second-expensive half, and the
thing everybody instinctively guards — the model — is a rounding error.** The
division of labour is correct on latency, correct on governance, correct on
lineage, and it buys none of the cost savings that "keep expensive computation off
the conversational path" implies to a reader who has not seen the bill.

Three things follow, and none of them is *undo D2*.

**The split is a latency and governance decision, and it should be argued as
one.** Anybody who defends it on cost grounds will lose the argument the first
time somebody opens Cost Management. The nightly publish costs about $7 a month;
the lane it feeds is not the expensive one. The expensive one is the lane that
does inference at query time.

**One lane's economics are structurally different from the other four's and the
single blended target was written before anybody knew that.** PRD §05 says
*cost per conversation < $0.05*. Four of the five lanes are essentially free
relative to the model call that drives them; one is $0.20 per question, four times
the budget for the entire conversation it appears in. The recommendation in
`docs/cost.md` is the same shape as #104's latency concession: name a different
target for the lane that pays, rather than weakening it everywhere or pretending
the lane can be made cheap. `get_points_balance` is already the cheap
deterministic path for the commonest question, which is why it is a separate tool
with no Analyst call behind it, and widening that set is both the cheapest fix and
a correctness improvement. What must **not** happen is making the model reluctant
to call the tool, which is a prompt instruction standing in for a control — the
thing this repository says not to do about writes and should not start doing about
spend.

**At demo volume the fixed cost is the cost.** $41.50/month of standing
infrastructure divided by 200 conversations is $0.208 each, against $0.016 of
marginal cost. Both belong in an honest answer and conflating them in either
direction makes both meaningless. The estimated steady state is ~$92/month against
a $150 budget whose 50% alert is at $75 — the estimate crosses the alert, and the
two things pushing it over are the account lane and a NAT gateway, neither of
which was in the original arithmetic. The budget alert is correctly calibrated to
be surprising, and it would be surprised.

---

## 9. Seven failures worth dwelling on

This is the heart of the document. Each of these is a thing that went wrong, and
each was found by a different mechanism — which turns out to be the pattern
(§10).

### 9.1 A committed baseline contained a false conclusion, and it read as a confirmation

**What happened.** Azure AI Search on the Free tier returns

```json
{"@odata.context": "…/indexes('corpus')/$metadata#docs(*)", "value": []}
```

to a vector query, with **HTTP 200**, no `error` key, no warning, and an
`elapsed-time` header in the ordinary 115–350 ms range. Not an error, not a
timeout, not a partial result — an empty result set indistinguishable from a
corpus with nothing in it. Measured: about **25% empty minutes after a build,
85% after a few hundred queries, 90% by the end of a sixty-query run**, and it
does not recover in minutes.

The first retrieval sweep therefore scored the vector-only arm at **41% recall@3
overall, 40% on the first category in the question file and 0% on all four
categories after it** — which is not a fact about embeddings, it is the order the
questions are in. And that number was written into a committed baseline and read
as **confirmation of RFC-001 §08's hybrid argument**: *item names are proper
nouns that embeddings handle poorly, and look, vector-only scores zero.*

It was a service fault presented as a finding, in a file whose whole purpose is
to be trusted later.

**What would have caught it sooner.** Three things, in increasing order of how
much I wish they had been in place.

*Repeating the sweep.* The defect was found by re-running the same forty
questions against equivalent corpora and getting 41%, 7% and 83% on three
consecutive sweeps. **A number that is not reproducible is not evidence,
whichever way it points** — and a baseline that is written once is a baseline
nobody has checked. The ablation is repeatable by design (that is #50's fourth
acceptance criterion and it holds); nothing required it to be *repeated* before a
conclusion was drawn from it.

*Suspicion of a result that agrees with you.* The hybrid argument was in the RFC
before the measurement existed. A vector arm scoring 0% is a spectacular result,
and spectacular results that confirm the plan are exactly the ones to re-run
first. The arm that disagreed with expectations — hybrid coming out **equal to
keyword-only in every single cell** of sweeps 2 and 3 — is the one that should
have been surprising, and on a working vector half it would have been. It was not
surprising because the degrade path *is* the keyword path.

*The arithmetic tell, recorded on the span.* This is the concrete fix and it is
cheap. RRF at `k = 60` gives a document found by exactly one ranker a score of
`1/(60 + rank)`. So a fused result set whose top score is **0.0167** was found by
one half, and one whose top score is **0.0321** was found by two. Both numbers
appear in `docs/retrieval.md` — §2's worked example was taken on a healthy
service and §9's probes were not. Nothing in the response says which ranker
contributed; the arithmetic does. Putting that tell on the `retriever.search`
span, computed from the top fused score, would have made every one of those
sweeps say *this hybrid query went lexical-only* in its own trace, and the false
conclusion would have been impossible to draw.

**What was deliberately not done.** Nothing, in the lane. A retriever that
retried until the vector half answered would be measuring a service that does not
exist; one that inferred the tell and declined would turn a degraded answer into
no answer. The candidates are tracked (`chip-wez`) rather than chosen, and the
headline number survives because the reranked arm production sends reorders a
union the lexical half is always in: **allergen top-3 recall 100% on 4 of 8 allergen questions, three sweeps
out of three.**

**The general lesson.** A committed baseline is an artefact other people will
reason from without re-deriving it. The bar for writing a number into one should
be higher than the bar for observing it, and the difference between the two is a
second run.

### 9.2 A row access policy was weakened on the live account to make a job work

**What happened.** The first live nightly publish staged 18,898 orders, swapped
them into `CHIP_CHAT.ACCOUNTS.orders`, counted the target, read **0**, and
stopped. The cause is that `VISITOR_ISOLATION` compares `demo_id` against a
session variable the publisher never sets, and **Snowflake has no owner
exemption** — a row access policy filters the table for whoever reads it,
including the role that owns it.

The fix applied to the live account was a third clause in the policy body:

```sql
OR CURRENT_ROLE() = 'CHIP_CHAT_PUBLISH'
```

applied with `ALTER ROW ACCESS POLICY … SET BODY`, because `CREATE OR REPLACE` is
refused while a policy is attached to anything.

**Why it looked right.** `CHIP_CHAT_PUBLISH` is a batch role held only by the
Databricks job's service user. It is reachable from no session the model or the
app can open. There is no path from a visitor to that role. Every sentence in
that argument is true, and the change was still wrong — which is the part worth
dwelling on, because *a wrong fix defended by a false premise is easy to catch,
and this one had no false premise in it.*

**Why it was wrong.** `snowflake/tests/test_row_access_policies.py::test_no_policy_body_exempts_a_lane_role`
refuses any lane role named in any policy body, iterating
`("CHIP_CHAT_READ", "CHIP_CHAT_WRITE", "CHIP_CHAT_PUBLISH")`, and its reasoning
survives this case exactly: **a lane role that appears in a policy body is a lane
role the policy has stopped applying to.** #43's acceptance criterion is that a
session with no `demo_id` set reads zero rows from every visitor-scoped table —
*for every role*, not for every role but one. And a role-only clause fails safe in
the wrong direction: a publisher that could see everything and a publisher that
could not would look identical, where the whole point of default deny is that
they do not.

The repository's own test would have refused the change. The change was made on
the live account, where the test does not run.

**The fix that was kept.**

```sql
SELECT MAX(row_count) FROM CHIP_CHAT.INFORMATION_SCHEMA.TABLES
 WHERE table_schema = 'ACCOUNTS' AND table_name = 'ORDERS'
```

`INFORMATION_SCHEMA.TABLES.ROW_COUNT` is metadata *about* a table rather than a
read of its rows, so no row access policy filters it. The publisher gets its
number and the guarantee is not touched to give it. Measured on the live account
after the clause was removed again:

| Role, nothing bound | `SELECT COUNT(*)` | `INFORMATION_SCHEMA.ROW_COUNT` |
| --- | ---: | ---: |
| `CHIP_CHAT_READ` | 0 | 18,898 |
| `CHIP_CHAT_PUBLISH` | 0 | 18,898 |

The left column is the isolation guarantee holding for both roles; the right is
the publish verifying its own swap. `publish.row_count()` builds that query and
`databricks/tests/test_publish.py::test_the_landed_count_reads_metadata_and_not_the_rows`
asserts `"INFORMATION_SCHEMA.TABLES" in query` and `"COUNT(*)" not in query` for
every published table, so the refused fix cannot come back quietly.

**The lesson, and it is about process rather than SQL.** The security property
here is not defended by anyone's care. It is defended by a test, and the test was
not in the room when the change was made — because the change was made against a
live account with a `snow sql` command at the point of failure, and no gate exists
on the shape of a running account's policy body. The generalisable fix is that
**a live-only fix should be treated as a proposed diff to the checked-in artefact
and run through the same gate**; the smaller concrete fix is that
`make snowflake-verify` should assert the live policy body matches
`snowflake/sql/10_policies.sql` rather than only asserting the behaviour. It
currently asserts behaviour, and behaviour is precisely what a role-only exemption
preserves.

One residual, recorded because it is exactly the kind of thing this section is
about: the status block at the top of `docs/nightly-publish.md` still says the
policy *does* exempt `CHIP_CHAT_PUBLISH` and that the exemption belongs in
`10_policies.sql`. §7 of the same file says it was removed and the SQL is
untouched. The two blocks contradict each other, and the stale one is the one a
reader meets first.

### 9.3 The nightly reset would have reported `ok: true` forever

**What happened.** `demo_visitor_baseline` is the table the reset restores an
aged-out visitor *to*, and its correctness rests on one property: it must be
loaded in the same run as the table it mirrors, so that it is the generator's own
output rather than a second generation's. `load.py` does exactly that — it fills
`demo_visitor_baseline` from `demo_visitors.jsonl` in the same run as
`demo_visitors`.

The baseline table arrived with #47, **after** the population had already been
loaded. So there had never been a run in which to fill it. Every table existed.
Every policy was attached. Every offline test passed.

And the reset's cursor **inner joins** the baseline — deliberately, because a
visitor the baseline does not carry must never be aged out, since deleting a
visitor's live rows and restoring nothing is worse than leaving them alone. So a
visitor with no baseline row **drops out of the join rather than raising**. The
nightly task would have run every morning, aged nobody, deleted nothing, and
reported

```json
{"visitors_aged": 0, …, "held_no_baseline": 0, "ttl_days": 2, "ok": true}
```

forever, for as long as nobody looked.

**Why this one is the most frightening of the seven.** Every other failure here
announced itself: a job stopped, a probe failed, a number moved. This one produces
a clean receipt. The safety property that made it survivable — the inner join — is
the same property that made it silent, and that is not a coding error, it is a
genuine tension in the design. Failing safe and failing loudly are different
things, and this code chose the first.

It was recoverable only because no visitor had ever written to the account, so
the live `demo_visitors` was still the loaded generation exactly and the baseline
could be filled from it without inventing a second generation. One real
conversation earlier and it would not have been.

**The fix, and the shape of it.** `make snowflake-verify` now fails by name on an
unfilled baseline — *"every visitor has the baseline the reset restores them
to"* — so the next occurrence is loud. The generalisable version: **every receipt
field whose zero can mean either "nothing to do" or "nothing happened" needs a
separate check that the precondition held.** `held_no_baseline` was already on the
receipt for this reason; what was missing was anything that looked at it.

The same live run turned up a second finding of the same family, and the contrast
is the useful part. The first ageing attempt aged nobody, because `last_active` is
the **greatest** of the visitor's own `last_seen` and every live row's timestamp —
a visitor whose `last_seen` says last week and whose receipt says one minute ago
is a visitor who is still here. The reset reported her `dirty` and skipped her,
correctly, and said so on the receipt. Same code path, same zero, opposite
outcome: that zero was explained on the receipt and the other one was not.

### 9.4 Three bugs that only deploying could find, against a green test suite

`docs/deployment.md` §3 is titled *"What surprised me"* and is the most useful
section in the repository. Three of its entries are the ones to dwell on, because
all three were invisible to a passing `make ci` and all three were fatal to the
visitor.

**The ingress killed every turn with the answer already written.** Every turn
against the deployed app came back to `curl` as *"Error in the HTTP/2 framing
layer"* at **60.19 seconds, ten times out of ten, to the second decimal place.** A
number that repeatable is a timeout, and the container's own access log settled
where: the app had written `POST /api/chat HTTP/1.1 200 OK`. It had finished the
turn and produced the answer, and the connection carrying it was already gone.
Container Apps ingress closes a response that has sent no bytes for sixty seconds
— and with a median turn of 34.2 s and a p95 of 62.7 s, that is not an edge case,
it is the p95. **The worst failure available to this system: the visitor is billed
for tokens they never see, and the trace records a successful turn nobody read.**

The fix is a `{"type":"waiting"}` heartbeat every ten seconds on the NDJSON branch
(`_HEARTBEAT_SECONDS: Final = 10.0`). The *first* attempt is the instructive part:
an `{"type":"open"}` frame sent before the turn started, on the theory that a
response whose headers are flushed is a response the ingress will wait for. It is
not — the stream still died at 60.19 s with one frame delivered. **The timeout is
on idleness, not on the response having begun**, and the only way to learn that
was to deploy the wrong fix and watch it fail identically.

**One replica served one conversation at a time, and the liveness probe queued
behind a model call.** `POST /api/chat` was an `async def` handler calling
`_run_turn` synchronously. `_run_turn`'s docstring says it is synchronous *on
purpose*, on the strength of FastAPI running a `def` handler's work in a thread
pool — reasoning that is correct and that this handler was not eligible for. So a
turn held the only event loop the process has; `/healthz` went unanswered for the
length of it (measured at **44 seconds** while one turn was in flight and
**13 seconds** while another was); Container Apps concluded the container was dead
and restarted it **mid-conversation**. A restart clears the in-process
`BudgetLedger`, so a busy afternoon is a restart loop that also resets the daily
spend counter. A second concurrent visitor's request was cut at exactly 60.2
seconds with no HTTP status at all, bypassing the friendly stop state entirely.

The fix is `run_in_threadpool`. The bug is one keyword.

**Startup work in front of a one-second probe produced a twenty-minute
crash-loop.** The Phase 8 revision never became ready: healthy for about
thirty-five seconds after each start, then silent, then restarted — **every ninety
seconds, for twenty minutes.** Two contributing faults besides the loop one.
Assembling the photo intake in `build_service` constructed two Azure SDK clients
and a `DefaultAzureCredential` **before uvicorn started serving**; nothing that
talks to Azure belongs in the start-up of a process that scales from zero. And
Container Apps' probe defaults are a **one-second timeout with no initial delay**,
which a cold Python process on a fraction of a vCPU cannot possibly meet, so the
platform opens a restart loop against an application that is merely starting.
`compute.tf` now sets ten seconds of grace, a five-second timeout and three
consecutive failures — *forty-five seconds of evidence before a restart, rather
than three.*

The sentence in the deployment doc is the one to keep: **"A liveness probe is a
statement about what 'alive' means, and the default statement is 'answers HTTP
within one second, from the instant the process exists'. For an app that starts
cold and occasionally blocks, that statement is false, and the platform enforces
false statements enthusiastically."**

All three were invisible to the test suite, and not because the suite is thin.
They are properties of the *ingress*, of the *event loop under a real probe*, and
of the *cold-start path* — three things a test suite substitutes away by
construction. `.github/workflows/deploy.yml` now starts the image and curls
`/healthz` before it is allowed near the Container App, so the next version of
this is a red CI run. That is the right lesson and it is a narrow one: it catches
the third bug and neither of the first two.

There is a fourth in the same family and it deserves a sentence, because it is the
purest example. `FundedTurn.run` never forwarded `confirmed_draft_id` to
`run_turn`, so `CONFIRMATION_NOTE` was dead code and **the model could not see the
Confirm button** — it refused a confirmed order forever. §3.10 titles it *"The bug
only the deployment could find"* and explains why: **no test would have found
this, because every test scripted the model's next move.**

### 9.5 An experiment won the headline and lost everywhere that mattered

`eval/experiments/COMPARISON.md`, `shipped` → `lean-lanes`, same 34-row dataset,
two prompt versions twenty-four minutes apart:

| | Baseline | Candidate | Δ |
| --- | ---: | ---: | ---: |
| **Tool-selection accuracy** | 42.9% | **55.0%** | **+12.1** |
| Task completion | 14.7% | **2.9%** | **−11.8** |
| Action lane, completion | — | — | **−57.1** |
| Requirement T3 | 100.0% | 0.0% | **−100.0** |
| Requirement T5 | 100.0% | 0.0% | **−100.0** |
| Requirement T2 | 62.5% | 0.0% | −62.5 |

Ten regressions. **The candidate improved the metric PRD §05 names first and
destroyed the product**: the action lane lost 57 points of completion, the two
requirements sitting at 100% went to zero, and task completion fell to a single
row out of thirty-four.

**An aggregate would have shipped it.** *Tool-selection accuracy up twelve points*
is a defensible sentence to put in a commit message, and a leaderboard with one
number on it would have said yes. What stopped it is that the comparison report is
built to refuse a single number: it breaks every rate down by lane and by
requirement, it prints the failure *shapes* that moved
(`action: correct −2, extra_tools −2, unscored +7, wrong_lane −3`), and it leads
with **"10 regression(s). Read the breakdowns before the headline."**

Two pieces of that design are worth stealing wholesale. **A rate has to move by at
least 3% to be called a regression, because the dataset is 34 rows and one row is
2.9% of it** — a threshold below one row would report a regression every time a
single case flipped. And **a count has no threshold at all**, because PRD §05
makes the gates zero and one more than zero is one more than zero. Choosing the
threshold from the resolution of the dataset rather than from a convention is the
difference between a report that is read and a report that is ignored.

The honest footnote: both runs are degraded by 429 contention, `unscored` moved by
double digits in several cells, and the comparison says so at the top. A regression
measured under contention is still a regression — the *direction* is robust even
where the level is not — but this pair should be repeated on a dedicated
deployment before anybody concludes anything about the prompt itself.

### 9.6 Lakehouse defects the live workspace had and nothing else did

Eleven of them across bronze, silver and the publish, and the shape matters more
than the count. `docs/silver-conformance.md` says it plainly: **"four of the six
defects below were found by an expectation stopping the update, and a fifth by the
graph refusing to validate… Each cost a cluster start to find, and none is
reachable by `make ci`."**

*Bronze, two.* A quarantine predicate keyed on `_rescued_data` alone lets a
corrupt document through, because a truncated JSON file read with `multiLine`
produces a row of nulls and an *empty* rescued column, indistinguishable from a
legitimately sparse record. Found by seeding one. And `cloudFiles.schemaLocation`
**outlived the tables built from it**: seven `bronze_synthetic` tables arrived
carrying 50 columns each — the union of every file in the directory, so
`order_items` had an all-null `demo_id` and `personas` had a `line_total`. A full
refresh does not reset an explicitly configured schema location, so a schema
inferred wrong once survives the ordinary remedy. The ingestion code was
byte-identical to the code that had passed; what was stale was the *state*.

*Silver, six.* Five expectations stopped an update and each was right to. A
cloudpickled UDF looking for a module the Python worker's `sys.path` never had.
`line_total = unit_price * qty`, which is wrong for the **24,592 of 48,767** lines
carrying a priced modifier. A "says something" expectation on
`catering.chipotle.com`, which is a 395-byte Vue shell fetched for its script
bundle's address and is not a document. A redemption naming both the reward and
the order it was spent on — **13,684 of 32,234** rows. A vocabulary expectation
that every term resolve to an item, when a vessel and a protein are each half of
an entree. And a sixth that was not a declaration at all: the verify job's own
criterion asserted `distinct_blocks < occurrences`, which this corpus fails while
working correctly.

*The publish, four across five attempts, **every one at or after the last step of
a run**.* The session-clock assertion compared a zone against the string `UTC` and
the workspace reports `Etc/UTC` — the same clock, the same offset, a different
spelling — so *"nothing was wrong, nothing could be published, and the alert
fired."* The connector wanted the private key's body rather than the key, and said
so as `IllegalArgumentException: Input PEM private key is invalid`, which names
neither the armour nor the newlines and reads exactly like a corrupted secret. The
verdict would not serialise, because Snowflake counts come back as `NUMBER(18,0)`,
the connector maps them to `Decimal`, and a `Decimal` compares correctly against an
int and then refuses `json.dumps` — learned **at the end of a run in which all
eleven tables had already swapped**. And §9.2's row access policy.

**The thing to take from this is where they sat.** Every one is at or after the
last step: the expectation that runs when the table is written, the verify that
runs when the swap has landed, the serialisation of the verdict that comes after
everything it verified. They cluster there because that is where the first contact
with reality is. A pipeline is a long chain of code that runs without touching
anything real until, at the very end, it does. **The correct inference is not
"write more tests" — it is "make the first contact with reality happen earlier and
more often", which for a lakehouse means a cluster start you are willing to pay for
on every change.**

Regression cover was added where it could be: the two defects that were copied
constants — the ledger's reasons and the vocabulary's derivations — are now
asserted against `data-gen` and against `catalog.records.Derivation` in
`test_silver.py`, so the next disagreement is a `make ci` failure rather than a
cluster start. That is the right move and it converts two of eleven.

### 9.7 A launch gate was closed by bookkeeping, and the harness saved it

**What happened.** Issue #82 — *zero cross-visitor data disclosures*, one of the
two pass/fail launch gates — was closed during a reconciliation pass that matched
the bead id `cc-6k5` to a **commit subject**. (An earlier body-wide grep had
produced a false positive where a commit mentioned a bead in a *"Not done, and
why"* list, so the reconciliation had been tightened from bodies to subjects. It
was still matching strings.)

The comment that reopened it says the necessary sentence: **"That is bookkeeping,
not verification, so here is the measurement."**

**And the measurement is the good part.** The first re-run was **unscored, not
passed**: three visitors against a pool of four is never contended, and RFC-001
§05 is explicit that sequential tests pass regardless of whether the pool leaks.
The harness reported `could have caught a bleed: no` and declined to report a gate
result at all. **That refusal is the single most valuable behaviour in the
red-team harness** — a suite that had returned "0 breaches" there would have been
correct, useless, and indistinguishable from a real pass. The parameters were
wrong, not the app.

Re-run at `--visitors 8 --pool-slots 2 --rounds 6`: **48 attempts, 48 held, 0
breaches, 8 at once, contended, 285.01 s, could have caught a bleed: yes.**

**And the pass is still shallow, and the harness says that too.** It records where
each attack died:

> answered in conversation — the turn called no tool: **it died in the model,
> which is the weakest place for a guarantee to live**

The account lane is unwired, so no tool was called and no Snowflake connection
left the pool. The run demonstrates that the app does not leak today. It does not
demonstrate that the structural guarantee works, because the structural guarantee
was not on the path. Where the mechanism *is* verified is at the database directly
— nine visitor-scoped tables at zero rows for an unbound `CHIP_CHAT_READ` while
`INFORMATION_SCHEMA` shows 18,898 — and in `test_pool_concurrency.py`.

An earlier run had reported one canary crossing, and it is explained: it was made
against a revision where `POST /api/entry` returned 404, so every session resolved
to the same account, and two "visitors" that are one visitor produce exactly that
signature — including the part that looked anomalous, where neither could see
their own draft and one was shown the other's, which is what a shared order desk
on a shared account looks like. The same run coincided with the event-loop bug
dropping connections at the ingress. Both causes are fixed and it did not
reproduce in the thirty concurrent attempts that followed.

**Three lessons, and the third is the general one.**

A reconciliation process that matches identifiers is inventory, and inventory
answers *did somebody write code for this*, never *does the property hold*. For an
ordinary ticket that is a reasonable proxy. For a pass/fail gate it is not a proxy
at all, and gates should be exempt from bulk closure by policy.

An eval that cannot distinguish *held* from *never asked* will report a clean
result to a run that could not have produced anything else. This suite is built
around that distinction — capabilities are probed rather than declared, every
probe fails conservative, and every baseline prints `unscored` beside its rate.

And the sharpest version, from the same campaign: **a throttle in front of a
target silently converts a red team into a clean report.** A rate-limited turn
returns HTTP 200 with a friendly stop message carrying no canary and no receipt,
so it scores `held` on both gates, every time — and with 20 requests per minute in
front of a several-hundred-request suite, *the harder the suite pushed, the
cleaner the report got.* The symptom of this bug is a safety suite that gets
**quieter** as it is scaled up. Any safety eval whose target has a throttle in
front of it has this bug unless somebody went looking for it.

### 9.8 Four more of the same shape, briefly

**The persona roster assigned nobody, on every deployment that has ever existed.**
`chip_chat.api.visitors` was written correctly; there was no Snowflake driver in
the lockfile, so `build_visitors(None)` produced a `VisitorDesk` over a
`StaticRoster`, and `admit` returned `None` for every visitor on the live URL. PRD
§06 names the empty account as the single largest product risk. *That was not a
risk — it was the deployed behaviour.* Fixed by shipping the 28
`persona_fixtures` rows into the container image as an explicitly-named stopgap
that logs a `WARNING` on every use, and retired by `cc-lpy4`.

**A capability probe compared model prose and granted a capability the deployment
did not have.** `_accounts_differ` declared two sessions isolated because their
replies were different *strings* — *"I don't have your name in the account info I
can access. Your rewards balance is 1,340 points…"* against *"You're signed in as
the Ballard regular… Your rewards balance is 1,340 points."* The same rewards
member, two different sentences, and the probe granted isolation. It now compares
facts. **A probe that compares model prose is comparing the temperature.**

**A failed confirmation is discarded with no span attribute and no log.**
`turns.py` drops `OrderDesk.confirm`'s return value, so a stranger presenting
somebody else's draft id is a no-op that leaves no trace — and that is the single
most interesting event this endpoint can produce.

**The strongest available text analyzer is not the one running.**
`AzureTextAnalyzer` exists and is correct; `build_service` passes no `moderator=`,
so the running app screens with `LocalTextAnalyzer`, which recognises published
jailbreak shapes by regular expression and flags **zero harm categories** —
deliberately, because inventing a `hate` verdict from a regex would be exactly the
false confidence #79 was written against. The one-line fix is not made because
`AzureTextAnalyzer` has no test against the live service, and shipping an
unexercised client into the request path of a public endpoint is how a moderation
outage becomes a moderation absence. That is a defensible call, and it is also the
same shape as everything else here: **the criteria are met; the strongest
available implementation is not the one deployed.**

---

## 10. The through-line, and what it means for sequencing

Read §9 back and one pattern dominates.

**Almost every real defect in this project was found by running against something
real. Almost none was found by the test suite.**

| Found by | Defects |
| --- | --- |
| Deploying to Container Apps | the 60 s ingress timeout, the blocked event loop, the cold-start crash-loop, the invisible Confirm button, the roster that assigned nobody |
| Running the job against the live Databricks workspace | eleven lakehouse defects, every publish one at or after the last step |
| Running against the live Snowflake account | the row access policy filtering the publisher, the unfilled baseline, `Etc/UTC`, the PEM body, the `Decimal` verdict |
| Re-running an eval that had already been committed | the AI Search vector defect, and the false conclusion drawn from it |
| Running the red team over a real socket | the throttle that quieted the suite, the prose-comparing probe, the canary crossing |
| Harvesting the whole real menu instead of a fixture | three catalogue designs that "looked obviously right until 192 items said otherwise" |
| `make ci` | — |

That last row is not a criticism of the test suite, and this needs saying
carefully, because the obvious reading of the table is wrong.

**The test suite is what made every one of those fixes safe.** `NaivePool` is why
the pool fix is known to be a fix rather than a hope. The fixed-window chunker
kept in the test file is why the chunker's guarantee is a property rather than a
claim. `test_no_policy_body_exempts_a_lane_role` is why the wrong policy fix could
be recognised as wrong the moment anybody looked at it. The four
`xfail(strict=True)` content-safety tests were written as a *specification*, so
the build would break on the day they started passing. Neutering `_authentic()`
fails four tests, which is how anybody knows those four tests are about anything.
That standard holds across the tree.

So the honest formulation is: **the test suite is excellent at defending
properties and nearly useless at discovering facts.** Every defect above is a fact
about a service — an ingress's idle policy, a platform's probe defaults, a
warehouse's owner-exemption semantics, a search tier's degradation mode, a
connector's decimal mapping, a schema location's persistence. A test suite
substitutes those away on purpose, because that is what makes it fast, and the
substitution is exactly where the bugs are.

### What that implies for how the next project should be sequenced

The system design already contained the right instruction, and the project
followed it and did not follow it far enough:

> Do not run these phases strictly in order. In your first week, wire one
> end-to-end path on hardcoded data — one menu question, one account question, one
> fake order — and put it on the public URL immediately, however embarrassing it
> looks.

**The ugly slice was built and it worked.** It is why `make ci` has run against a
real span schema since week one, why the golden set has been runnable from the
beginning, and why the cost dashboard fell out of the instrumentation. That
instruction is vindicated.

**What it did not say, and should have, is that the slice has to be widened before
it is deepened.** The five lanes were each built to a high standard — retrieval
with an ablation and a labelled set, the semantic view with an argument for every
excluded table, the matcher with a vocabulary generated from the live catalogue,
the ops API with a confirmation precondition and 36 tests — and **not one of them
was ever connected to the deployment.** So on the day the launch gates were
measured, three of the five headline targets were scored against
`chip_chat.agent.hardcoded`, the citation gate could not be scored at all, the
allergen red team came back 13-of-13 unsettled, and gate 1 was defeated by the
model declining rather than by the policy that exists to make it impossible.

Four sequencing rules I would take to the next project:

**1. A lane is not done until it is reachable from the deployed URL.** Not
"implemented and tested" — *reachable*. The definition of done for a capability
should include a request from outside the process that exercises it end to end. On
this project that one rule would have moved the connection factory (`cc-lpy4`)
from a Phase 11 bead to a Phase 4 blocker, and it is the change that moves the
most numbers in §5.

**2. Deploy on day one and on every change, and put the smoke test in CI.** The
three deployment bugs cost a day between them and each was cheap the moment it was
seen. The gate that now starts the image and curls `/healthz` before it reaches
the Container App should have existed before the first deployment, not after the
crash-loop.

**3. Pay for the cluster start.** The lakehouse defects all sat at the end of a run
because that is where reality begins. A pipeline that meets the workspace once a
week meets eleven surprises at once. The corollary is that the cost of running
against live infrastructure has to be budgeted as *development* cost rather than
treated as verification — and §5.6 says what that budget is: about $2 for a
golden-set pass, $1.50 for a Snowflake verify, 40 of 1,000 monthly semantic
requests for a retrieval sweep.

**4. Observe a number twice before committing it.** The false conclusion in §9.1
is the cheapest failure here to prevent and the most expensive to leave in,
because a committed baseline is read by everyone afterwards and re-derived by no
one. Anything that goes into a `BASELINE.md` should have been observed twice, and
where the second observation is genuinely expensive, the file should print `n = 1`
in the same sentence as the number.

And one practice this project got right that I would protect above all the others:
**reporting `unscored` rather than `failed`, and `not measured` rather than an
estimate.** `Scores.gates_pass` is `None` rather than `True` or `False` on every
run this repository can currently produce, and that third value is the correct one.
Every baseline prints its scored column beside its rate. The launch-readiness
table carries *unmeasured* in rows where a plausible number would have been easier
to write, and it was revised after an evidence audit that contradicted an earlier
draft in three places. A project that reports honestly under pressure is a project
whose numbers you can use, and almost every finding in §9 was findable only
because the numbers around it were trustworthy.

---

## 11. What I would build next, and what I would not build at all

### Next, in the order that buys the most readiness per unit of effort

**1. The Snowflake connection factory (`cc-lpy4`).** One credential in Key Vault
and one factory passed to `build_service`. It wires the account and
personalization lanes, replaces the shipped roster with `SnowflakeRoster`, closes
the opening-message contradiction, converts four `partial` PRD requirements into
measurable ones, and makes gate 1's contended re-run exercise a row access policy
instead of a model's reticence. Nothing else in the tree is close on leverage.

**2. Deploy the ops API's functions.** `func-chip-chat-ops-4cy39i` is Running with
zero functions deployed, so `POST /api/place_order` returns 404, the action lane
is unreachable from the public URL, and launch gate 2 has nothing to be attacked.
The code, the confirmation precondition, the retry-key semantics and 36 tests all
exist already.

**3. Build the `ResponseEnvelope` into the reply (`cc-bap`).** `agent.envelope`
exists, is tested, and has no caller. Until it does, PRD K2 is unmeasurable and
the two most rule-shaped checks in the whole suite — `cited` and `minted` — report
34 rows unscored. This is the cheapest way to turn a target into a number.

**4. A judge behind `chip_chat.eval.golden.run.Judge` (#72).** There is no judge
anywhere in the tree, which is why K3 is asked by four cases and scored by none,
why `eval/dietary` came back 13-of-13 unsettled, and why the entire `invention`
family of adversarial attacks is unscored. Two prerequisites the cost work already
identified: `BudgetLedger` must move somewhere shared **before** an out-of-process
judge arrives rather than after, and the judge's own token spend has to land on
the same dashboard as everything else.

**5. Thirty labelled photographs.** The vision lane is the best-designed part of
the system and the least evidenced part of it. Thirty frames turn D3 from an
argument into an F1, and `_DEFAULT_FLOORS` from a plausible set of numbers into a
tuned one.

**6. Purchase Arize AX Free and repoint.** Proven cheap — empty instrumentation
diff, two agent-manifest lines, one Terraform variable and about six lines for the
headers secret. Online evals before the URL is shared is a stated launch criterion,
and Free at 25,000 spans/month is roughly 450 conversations.

**7. Re-measure the five headline targets without 429 contention**, on a dedicated
deployment or serialised, and re-run the `shipped` → `lean-lanes` comparison at the
same time.

Two smaller items that are security rather than readiness: **sign the ops host's
session header**, which is currently a bearer identity standing where D4's session
binding should be; and **add a disclosure detector that does not need a canary**,
because a model that says *"the other person here ordered a burrito bowl"* has
disclosed something real and nothing in the suite can see it.

### What I would not build at all

**A multi-agent router.** The obvious response to 42.9% tool selection is to add a
routing model. The failure shape says otherwise: **zero `wrong_lane`, ten
`no_tool`.** The model does not confuse the lanes; it declines to call tools that
are not registered, and a router in front of an unregistered tool routes to
nothing. Wire the lanes and re-measure — the number that would justify a router
does not exist yet.

**A retriever that retries until the vector half answers, or one that detects the
tell and declines.** The first measures a service that does not exist. The second
turns a degraded answer into no answer, and the reranked arm production sends is
unaffected by the defect in every measurement taken. Record the tell on the span
so a trace can say *this hybrid query went lexical-only*, and leave the behaviour
alone.

**AI Search Basic at $73.73/month, bought to fix the vector defect.** It would
settle it, and that is the wrong reason to spend the money: the arm production uses
measures 100% allergen recall on all three sweeps. If Basic is ever bought it
should be bought for what the tier decision actually gave up and did not count —
**the Free tier has no managed identity, no customer-managed keys, no IP firewall
and no private endpoints** — which is the strongest non-reranker argument for it
and was not the reason the tier was chosen.

**Iceberg for the nightly publish.** 1.53 MiB once a night. Iceberg's principal
benefit is avoiding duplication at a scale this project explicitly is not trying to
reach, and swapping the mechanism later changes one job rather than the
architecture. The loss — that a platform this project exists partly to exercise
goes unexercised — is real, recorded, and smaller than the deadline risk.

**`no_public_ip = false` to delete the NAT gateway.** It would save $36.50 a
month, requires the Databricks workspace to be replaced, and is a security
downgrade. That trade is not obviously worth it and it is not obviously not; it
belongs written down rather than taken quietly in a cost review.

**A second Snowflake account in a native region, to fix the account lane's cost.**
It would fix the latency and change the cost not at all — 67 credits per 1,000
messages is Snowflake's published rate everywhere. The 30-day clock and roughly
$400 of credits are already spent on the account that exists.

**Any prompt instruction that makes the model reluctant to call
`ask_account_question`.** It is the cheapest-looking fix for the $0.20 problem and
it is a prompt standing in for a control — the exact thing this repository refuses
to do about writes, and it must not start doing it about spend. Widen the
deterministic fast path (`get_points_balance` already is one) or put a per-session
ceiling on Analyst messages in the same place the token ceiling lives.

**Fine-tuning a model on menu data.** Rejected in the RFC and the rejection has
only got stronger. It trades a citable, updatable corpus for weights that go stale
the moment the menu changes and cannot cite anything — the exact opposite of what
D9, K2 and the allergen boundary all need.

**Group ordering, multi-meal photo resolution, and persona editing beyond the
three fields.** Each has a decision record explaining why V0 stops where it does
and each argument still holds. The multi-meal one is the best of them: the stage-4
schema returns one slot set, so on a table of four bowls those slots describe the
*photograph*, and building from them produces a well-formed order composed
entirely of real menu items that nobody in the picture is eating.

**And a leaderboard with one number on it.** §9.5 is the argument. The aggregate
would have shipped a candidate that improved tool selection by twelve points and
took two requirements from 100% to zero.

---

## 12. What is genuinely ready

A no-go reads as a verdict on the whole project and it is not one, so this is
worth stating last.

The isolation mechanism is real and proven at the database, with a concurrency
test that ships its own negative control. The confirmation precondition is
structural and survives a deliberately sabotaged system prompt. The spend cap is
inline, was tripped for real over a real socket, and consumes zero tokens while
tripped. Moderation runs before the model by construction rather than by
convention — `TextModerator` is private to `SpendGate`, and the only object in the
process that can call a model is one that `SpendGate.turn()` yields after both
checks have passed. The corpus is chunked so that no nutrition row splits, and the
chunker's guarantee is proved against a real fixed-window chunker kept in the test
file. Allergen top-3 recall is 100% on three sweeps out of three — on the four questions whose vector arm was not silently dropped, a denominator this document could not state until `chip-wez` built the detector that measures it. The index alias
swap is atomic across a live continuous query; the nightly publish is atomic per
table and leaves a consistent previous generation when killed. The gold marts
rebuild deterministically, and the recommender is registered in Unity Catalog
behind a `@champion` alias that a run only takes by beating a popularity baseline
on *novel* hits — 0 of 138 visitors were recommended something they had already
ordered. The unaffiliated framing is correct and unmissable: sticky,
non-dismissible, `noindex`, `Disallow: /`, no logo, wordmark or brand colour.

And the evaluation harness caught a regression the aggregate would have called an
improvement, and refused to score a launch gate whose run could not have caught a
failure. Those two behaviours are the reason the rest of the numbers in this
document can be believed, including the bad ones.
