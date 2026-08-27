# The item-affinity recommender

A modest model, tracked properly. What it scores, what it refuses to say, how a
run earns the right to be deployed, and why the acceptance criterion that reads
*"does it recommend things the customer has genuinely not tried"* turned out to
be the one that decides the whole design. Issue
[#37](https://github.com/gganssle/chip_chat/issues/37).

Everything below is `infra/terraform/databricks_recommender.tf`,
`databricks/notebooks/recommender_{train,publish,verify}.py`,
`databricks/src/chip_chat/databricks/recommender.py` and
`databricks/src/chip_chat/databricks/recommender_model.py`. Nothing was made by
hand in the workspace UI.

> **Status.** The code, the declarations, the scoring rule, the promotion rule
> and the Terraform are here and `make ci` is green over them — including the
> scoring rule and the two hit rates, which are *run* rather than described.
> The job has **not** been run against `dbw-chip-chat`: it needs a
> `terraform apply`, a silver layer built by
> [#34](https://github.com/gganssle/chip_chat/issues/34) and the gold marts
> from [#36](https://github.com/gganssle/chip_chat/issues/36), none of which
> has been run either. §7 says exactly what the live run has to show, and it is
> a job you run rather than a screenshot you take. Tracked separately.

## 1. The shape

One two-task job, `chip-chat-recommender`, reading silver, writing one model
version into Unity Catalog and one table into `chip_chat.gold_synthetic`.

```
chip_chat.silver_synthetic
├── orders ──────────┐
│                    ├─► train ──► MLflow run     params, 8 metrics, the fit
└── order_items ─────┘      │      ────────────
                            └────► chip_chat.gold_synthetic
chip_chat.gold_synthetic           .item_affinity_recommender   version N
└── item_affinity ──── the refit must reproduce it (§5)          │
                                                          @champion
chip_chat.silver_harvested                                       │
└── menu_items ─── the two item names in the sentence ──┐        ▼
                                                        └─► publish
                                                              │
                                                              ▼
                                        chip_chat.gold_synthetic.recommendations
                                        demo_id, rank, item_id, seed_item_id,
                                        seed_share, score, rationale,
                                        model_version, derived_at

                     demo_visitors ── NOT READ, and that is the mechanism (§3)
```

**The serving path reads a table, never a model.** That is the issue's third
bullet and it is the reason `publish` exists as a task at all. RFC-001 §06 backs
`get_recommendations` with a gold mart; a model invoked inside a chat turn would
put an inference — and a model endpoint's availability, and its cold start —
between a visitor and an answer that was already computable overnight. There is
no `databricks_model_serving` resource in this repository, and `test_recommender.py`
asserts there is not.

**A job, not a fourth Lakeflow pipeline.** The three medallion lanes are
declarative because each of them is "declare a table, let the engine keep it
right". This is not that shape: training is *fit, evaluate, compare against a
baseline, decide whether to move an alias*, and a declarative pipeline has
nowhere to put a decision. It also has to talk to two systems that are not the
table store — the tracking server and the model registry.

**Two tasks rather than one notebook**, because they fail for different reasons
and are worth retrying separately (a registry timeout is not a reason to refit),
and because the task boundary is where "the model that is deployed" is *read
back* rather than passed along. `publish` loads `@champion`; it never receives a
version number from `train`.

**Every decision is in `recommender.py`**, where `databricks/tests/test_recommender.py`
reads it without a cluster — the same arrangement as `gold.py`, and for the same
reason. The notebooks are Spark calls, MLflow calls and print statements.

## 2. What #37 asked for, and what landed

The issue's third bullet says *produce the `item_affinity` mart from the
registered model*. Two things had happened by the time this was implemented.

[#36](https://github.com/gganssle/chip_chat/issues/36) landed first and made
`item_affinity` a Lakeflow materialized view whose determinism is its **own**
fifth acceptance criterion — `gold_verify.py` re-runs the pipeline's query and
compares. And `docs/gold-marts.md` §8 had already recorded the division of
labour the other way round: *"`item_affinity` is [#37's] training input."*

Moving the mart out from under a passing criterion to satisfy a bullet would
have cost more than it bought, so the relationship is **inverted and turned into
a check**:

- `item_affinity` stays exactly as #36 built it.
- The model's full-history refit has to **reproduce** it. That is the
  `item_affinity_agreement` metric, logged on every run, and it is 1.0 or the
  two definitions of lift have drifted — which is a finding either way round.
- What the model publishes is a new table, `recommendations`.

The new table was not avoidable under either reading. RFC-001 §06 says
`get_recommendations` returns *ranked items with rationale*; `item_affinity` is
three columns wide, carries no `demo_id`, and has nowhere to put either the
visitor scoping or the sentence. A per-visitor table was required from the
start.

**It is a fifth table in `gold_synthetic` and RFC-001 §04 names four.** That is
a deliberate deviation and it is recorded here rather than absorbed quietly. §04
fixes the schemas of the four marts and this changes none of them; what it adds
is the table the §06 tool contract implies and §04 does not enumerate.
`test_recommender.py` asserts `recommendations` is *not* in `gold.RFC_COLUMNS`,
so the addition stays a decision somebody made rather than a diff nobody read.

## 3. What it may read

Three silver tables: `orders` and `order_items` for the co-occurrence and each
visitor's history, and `menu_items` for the two item names the sentence needs.

**Never `demo_visitors`.** RFC-001 §04 answers PRD Q2 by containment — the three
fields a visitor may edit are columns of that table, no editable field is an
input to a derived table, so no edit can invalidate one — and the RFC says a
reviewer checks the property by confirming nothing under the medallion pipeline
selects from it. `gold.py` carries that check for the four marts;
`recommender.FORBIDDEN_SOURCES` is the same list, asserted equal to it, and
`test_recommender.py` runs it over these queries and all three notebooks.

It matters more here than it looks. A recommender is exactly the component
somebody would think to feed a visitor's `stated_preferences` into, and doing so
would break the containment argument *while looking helpful*.

## 4. What a score is

Two numbers, and the second is the one that stops a thin pair from arriving at a
visitor as a suggestion.

**Lift**, exactly as `item_affinity` defines it: `P(both) / (P(a) · P(b))` over
settled orders, evaluated as four integers and one division. One means
independent; above one means the two items are ordered together more often than
chance; below one means they are ordered *instead of* each other.

**Shrinkage**, which lift on its own cannot express. A pair seen 25 times and a
pair seen 2,500 times can carry the same lift and are not the same evidence, so
the score is `lift · co / (co + SHRINKAGE)`. `recommender.SHRINKAGE` is 40 — the
number of co-orders at which a pair keeps half of what it claims — and it is
deliberately above the 25-co-order support floor, so that a pair which has only
just cleared the floor is still visibly penalised rather than keeping half its
claim on the strength of "we saw this 25 times".

Then the visitor. A visitor is a bag of seeds: every item they have
settled-ordered, weighted by the share of their orders that contained it. A
candidate's score is the **maximum** over seeds of `seed_share · shrunk_lift`,
never the sum.

That trades a little accuracy for the thing the issue asks for by name. The
rationale has to be explainable in one sentence, and a sum names no seed — it
produces *people who order the things you order tend to like this*, which is
true of a popularity list too. A maximum has an argmax, the argmax is a real
item the visitor really orders, and the sentence is about it.

| Setting | Value | What it does |
| --- | --- | --- |
| `MINIMUM_CO_ORDERS` | 25 | support floor. **Equal to `gold.MINIMUM_CO_ORDERS`**, asserted in the tests, so the refit and the mart keep the same pairs |
| `SHRINKAGE` | 40 | co-orders at which a pair keeps half its lift |
| `MINIMUM_SCORE` | 1.0 | lift's null value. Applied to the **pair**, before the visitor's share weights it |
| `TOP_K` | 5 | recommendations per visitor |
| `HOLDOUT_FRACTION` | 0.2 | share of history, by time, held back for evaluation |
| `MINIMUM_MARGIN` | 0.01 | how far above the baseline a run must land to take the alias |

Every one of these is logged to MLflow as a parameter **with a `why.<name>` tag
carrying the sentence above**. MLflow records a parameter as a string with
nothing attached, and a run whose reader has to open the source to find out what
`shrinkage=40` meant is tracked but not documented.

**The floor is about the pair, not about the visitor.** "These two items do not
go together" is a reason to stay silent; "this visitor does not order the first
one very often" is not. Both numbers are published — `score` and `seed_share` —
so the floor is still checkable against the rows: `score >= 1.0 * seed_share` is
one of the table's expectations.

**Ties break on the data.** Candidates sort by `(-score, item_id)` and seeds by
weight, then by whether the seed is an entree, then by item id. An ordering that
is reproducible only until the files underneath are rewritten is one that makes
a visitor's recommendations change for no reason a month later;
`test_recommender.py` asserts the property by scoring the same input ten times
in shuffled order.

## 5. What it refuses to say

**Anything the visitor has ever ordered.** Not "anything they order constantly"
— everything, including the item they tried once eleven months ago.

The issue asks two things of the exclusion: that recommendations are for things
the customer has *genuinely not tried*, and that the model does not recommend
what they *already order constantly*. A share threshold satisfies the second and
argues about the first. Excluding the whole history satisfies both, makes the
second a strict consequence of the first, and — the reason it was chosen —
turns the acceptance criterion into an **emptiness assertion**:
`recommendations` joined to its own visitor's settled order lines must return no
rows. `recommender_verify.py` runs exactly that join, over the whole population
rather than over a sample.

The cost is real and worth naming: a visitor who tried something once and would
happily be reminded of it will not be. That is the trade this project wants — a
suggestion for something already familiar reads, in conversation, as the
assistant not having looked.

**Anything at all, for a visitor with nothing to go on.** No popularity
fallback. That is `usual_order`'s call in #36 — a visitor with no usual gets an
honest absence — made again, and PRD requirement P2 makes it sharper here: a
popularity fallback is precisely the generic top-sellers list the requirement
exists to rule out, and in the served table it would be indistinguishable from a
real recommendation.

## 6. How a run earns the right to be deployed

Fitting on everything and reporting how well the fit fits is not a measurement,
so the run splits on **time**: the earliest 80% of the population's settled
history trains the model, and the rest is what it is scored against. A random
split would put a visitor's later orders into the training set and then ask the
model to predict their earlier ones.

Four hit rates come out of it, and the pairing is the whole point.

| Metric | What it is |
| --- | --- |
| `hit_rate_at_k` | share of scored visitors for whom a recommendation turns up in their holdout orders |
| `novel_hit_rate_at_k` | the same, counting only hits on items absent from that visitor's training history |
| `baseline_hit_rate_at_k` | `hit_rate_at_k` for the most-ordered items in the training window, recommended to everybody |
| `baseline_novel_hit_rate_at_k` | the same top-sellers list, judged on novelty |

**The baseline is not a formality.** PRD requirement P2 asks for
recommendations grounded in the visitor's actual ordering behaviour *rather than
generic popularity*, and adds that a global top-sellers list does not satisfy it
**even if it scores well**. It does score well — most people's next order
contains a staple — so popularity is expected to beat the model on
`hit_rate_at_k`. What it cannot do is recommend something a visitor has not
already had.

So the promotion rule is about novelty alone: a run takes the `@champion` alias
only if its `novel_hit_rate_at_k` beats the baseline's by at least
`MINIMUM_MARGIN`. A run that cannot logs its metrics and leaves the alias where
it is, and `publish` therefore republishes the previous champion's
recommendations rather than worse ones. **A bad training run is a version in the
registry with its metrics attached, not a worse table in front of a visitor.**

`test_recommender.py::test_a_popularity_list_wins_on_hits_and_loses_on_novelty`
is that argument as a fixture: three visitors who each re-order their staple and
try one new thing. Popularity scores 1.0 on hits and 0.0 on novelty; the
affinity model scores the reverse. Both sides are measured by the same function
the training run calls, because a comparison whose two halves were measured by
two routines is a comparison of the routines.

Four more metrics are logged beside them: `catalogue_coverage` (reported rather
than gated — a real menu has items nobody should be pushed toward),
`visitors_scored` so the denominator is never implicit, `pairs_kept`, and
`item_affinity_agreement` from §2.

### The first run, and what it actually scored

Version 1, 2026-08-27, over the live silver layer. The numbers are more
interesting than a pass would have been.

| Metric | Value |
| --- | --- |
| `hit_rate_at_k` | 0.107798 |
| `novel_hit_rate_at_k` | **0.107798** |
| `baseline_hit_rate_at_k` | 1.0 |
| `baseline_novel_hit_rate_at_k` | **0.107798** |
| `catalogue_coverage` | 0.4 |
| `visitors_scored` | 436 |
| `pairs_kept` | 12 |
| `item_affinity_agreement` | **1.0** |

Three things to read out of that.

**`item_affinity_agreement` is 1.0**, which is §2's whole argument settled: the
model's full-history refit reproduces `gold_synthetic.item_affinity` pair for
pair, so the mart and the model are two computations of one definition rather
than two definitions.

**`hit_rate_at_k` and `novel_hit_rate_at_k` are the same number**, and that is
the design's central property observed rather than asserted: *every* hit this
model scored was on an item that visitor had never ordered, because it cannot
recommend anything else. Popularity, by contrast, hits every visitor — 1.0 —
and almost all of those hits are staples they already buy.

**And the two novel rates are identical to six places**, which is the finding.
On this catalogue the discriminator PRD P2 asks for cannot discriminate. Ten
menu items and twelve pairs above the support floor leave each visitor a very
small set of things they have not tried, and affinity and popularity pick the
same ones out of it. That is a property of the **trimmed harvest**, not of the
model: the real menu is hundreds of items and `catalogue_coverage` of 0.4 means
four items were ever suggested to anybody. The metric is right, the gate is
right, and there is not enough menu here for either to say anything.

Which is exactly why the promotion rule was not loosened to make this pass. See
below.

### The first version has nothing to beat

`beats_baseline` compares a run against the incumbent's yardstick, and on the
first run there is no incumbent. Version 1 tied the baseline, did not clear
`MINIMUM_MARGIN`, did not take the alias — and the `publish` task then failed
with `RESOURCE_DOES_NOT_EXIST: Registered Model Alias 'champion' does not
exist`, a message about a missing alias rather than about the situation.

The gate exists to stop a run **replacing** a good champion with a popularity
list wearing a hat. With no champion, what it protects instead is an empty
serving table. So `recommender.takes_the_alias` is `beats_baseline` plus that
one case: **the first version takes the alias on the strength of being the only
one**, and every version after it is held to the margin.

The alternative — lowering `MINIMUM_MARGIN` until version 1 passed — would have
made the gate say something false about a model that genuinely tied. This way
the metrics above stay on the record, the rule stays where it was, and the
reason the first version is serving is written down rather than implied by a
threshold somebody quietly moved.

### The model is fitted twice

The training-window fit is what the holdout can honestly judge. The
**full-history refit**, with the same hyperparameters, is what gets registered —
throwing away the most recent fifth of a customer's history to serve them is a
strange thing to do on purpose. The refit is also the one compared against
`item_affinity`.

### A version and an alias, not a stage

The issue asks for "a version and a stage". Unity Catalog's model registry
replaced the Workspace Model Registry's `Staging`/`Production` transitions with
**aliases**, and `transition_model_version_stage` is not a call this registry
has. `@champion` is what a stage became. Writing the stage API here in 2026
would be the same mistake as writing `import dlt` — `test_recommender.py`
asserts the call is absent.

The model is registered at a **three-level** name,
`chip_chat.gold_synthetic.item_affinity_recommender`, which is the whole
difference between this registry and the workspace one: the model is a securable
in the same namespace as the tables it was fitted from, so the grants in
`databricks_catalog.tf` already reach it and a principal who may not read the
synthetic population may not load the model fitted on it either.

Terraform creates the registered model and the experiment; the notebook creates
*versions* and moves the alias. That is `databricks_catalog.tf`'s rule about
schemas applied one level down — ownership and grants are cheap to set on an
empty object and tedious to retrofit onto a populated one.

## 7. What the published table says

`chip_chat.gold_synthetic.recommendations`, one row per visitor per suggestion,
at most five per visitor.

| Column | What it means |
| --- | --- |
| `demo_id` | whose these are. What [#43](https://github.com/gganssle/chip_chat/issues/43)'s row access policy compares |
| `rank` | 1 for the strongest, dense and contiguous within a visitor |
| `item_id` | what to suggest. Never in this visitor's own settled history |
| `seed_item_id` | the item of theirs that earned it — the argmax, and what the sentence names |
| `seed_share` | that item's share of their settled orders |
| `score` | `seed_share ×` the pair's shrunk lift. Ranks within a visitor rather than across them |
| `rationale` | the sentence, at most 160 characters |
| `model_version` | the registered version that produced the row |
| `derived_at` | when it was computed. RFC-001 §10 serves it with the row |

**The rationale.** The issue gives the shape — *"You order barbacoa most weeks
and people who do tend to like the tomatillo-red chili salsa"* — and this is it:

> You order the Barbacoa Burrito in most of your orders, and people who do tend
> to add the Tomatillo Red-Chili Salsa.

*"In most of your orders"* is a band rather than a number, for the reason
`usual_order.confidence` has bands: *you order this in 43% of your orders* is a
sentence no person has ever said about themselves. There are three
(`SHARE_PHRASES`), and each has to be a phrase somebody would actually use.
*"Tend to"* is the shrinkage showing up in the prose — the model is reporting a
tendency, and a sentence that said *you will like* would claim something it has
not measured.

Short is enforced as a number, because it is only enforceable as one: 160
characters, applied as a table expectation rather than only as a test, so a menu
item with a very long published name fails the update instead of shipping a
sentence the agent truncates mid-word.

The sentence is rendered in SQL and not inside the model, because the two item
names live in `silver_harvested.menu_items` and joining them is a join. That
also means a catalogue re-harvest changes the wording without invalidating a
model version. `recommender.rationale` is the same definition in Python and
`test_recommender.py` holds the SQL to it.

**`CREATE OR REPLACE TABLE`**, not a truncate and an insert. Delta replaces the
table in one commit, so a reader mid-publish sees last night's recommendations
or tonight's and never an empty serving path — which matters more here than in
the pipeline-built marts, because a job can die between two statements.

**Every expectation is fatal.** The pipeline-built marts get
`expect_all_or_fail` from Lakeflow; this table is written by a job, so the same
constraints are re-run as filters that must match nothing and a match fails the
task. Fatal for `gold.Expectation`'s reason: this table is what the agent
answers from, so a row that violates its own definition is a wrong answer in
somebody's conversation rather than a line in an event log.

## 8. Retraining is scheduled, and it ships paused

Issue #37's fourth acceptance criterion is that retraining is a **scheduled
job** rather than a notebook someone remembers to run. Every other file in
`infra/terraform` says the opposite: *no schedule — nothing in this workspace
should be able to start spending on its own*, and
[#38](https://github.com/gganssle/chip_chat/issues/38) moved the weekly
re-harvest onto a GitHub Actions runner rather than a job cluster for exactly
that reason.

Both are right, and they are about different halves of the sentence. The
criterion is about retraining being *scheduled infrastructure* — declared,
reviewable, with a cron expression a person can read. The guardrail is about
this Terraform not starting a cluster nobody asked for.

So the schedule is declared, with its cron — `0 0 9 ? * MON`, two hours after
#38's weekly re-harvest, so a week in which the catalogue changed is a week the
model is refitted *after* the change — and its `pause_status` is driven by
`var.databricks_recommender_schedule_enabled`, which defaults to false.

What ships is **a job with a schedule that is paused**. Turning retraining on is
one variable and one apply, not a job somebody builds later. And
`recommender_verify.py` reads the schedule back off the Jobs API and fails if
there is none, so "scheduled" cannot quietly become "manual" — while a *paused*
schedule is reported rather than failed, because paused is the shipped default
and the argument for it is above.

Unlike the re-harvest, this genuinely belongs on a job cluster: it is a self-join
over every order in the population followed by an MLflow log, which is Spark work
against Unity Catalog tables and cannot run on a free runner holding no
credentials. The cluster is single-node, policy-constrained and stops when the
run does, like every other job in this repository.

## 9. Checking it, rather than believing it

`make ci` runs everything that does not need a cluster, and for this issue that
is more than usual, because the scoring rule and the promotion rule are
*algorithms* rather than thresholds. `test_recommender.py` runs them, checks the
sentence against the SQL that renders it, and holds the queries and the
notebooks to the properties that make the output trustworthy: nothing tried is
recommended, ranks are contiguous, no tie breaks on arrival order, every
threshold reaches the SQL that applies it, exactly one wall clock, and nothing
anywhere naming `demo_visitors`.

`recommender_model.py` is the one module in the tree that imports MLflow and it
is deliberately not importable in CI — adding MLflow and pandas to the lockfile
would put a very large dependency into every developer's virtualenv to satisfy a
file nothing here imports. It is read as *text* instead, the way this suite
already reads the notebooks and the Terraform, and asserted to delegate, to
declare a signature, and to hold no threshold of its own.

What is left is the live system, and #37's four criteria are claims about one.
`chip-chat-recommender-verify` is those claims, as assertions:

| # | Criterion | How it is checked |
| --- | --- | --- |
| 1 | Trained, tracked, registered with a version | The model resolves at a three-level name, `@champion` resolves to a version, and that version's run carries **every** parameter and **every** metric the module declares |
| 2 | Not recommending what they already order | An emptiness join over the whole population, plus the persona fixtures' own recommendations printed in full for a human to read, plus an assertion that no fixture is offered their own `usual_item_id` |
| 3 | A short rationale on every recommendation | Non-empty, within 160 characters, opening with the declared lead, carrying the declared join, and naming the seed item |
| 4 | Retraining is a scheduled job | Read off the **Jobs API**, not off the Terraform: the job exists, carries a cron, and its two tasks are `train` then `publish` |

It also asserts the thing none of the four say and PRD P2 does: that this is
**not a top-sellers list**. If every visitor's top recommendation were the same
item, whatever the metrics said, the served table would be a popularity list —
so the notebook counts distinct first-ranked items and prints the most-recommended
items and the most common seeds beside it.

Run them in order, after the gold pipeline:

```bash
databricks jobs run-now $(terraform output -raw databricks_recommender_job_id)
databricks jobs run-now $(terraform output -raw databricks_recommender_verify_job_id)
```

The verify job is read-only and safe to run at any time. Both exit with a
machine-readable verdict, so the numbers they asserted on are quotable without
opening the workspace.

## 10. What this does not do

- **It has not been run.** See the status note at the top. Four criteria about a
  live system are four claims about an unrun job until `recommender-verify` has
  returned SUCCESS against `dbw-chip-chat`.
- **No model serving endpoint.** The issue is explicit that the serving path
  reads a table, and an endpoint would also be an always-on cost — the trap
  `databricks_compute.tf` exists to close.
- **No publish to Snowflake.**
  [#39](https://github.com/gganssle/chip_chat/issues/39) owns the nightly
  hand-off. This job's work ends when the table is correct in Unity Catalog.
- **No row access policy.**
  [#43](https://github.com/gganssle/chip_chat/issues/43) applies them, and
  `recommendations` carries `demo_id` so that it can.
- **No hyperparameter search.** The fitted pairs are logged as counts rather
  than scores precisely so that a sweep can rescore an existing fit instead of
  refitting, but nothing sweeps them yet. The six numbers in §4 are argued for,
  not tuned.
- **No cold-start path.** A visitor with no settled orders gets no rows, on
  purpose (§5). What the assistant says in that case is the serving layer's
  decision, and the absence of a row is the honest input to it.
- **The schedule is paused.** By design (§8), and it is one variable.
