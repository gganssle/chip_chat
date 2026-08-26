# Golden set — baseline

**No deployment has been scored. The set has never met a model.**

This file is written by hand today and is overwritten by
`python -m chip_chat.eval.golden --out eval/golden/BASELINE.md` the first time
there is a deployment and credentials to run it against. Until then it records
what is and is not known — because a file with numbers in it produced from
something other than a model is the exact failure issue
[#29](https://github.com/gganssle/chip_chat/issues/29) exists to prevent.

| | |
| --- | --- |
| Cases in the set | 34 |
| PRD requirements covered by a case | 19 |
| …covered by a delegation elsewhere | 12 |
| …uncovered | **0** |
| Cases scored against a model | **0** |
| Task completion, tool-selection accuracy, groundedness | **unverified** — not unmet |

## What is unverified, and what that blocks

**PRD §05's four golden-set numbers.** Task completion ≥ 85%, tool-selection
accuracy ≥ 95%, groundedness ≥ 0.95, and uncited menu claims = 0. Unverified is
not the same as unmet: nothing has been measured, so nothing has failed. Do not
read the absence of a number as a passing grade or a failing one.

**Both launch gates.** `Scores.gates_pass` is `None` rather than `True` or
`False` on every run this repository can currently produce, and that is the
correct third value: a gate nobody measured has not passed, and PRD §12 makes
both of them blocking.

**Every judged check.** `declines`, `grounded` and `explains` need a judge and
there is none — `chip_chat.eval.golden.run.Judge` is the seam and
[#72](https://github.com/gganssle/chip_chat/issues/72) is where a model lands
behind it. So K3 is *asked* by four cases and *scored* by none. This is the
single largest hole in the set, and it is a hole with a shape rather than an
omission.

**Every citation check, on any deployment that exists today.** See below.

## The one thing this run did find

`chip_chat.agent.envelope` — the response envelope of decision D9, where a
citation is an id the retriever returned rather than a sentence the model wrote
— **exists, is tested, and is imported by no caller.** Nothing in
`chip_chat.agent.loop` or `chip_chat.api.app` builds one, so a citation id has
nowhere to travel and `ChatReply` carries none.

PRD §05's target for uncited menu claims is zero and no deployment in this
repository can count them. That is a wiring gap rather than a model failure, and
it is why `Signal` exists: the citation checks come back **unscored**, not
failed. Bead `cc-bap`.

The same reply carries no tool calls either, which is why the runner's first
adapter is in-process rather than over HTTP: `SliceDeployment` reads the tools
off the conversation the loop actually sent. An adapter over the public URL
cannot score tool selection until the reply says what was called.

## The ceiling run

Free, reproducible, and **not a score for the agent**. It runs the set against
the week-one slice with lane selection handed to it —
`chip_chat.eval.golden.testing.RoutingOracle` calls, for each message, the tool
that case expects. A model told the answer measures nothing about a model.

What it does measure is the plumbing at its ceiling: give a deployment perfect
routing and this is what the golden set can still not get out of it. Every
remaining failure is a tool that is not built.

- **Deployment** — week-one slice, routing handed to it (`routing-oracle`)
- **Set** — `eval/golden/cases.json`, 34 cases
- **Catalogue build** — not checked
- **Judge** — none; see below
- **Signals reported** — card, receipt, tools, writes

## Coverage

19 requirements covered by a case, 12 measured elsewhere, 0 uncovered.

Every requirement covered, every tool exercised, every shape clause met.

## Against the PRD's targets

| Metric | Target | This run |
| --- | --- | --- |
| Task completion | ≥ 85% | 21% |
| Tool-selection accuracy | ≥ 95% | 69% |
| Menu claims without a citation | 0 | -- |
| Writes without confirmation | 0 | 0 |
| Both launch gates | pass | not measured |

7 passed, 10 failed, 17 unscored, of 34 run.

## Per lane

| Lane | Cases | Passed | Failed | Unscored | Pass rate | Tool selection |
| --- | --- | --- | --- | --- | --- | --- |
| knowledge | 12 | 0 | 0 | 12 | 0% | 100% |
| account | 7 | 1 | 5 | 1 | 14% | 17% |
| personalization | 4 | 1 | 1 | 2 | 25% | 67% |
| action | 7 | 4 | 3 | 0 | 57% | 57% |
| vision | 1 | 0 | 1 | 0 | 0% | 0% |
| none | 3 | 1 | 0 | 2 | 33% | 100% |

## What this run did not measure

The deployment does not report: `citations`. Every check needing one of those is unscored rather than failed.

No judge was supplied, so these checks are unscored on every case carrying them: `declines`, `explains`, `grounded`. They are judgements about meaning rather than properties of a payload — see `chip_chat.eval.golden.run.Judge`.

Cases the deployment could not answer at all: `p1-usual-low-confidence`, `p3-stored-value-unprompted`.

## Failures

| Case | Lane | Failed | Why the case exists |
| --- | --- | --- | --- |
| `a1-last-order` | account | routing | One specific past order, which is a query. The usual is a precomputed habit and a different mart -- a model that answers this from usual_order is right by luck for the Regular and wrong for everybody else. |
| `a2-spend-this-year` | account | routing | An aggregate over a time range, which is the whole reason Cortex Analyst is in the design rather than a handful of fixed lookups. |
| `a2-store-last-visit` | account | routing | A time range with a dimension filter on it. The semantic view has to carry the store, or this comes back as confident nonsense. |
| `a2-most-visited-store` | account | routing | An aggregate with no time range, deliberately: an answer that silently applies one is wrong in a way nobody will notice. |
| `a4-unanswerable-aggregate` | account | routing | The account lane is the right lane and the answer is still no: nothing joins an order line to a calorie count in the semantic view. A4 is the difference between saying so and returning a plausible number. |
| `p2-recommendation-grounded` | personalization | routing | The other mart. PRD P2 wants this grounded in the visitor's own behaviour rather than in generic popularity, which is exactly the difference a recommender earns its place by. |
| `t1-cancel-order` | action | routing, confirms_first | One of T1's six, and the one docs/action-surface.md section 10 marks as invented: Chipotle's published FAQ refuses cancellation outright. The receipt has to say so, which is why this case exists rather than being dropped as unrealistic. |
| `t1-redeem-points` | action | routing, confirms_first | The redemption write, and the one miss common to every run of chip_chat.agent.selection: 'redeem my free guac' goes to get_points_balance even with both descriptions naming the boundary. Worth a case here so the fix, when it comes, is a number. |
| `t1-update-preferences` | action | routing, confirms_first | The last of T1's six, and one of the three fields E7 lets a visitor edit. A standing preference is a write like any other and gets the same two-step. |
| `v2-photo-routing` | vision | routing | The one thing about the vision lane the labeled photo set cannot measure: whether a photo turn reaches the lane at all. eval/photos runs the lane directly, so lane selection is invisible to it. Everything downstream of the tool call -- components, the not-Chipotle case, the clarify case, several meals in one frame -- is measured there and delegated from here. |



## How to read the failures above

Nine of the ten are a tool the week-one slice does not offer:
`ask_account_question`, `get_recommendations`, `cancel_order`, `redeem_points`,
`update_preferences`. The slice ships six of the eleven and the set covers all
eleven, which is the arrangement #29 asked for — the set is written against the
product, not against the slice, and runs against the slice from week one anyway.

The tenth, `v2-photo-routing`, is different and is the ceiling run's own limit
rather than the slice's: `match_meal_from_photo` is registered only when a photo
lane is wired, and wiring one needs a vision deployment. The tool is built. Give
`SliceDeployment` a lane and this case joins the other twelve that route.

Two cases could not be run at all, and are counted as errors rather than as
failures: `p1-usual-low-confidence` presumes the Explorer and
`p3-stored-value-unprompted` the Lapsed Customer, and the slice serves one
hardcoded Regular. Scoring the Explorer's question against the Regular's account
would measure the fixture.

The twelve knowledge cases are the interesting column: every one of them routes,
and every one of them is unscored, because what a knowledge case checks is a
citation and a grounding and this deployment can report neither.

## What would move each number

| Number | What it needs |
| --- | --- |
| Task completion | A deployment with the other five tools, plus a judge |
| Tool-selection accuracy | A real model. `make verify-tools` is the cheap preview |
| Uncited menu claims | The response envelope wired into the reply — `cc-bap` |
| Groundedness, K3, P1's reasoning | A judge behind `run.Judge` — #72 |
| The vision delegations | Photographs in `../photos/labels.json` — #56 |

Run it for real with:

```bash
export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
python -m chip_chat.eval.golden --catalog <build> --out eval/golden/BASELINE.md
```
