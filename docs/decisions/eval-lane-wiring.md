# Decision: an eval states which lanes it had, or its numbers are refused

**Bead:** `cc-lanes` · **Decided:** 27 August 2026 · **Measured:** 27 August 2026
**Changes:** `eval/` entirely — the four runners that can execute a deployment, the recorded result shape, the comparison — and `docs/launch-readiness.md` §2
**Does not change:** `make ci`, which stays free, offline and credential-free

---

## What happened

`make experiment-baseline` was re-run after two changes that should each have
moved a number.

1. `cc-lpy4` wired the account and personalization lanes onto the deployment. A
   visitor's points balance and usual order now come from their own rows in
   Snowflake under #43's row access policies, and `ask_account_question` and
   `get_recommendations` are offered to the model at all.
2. The `gpt-5-mini` deployment's capacity went from 10,000 tokens a minute to
   200,000. The previous baseline had been degraded by 429s — `scored` was 13
   and 10 of 34 — and that ceiling was gone.

The re-run came back **byte-identical**. Task completion 14.7%. Tool-selection
accuracy 42.9%. The same failures in the same lanes.

That was the correct result, and it is worth being precise about why, because
"the harness was stale" is the wrong lesson and "the changes did nothing" is a
worse one. `eval/src/chip_chat/eval/golden/slice.py` declared
`lanes: Lanes = NO_LANES`, and every entry point above it took that default and
offered no way to pass anything else. The harness had been asked *how does the
unwired week-one slice score*, twice, and had answered correctly twice.

## Why it was worse than a stale number

`chip_chat.agent.lanes.CONDITIONAL_TOOLS` withholds `ask_account_question`,
`get_recommendations` and `match_meal_from_photo` from a deployment that has no
lane behind them. That is #64's argument and it is right:

> A tool definition the model can see and nothing can answer is worse than an
> absent one: the model will call it, the call will fail, and the trace will
> show a tool span with a refusal in it that reads as a lane outage rather than
> as a deployment nobody finished.

Applied to an eval, it means the **account lane's seven rows scored 0% tool
selection because the correct tool did not exist in the process doing the
scoring**. Not because the model chose wrongly. Not because Cortex Analyst
failed. Because `offered_tools(NO_LANES)` is six tools and the row expected the
seventh.

`docs/launch-readiness.md` §2 had been holding a ≥95% launch target against
that number since it was first recorded.

## The decision

**Three things, and the third is the one that will still matter in six months.**

### 1. Every runner can take real lanes, behind an opt-in flag

`--lanes {none,wired}` on `chip_chat.eval.golden`, `.trajectory`, `.grounding`
and `.experiment`. `none` is the default everywhere.

`wired` calls `chip_chat.api.connect.snowflake_connect` and
`chip_chat.api.app.build_lanes` — the deployment's own two functions rather than
a copy of them. A second assembly path in `eval/` would be a second place where
a credential is resolved and a lane is composed, and the first time the two
disagreed the harness would be scoring a deployment that does not exist. This is
the argument `eval/retrieval` already makes for calling the real `Retriever` and
`eval/adversarial/gate2.py` makes for attacking the real ops service.

**Identity is bound the way the application binds it.** `VisitorPool.for_session`
takes a session id and asks a store who that session belongs to; nothing may hand
it a `demo_id`, and a harness is not an exception to invariant 1. So
`chip_chat.eval.wiring.OneVisitor` is that store, holding one visitor for the
whole run — the rank-one `regular` fixture, read off `persona_fixtures` through
the pool's one deliberately unbound checkout. One archetype for the run rather
than one per case, because `SLICE_PERSONA` already says the golden set is written
for one and a run that rotated visitors would be scoring the roster.

**It refuses rather than falling back.** No credential in the environment is a
`LaneWiringError`, not a quiet `NO_LANES`. A silent fall back reproduces exactly
the failure this document is about, under a heading that says the deployment was
measured.

### 2. `make ci` does not change

Not one target in the gate reaches the builder. That is a rule in this repository
rather than an oversight — *a gate that needs a logged-in human is not a gate* —
and the wired path is exercised by `make experiment-baseline` against the live
account, not by a test. `eval/tests/test_wiring.py` covers the label, the
refusal, and the three deployment names; it opens no connection.

### 3. A number that does not say what it was produced against is refused

The lane configuration is part of the deployment's *name*, so it lands in the
**Deployment**, **Traces from** and **Answered by** lines of the four baselines
without any of those reports being taught about lanes. A recorded
`ExperimentResult` carries it as its own column, `wiring`, so a comparison can
check it rather than parse a sentence.

And `chip_chat.eval.experiment.compare` grows one refusal. `Comparison.stated` is
false when either side did not record its wiring, and such a comparison is not
drawn: the document prints why instead of the tables, and the CLI exits non-zero.

The distinction between that and the module's existing warnings is deliberate and
is the whole of this decision. A different dataset version, a different judge, a
different source — those are **differences**, and the module's own docstring
argues at length for reporting them rather than refusing, because refusing would
make the harness useless exactly when somebody needs it. An unstated lane
configuration is not a difference. It is the absence of the information a reader
would need in order to know whether there is one, and the delta it produces looks
identical whether a prompt got better or a lane came up. On 27 August that
difference was worth more than twenty points of tool selection, and nothing in
the document would have let anybody see it.

