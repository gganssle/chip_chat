# Decision: `get_recommendations` is withdrawn by name, and the fifth mart is still not published

**Bead:** `chip-znk` · **Decided:** 28 August 2026 · **Measured:** 27 August 2026, on the deployed app
**Does not resolve:** `cc-afo5` — whether `CHIP_CHAT.MARTS.recommendations` should exist
**Changes:** `agent/lanes.py` (`Lanes.withheld`), `api/app.py` (`WITHHELD_TOOLS`), `agent/health.py`

---

## The state this decision is about

`cc-lpy4` wired `Lanes.personalization` onto the deployment. That was right and
it is not being undone: the same wiring is what moved `get_usual_order` off the
hardcoded account fixture, which was half of `docs/public-demo.md` §9.

It also offered `get_recommendations` to the model on every turn, and every call
of it came back, measured on 27 August 2026:

```
{'declined': 'PERSONALIZATION_LANE_UNAVAILABLE',
 'say': "I can't reach the personalization marts right now...",
 'reason': "ProgrammingError: 002003 (42S02): SQL compilation error:
            Object 'CHIP_CHAT.MARTS.RECOMMENDATIONS' does not exist or not
            authorized."}
```

`CHIP_CHAT.MARTS` holds `ITEM_AFFINITY`, `CUSTOMER_360`, `SPEND_SUMMARY` and
`USUAL_ORDER`. There is no fifth. `chip_chat.snowflake.reads.RECOMMENDATIONS_MART`
already spells the table name once and, in the same docstring, the reason
nothing publishes it: RFC-001 §04 fixes four serving marts, this would be a
fifth, and creating it is a schema decision rather than something a tool ticket
takes on the side.

So the lane was up, one of its two tools could not be answered, and the trace
showed a red tool span with a refusal in it once per turn — which reads as a
personalization outage and was not one.

## The decision

**Give `Lanes` a way to withhold a single tool, withhold
`get_recommendations` on this deployment, and report the withdrawal on the
health surface and the start-up log. Do not publish a mart.**

`Lanes.withheld` is a `frozenset[ToolName]`; `offered_tools` filters the whole
list through it; `Lanes.withdrawn()` names what was taken from a lane that is
wired. `chip_chat.api.app.WITHHELD_TOOLS` holds the one name, because *which
tables exist on this Snowflake account* is a fact about the deployment and not
about how a lane works — `agent/` has no business knowing it.

The day `cc-afo5` is decided and the mart is published, `WITHHELD_TOOLS` goes
back to empty and nothing else changes.

## Why this and not the other way out

The bead named two and they are not equivalent.

**Publishing the mart** answers the question properly: the tool would work.
But #37 batch-scores recommendations into `gold_synthetic.recommendations` and
nothing publishes them into the serving schema *on purpose*. RFC-001 §04 fixes
four serving marts; a fifth changes the data model, the nightly publish job
(#39), the schema tests and the freshness surface. That is `cc-afo5`, it is a
decision somebody should take while looking at the schema, and taking it inside
a bug ticket would mean making a schema decision in a file nobody reviewing the
schema reads. Declined here, deliberately, and left open.

**Withdrawing the tool** is smaller and is already the rule this repository
argues for. `agent/lanes.py` has carried the sentence since #64:

> A tool definition the model can see and nothing can answer is worse than an
> absent one: the model will call it, the call will fail, and the trace will
> show a tool span with a refusal in it that reads as a lane outage rather than
> as a deployment nobody finished.

That is exactly what was happening. The fix is to apply the existing rule one
name at a time instead of one lane at a time.

## Why the unit of withdrawal had to change

Until now a lane was all-or-nothing, and for three of the four lanes that is
right: a lane is one service and a service is up or it is not. Personalization
is the exception and not a temporary one — its two tools read two different
tables through one connection, so a table that does not exist takes down one
tool and leaves the other answering.

Withholding the *lane* to withhold the tool was considered and rejected in one
line: it would have taken `get_usual_order` back to the hardcoded fixture and
undone the thing `cc-lpy4` was for.

## Why it is not silent

This is the half that could have gone wrong. Offered-and-always-declining is at
least loud — a red tool span, once per turn, in a place somebody eventually
looks. A tool that simply stopped existing, with nothing anywhere saying so,
would be the same defect with the evidence removed: an operator asking why
Cilantro never recommends anything would find a tool list with no gap in it and
a personalization lane reporting `up`.

So the withdrawal is reported in three places, and in none of them is it an
error:

- **`GET /healthz/lanes`** — `LaneHealth.withheld` beside the lane, and a
  report-level `withheld` list. The lane stays `UP`.
- **`HealthReport.render()`** — *"Withheld from the model: get_recommendations.
  The lane is up and the tool is not offered, deliberately — nothing to
  restart."* The last four words are the point; somebody reading a health
  report under pressure must not go and restart a warehouse.
- **The start-up log** — a second line beside the existing `lanes wired on this
  deployment`, because *"personalization: true"* and *"and yet
  get_recommendations is not offered"* are two different facts and the second
  one is the one nobody thinks to ask about.

`Lanes.describe()` was deliberately **not** extended. `chip_chat.eval.wiring`
builds itself with `cls(**lanes.describe())`, so that mapping is a structural
contract in another package rather than a log line, and a fifth key would be a
`TypeError` somewhere else.

## What the model is told

Nothing, and that is the existing rule rather than a new one. `runtime_context`
names the registered tool list, `offered_tools` produces that list, and the
withheld name is simply not on it. A model told *"there is a recommendations
tool but you may not use it"* would ask about it; a model that never sees the
name answers *"what should I try"* out of the lanes it does have, which is the
behaviour that was wanted all along.

A withheld tool called anyway — a model can emit any string it likes — gets
`TOOL_NOT_IMPLEMENTED` from `dispatch`, which is a refusal it can read, and no
query reaches a table that is not there.

## What is not measured

**How often the model reached for it.** The bead records that the tool declined
on every turn it was called on; it does not record what fraction of turns called
it. So the saving from this change — turns that no longer spend a round trip on
a refusal — is not quantified and should not be quoted.

**The quality of the recommendations nobody is serving.** #37's batch scoring
runs and writes `gold_synthetic.recommendations`. Whether those rows are any
good is a question for `cc-afo5` and the eval suite, and this decision says
nothing about it either way.

**Whether a visitor notices.** The tool has never worked on this deployment, so
there is no before-and-after to compare. What changes is a trace and a tool
list, not an answer the visitor was previously getting.

## Sources

RFC-001 §04 (the four serving marts), §06 (the eleven tools), §10 (blast
radius). PRD A4. Beads `chip-znk`, `cc-afo5`, `cc-lpy4`. Issues
[#37](https://github.com/gganssle/chip_chat/issues/37),
[#38](https://github.com/gganssle/chip_chat/issues/38),
[#64](https://github.com/gganssle/chip_chat/issues/64),
[#65](https://github.com/gganssle/chip_chat/issues/65).
`docs/public-demo.md` §9, `docs/failure-isolation.md`.
