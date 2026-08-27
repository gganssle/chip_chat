# Trajectory and tool-selection baseline

- **Traces from** — week-one slice, routing handed to it (routing-oracle)
- **Dataset** — cilantro-golden-set `9ba196eb786c`, 34 rows that score routing
- **Target** — tool-selection accuracy ≥ 95.0% (PRD §05)

> **This is not a score for the agent.** Lane selection was handed to the deployment: `RoutingOracle` calls, for each message, exactly the tool the row expects. Nothing about model quality survives a model that was told the answer.
>
> What the number below measures is the plumbing at its ceiling -- give a deployment perfect routing and this is what the trajectory eval gets out of it anyway. Every shape left in it is a property of the wiring: the week-one slice registers six of the eleven tools, and a tool that is not registered cannot be routed to, so it comes back `no_tool` however good the model is. A span tree cannot tell that apart from a model that chose not to call, which is why this paragraph exists.

## Coverage

34 rows score routing; 9 of 9 scope clauses met.

- **Thin** — vision: fewer than 3 rows, so the lane's percentage is a fraction with a very small denominator rather than a rate.

## The metric

| | |
| --- | --- |
| Rows run | 34 |
| …scored | 32 |
| …unscored (trace could not be believed) | 2 |
| Split traces (#103) | 0 |
| **Tool-selection accuracy** | **68.8%** (target ≥ 95.0%) |
| Clean trajectories | 68.8% |
| Rows where a wrong query is observable | 17 of 34 |

**Target not met.** 68.8% against ≥ 95.0% — a gap of 26.2%, made of 10 no_tool.

The gap's *shape* is the explanation, not its size: the same number made of `no_tool` and made of `wrong_lane` is two different problems with two different fixes.

## By lane

| Lane | Rows | Scored | Tool selection | Clean | wrong lane | no tool | extra tools | wrong query |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| knowledge | 12 | 12 | 100.0% | 100.0% | 0 | 0 | 0 | 0 |
| account | 7 | 6 | 16.7% | 16.7% | 0 | 5 | 0 | 0 |
| personalization | 4 | 3 | 66.7% | 66.7% | 0 | 1 | 0 | 0 |
| action | 7 | 7 | 57.1% | 57.1% | 0 | 3 | 0 | 0 |
| vision * | 1 | 1 | 0.0% | 0.0% | 0 | 1 | 0 | 0 |
| none | 3 | 3 | 100.0% | 100.0% | 0 | 0 | 0 | 0 |

`*` fewer than 3 rows. See the caveat above.

**Below 95.0%:** account at 16.7%, personalization at 66.7%, action at 57.1%, vision at 0.0%.

## The four failure shapes

**wrong_lane** — 0. chose, and chose the other thing. A tool-description problem before it is a prompt one -- `chip_chat.agent.surface` is where the lanes are separated, and `python -m chip_chat.agent.selection` is how a change there is measured.

**no_tool** — 10. answered from what the model already knew. The quiet killer for groundedness: the prose reads fine and nothing in it is attached to anything. Also what a deployment produces when the tool was never registered, which a span tree cannot tell apart from a choice.

**extra_tools** — 0. reached the lane, and paid for more calls than the turn needed. A cost and latency finding rather than a correctness one; PRD section 05 asks for cost per conversation, and this is where it leaks.

**wrong_query** — 0. right lane, wrong ask. Only observable on the two tools that take the question as an argument, and only where the query drifted off the message entirely -- the subtle paraphrase needs a judge.

`wrong_query` is measured on the 2 tools that take the ask as an argument (ask_account_question, search_menu_knowledge) and on no others, so a zero here is not evidence that no query drifted elsewhere.

## Traces that could not be believed

- `golden/p1-usual-low-confidence` — this slice serves the regular persona (persona-loyal-regular); the case presumes explorer
- `golden/p3-stored-value-unprompted` — this slice serves the regular persona (persona-loyal-regular); the case presumes lapsed

## Failures

### `golden/a1-last-order` — no_tool

- **Asked** — 'what did i order last time'
- **Expected** — ask_account_question (account)
- **Called** — nothing
- **What went wrong** — answered without calling ask_account_question; 1 model round trip(s), no tool span
- **Why this row exists** — One specific past order, which is a query. The usual is a precomputed habit and a different mart -- a model that answers this from usual_order is right by luck for the Regular and wrong for everybody else.

### `golden/a2-spend-this-year` — no_tool

- **Asked** — 'what have i spent here this year'
- **Expected** — ask_account_question (account)
- **Called** — nothing
- **What went wrong** — answered without calling ask_account_question; 1 model round trip(s), no tool span
- **Why this row exists** — An aggregate over a time range, which is the whole reason Cortex Analyst is in the design rather than a handful of fixed lookups.

### `golden/a2-store-last-visit` — no_tool

- **Asked** — 'when did i last go to the ballard store'
- **Expected** — ask_account_question (account)
- **Called** — nothing
- **What went wrong** — answered without calling ask_account_question; 1 model round trip(s), no tool span
- **Why this row exists** — A time range with a dimension filter on it. The semantic view has to carry the store, or this comes back as confident nonsense.

### `golden/a2-most-visited-store` — no_tool

- **Asked** — 'which store do i go to most'
- **Expected** — ask_account_question (account)
- **Called** — nothing
- **What went wrong** — answered without calling ask_account_question; 1 model round trip(s), no tool span
- **Why this row exists** — An aggregate with no time range, deliberately: an answer that silently applies one is wrong in a way nobody will notice.

### `golden/a4-unanswerable-aggregate` — no_tool

- **Asked** — 'how many calories have i eaten here this year'
- **Expected** — ask_account_question (account)
- **Called** — nothing
- **What went wrong** — answered without calling ask_account_question; 1 model round trip(s), no tool span
- **Why this row exists** — The account lane is the right lane and the answer is still no: nothing joins an order line to a calorie count in the semantic view. A4 is the difference between saying so and returning a plausible number.

### `golden/p2-recommendation-grounded` — no_tool

- **Asked** — "what should i try that i haven't had before"
- **Expected** — get_recommendations (personalization)
- **Called** — nothing
- **What went wrong** — answered without calling get_recommendations; 1 model round trip(s), no tool span
- **Why this row exists** — The other mart. PRD P2 wants this grounded in the visitor's own behaviour rather than in generic popularity, which is exactly the difference a recommender earns its place by.

### `golden/t1-cancel-order` — no_tool

- **Asked** — 'yes, cancel it'
- **Expected** — cancel_order (action)
- **Called** — nothing
- **What went wrong** — answered without calling cancel_order; 1 model round trip(s), no tool span
- **Why this row exists** — One of T1's six, and the one docs/action-surface.md section 10 marks as invented: Chipotle's published FAQ refuses cancellation outright. The receipt has to say so, which is why this case exists rather than being dropped as unrealistic.

### `golden/t1-redeem-points` — no_tool

- **Asked** — 'yes, redeem it'
- **Expected** — redeem_points (action)
- **Called** — nothing
- **What went wrong** — answered without calling redeem_points; 1 model round trip(s), no tool span
- **Why this row exists** — The redemption write, and the one miss common to every run of chip_chat.agent.selection: 'redeem my free guac' goes to get_points_balance even with both descriptions naming the boundary. Worth a case here so the fix, when it comes, is a number.

### `golden/t1-update-preferences` — no_tool

- **Asked** — 'yes, save that'
- **Expected** — update_preferences (action)
- **Called** — nothing
- **What went wrong** — answered without calling update_preferences; 1 model round trip(s), no tool span
- **Why this row exists** — The last of T1's six, and one of the three fields E7 lets a visitor edit. A standing preference is a write like any other and gets the same two-step.

### `golden/v2-photo-routing` — no_tool

- **Asked** — "here's a photo of my friend's bowl, make me that"
- **Expected** — match_meal_from_photo (vision)
- **Called** — nothing
- **What went wrong** — answered without calling match_meal_from_photo; 1 model round trip(s), no tool span
- **Why this row exists** — The one thing about the vision lane the labeled photo set cannot measure: whether a photo turn reaches the lane at all. eval/photos runs the lane directly, so lane selection is invisible to it. Everything downstream of the tool call -- components, the not-Chipotle case, the clarify case, several meals in one frame -- is measured there and delegated from here.