Two runs that both say `none` are compared. Two that say `none` and
`account+personalization` are compared **with a warning above the table**, and
that is the most useful subtraction this harness performs. Only silence is
refused.

`UNSTATED` and `none` are therefore different values and the code says so twice:
a recorded result missing the key reads back as unstated rather than defaulting
to `none`. Defaulting would have been almost certainly correct — nothing could
wire a lane before this landed — and would have written a guess into a record as
though it had been measured, which is the habit
`docs/decisions/snowflake-region.md` §"the number that could not be measured"
exists to keep this project out of.

## What the wiring is worth, measured

Both runs are the `shipped` arm over dataset `cilantro-golden-set`
`9ba196eb786c`, 34 rows, `gpt-5-mini`, prompt `v1+1c6f84d1f21f`, judged by
`gpt-5-mini`, fifteen minutes apart on 28 August 2026. Same fingerprint
`0ec39d67a727` on both sides. **The only difference is `--lanes`.** No 429s in
either run — the deployment's ceiling is now 200,000 tokens a minute, and
`scored` is 32 of 34 on both sides where the previous baseline managed 14.

| Metric | `none` | `account+personalization` | Δ |
| --- | ---: | ---: | ---: |
| Task completion | 17.6% | **20.6%** | +2.9 |
| Tool-selection accuracy | 56.2% | **65.6%** | +9.4 |
| Groundedness | 40.0% | **70.0%** | +30.0 |
| Uncited menu claims | unmeasurable | unmeasurable | — |
| Photo F1 | delegated | delegated | — |

And the lane the wiring is actually about:

| Account lane | `none` | `account+personalization` | Δ |
| --- | ---: | ---: | ---: |
| Task completion | 14.3% | **42.9%** | +28.6 |
| Tool selection | 16.7% | **66.7%** | +50.0 |
| `wrong_lane` rows | 5 | 2 | −3 |

Fifty points of tool selection in one lane, and it is not a model that got
better. It is `ask_account_question` existing.

**Two things in that table are not the wiring and should not be read as it.**
The action lane is identical under both wirings by construction — nothing in
`build_lanes` touches it — and its requirements still moved: `T1` −16.7, `T2`
−12.5, `T5` −33.3, and `P1` −33.3 in personalization. Those are two live runs of
a non-deterministic model over six and three cases respectively, which is
run-to-run variance at a sample size where one flipped case is a third of a
requirement. `WIRING.md` reports them as regressions because the requirement
threshold is deliberately zero — a requirement covered by two cases has no
resolution for a threshold to use — and a reader should treat the action-lane
rows of that document as noise with a name rather than as a finding.

The **groundedness** jump is the one to be most careful with. Both runs answer
menu questions from the same hardcoded three-item menu, because the knowledge
lane is wired in neither. What moved is which lane the turns went down and how
many claims they made: over-refusals went 2 → 5 and ungrounded findings 6 → 3.
That is a real difference in behaviour and it is not evidence that retrieval got
better, because there is no retrieval.

See `eval/experiments/BASELINE.md` (wired) beside
`eval/experiments/BASELINE-NO-LANES.md` (unwired), and
`eval/experiments/WIRING.md` for the subtraction. `make experiment-wiring`
regenerates the third from the first two and costs nothing.

For the record, the **previous** baseline — the one that came back
byte-identical — was 14.7% / 42.9% with 13 and 14 rows scored, degraded by 429s
on a 10,000 TPM deployment. Against it, today's wired run is +5.9 and +22.7. Two
things moved between them, the capacity and the wiring, and only the pair of
runs above separates them: **the capacity is worth roughly +13 points of tool
selection and eighteen scoreable rows; the wiring is worth +9.4 on top of that.**

## What this does not fix

**Two of the five lanes are still absent on every deployment there is.**
`build_lanes` wires account and personalization. Knowledge needs one `Retriever`
against the live alias (`cc-e1sr`) and photo needs the upload route and a
production catalogue loader (`cc-mpd`). So `--lanes wired` is
`account+personalization` today and the label will lengthen on its own when those
land — which is the property that keeps this from becoming a second statement of
what is deployed, free to go out of date.

**Three numbers are still unmeasurable and this changes none of them.**

- *Uncited menu claims* (PRD K2) needs something in the request path to build a
  `ResponseEnvelope`. `chip_chat.agent.envelope` exists, is tested and is
  imported by no caller — bead `cc-bap`. Wiring a lane does not mint a citation
  id.
- *`get_recommendations`* is offered once the personalization lane is wired and
  then **declines**, because `MARTS.recommendations` does not exist
  (`chip-znk` / `cc-afo5`). Its row moves from *the tool was not there* to *the
  tool said it had nothing*, which is a better failure and is still a failure.
- *`redeem_points`* is not in `chip_chat.agent.tools.TOOLS` at all, so its row
  cannot be routed under any wiring.

Each of those is reported as **unscored** rather than as a failure. A target
nobody could measure has not been missed; it has not been measured.
