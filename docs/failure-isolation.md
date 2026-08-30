# Lane-level failure isolation — a lane may fail, the conversation may not

> **A lane may fail, the conversation may not.**
> — RFC-001 §10, and the governing principle of the whole design.

Cilantro should always be able to say what it cannot currently do and remain
useful for everything else. That is a claim about seven specific dependencies
going away, one at a time, and it is only worth writing down because each one is
checkable. This document is where each is checked, and where the surface that
tells you *which* one went away lives.

## The table, and what verifies each row

| Failure | Behaviour | Blast radius | Verified by |
| --- | --- | --- | --- |
| AI Search unavailable | knowledge lane declines and says why | knowledge only | `test_ai_search_down_*` |
| Snowflake unavailable | account, personalization, action decline; menu questions still work | three lanes | `test_snowflake_down_*` |
| Cortex Analyst timeout or low confidence | *"I can't answer that reliably"*, **never a fallback query** | one question | `test_a_low_confidence_answer_*`, `test_a_declined_question_ran_no_statement_at_all` |
| Vision model unavailable | ask the visitor to describe the meal in words | vision only | `test_vision_down_*` |
| Ops API unavailable | the card renders and reports ordering is unavailable; nothing half-written | writes only | `test_the_confirmation_card_still_renders_*`, `test_nothing_is_half_written_*` |
| Databricks job failed | serve stale marts **with their `derived_at`**; alert | personalization freshness | `test_a_stale_habit_is_served_with_its_derived_at`, `test_stale_marts_are_a_nightly_job_down_and_not_a_lane_down` |
| A serving mart was never published | withdraw that tool; the lane stays up and its other tools answer | one tool | `agent/tests/test_withheld_tools.py` |
| Daily budget exhausted | friendly stop state on entry and mid-conversation; **no model calls** | everything, gracefully | `test_an_exhausted_budget_*` |

All of them live in `api/tests/test_failure_isolation.py`, and each one breaks
the dependency for real rather than asserting on a mock's return value: the
search service raises, the pool refuses to check out, Analyst answers with
suggestions instead of SQL, the vision deployment is down, the write backend is
taken down, the marts are computed nine days ago, the daily ceiling is smaller
than one turn's reservation.

## Why that file exists at all, given the lanes already have tests

Each lane's own decline is asserted where the lane lives — `search/tests/`,
`snowflake/tests/`, `agent/tests/test_photo_tool.py`, `api/tests/test_ops.py`,
`api/tests/test_spend_gate.py` — and every one of those is a good test that does
not answer the question this ticket is about.

The second half of every row is the **blast radius**, and no test that exercises
one lane can see it. A lane can decline perfectly and still take the turn down
with it: an exception that escapes, a shared client that was left in a bad state,
a tool list that shrank. So every section of `test_failure_isolation.py` breaks
one dependency and then asserts, under that same break, that every other lane
still answers. That assertion is factored into one helper precisely so it cannot
be quietly omitted from the row where it would have failed.

One more thing that file asserts and none of the others could: **a wired lane
that is failing keeps its tool.** Withdrawal is for a lane a deployment does not
*have* (`chip_chat.agent.lanes`). A lane that is present and broken must still be
offered, because the model has to be able to call it, receive the decline, and
tell the visitor which lane is out. A tool list that shrank on an outage would
leave the model unable to name what it cannot do, which is the one thing this
whole design asks of it.

## The two rows with a criterion of their own

### Cortex Analyst must never reach for a hand-written query

Issue #65 asks for this **asserted in code, not just in behaviour**, and the
distinction is real. Observing "the refusal came back" is equally consistent with
a fallback that happened not to fire today. Three assertions, at three depths:

1. `test_a_declined_question_ran_no_statement_at_all` reads the statement log off
   the connection after a declined question. It is empty. The lane reached the
   database's door and ran nothing.
2. `test_the_refusal_carries_no_sql_for_anything_to_reach_for` checks that the
   refusal handed back to the model carries no `sql` key. A statement passed
   upward is a statement something will eventually run — `_refused()` in
   `chip_chat.snowflake.lane` never builds one.
3. `test_the_account_lane_has_no_hand_written_question_to_fall_back_to` reads
   `chip_chat.snowflake.reads` and asserts its fixed statements are exactly four:
   a balance, a reward list, and the two marts. None of them answers a free-text
   question. A general-purpose statement appearing there fails this test, which
   is the point of writing it as a set rather than as a subset.

`chip_chat.snowflake.analyst` is the judgement itself, and the ladder it applies
— verified, generated, suggested, unavailable — is what "low confidence" means
here. Cortex Analyst returns no probability; what it returns is which path
produced the answer.

### Stale marts must never present as fresh

The failure mode is not a mart that is missing. It is a mart that answers
beautifully and says nothing about its age, and a demo that shows last week's
recommendations as this week's. So both halves are asserted: `derived_at` is
present on the result, and `stale` says so — and separately, that a *fresh* mart
is not reported stale, because a flag that always fires reports nothing.

The health surface reports this as its own condition rather than as an outage.
The lane is up: it answers, and the visitor is served. What is down is the
nightly publish, and restarting the app would not fix it. `HealthReport.down` and
`HealthReport.stale` are therefore two different lists, and a stale mart appears
only in the second.

## Which lane is down: `chip_chat.agent.health`

Declining politely has one cost, and it is the cost this module exists to pay
back: it makes an outage quiet. Cilantro keeps answering menu questions while the
account lane is dead, the visitor is told something reasonable, and the sentence
that reaches whoever is running the stand is *"the demo is broken"* — which is
both wrong and useless.

```
--    knowledge        not wired on this deployment; its tools are not offered to the model
DOWN  account          ACCOUNT_LANE_UNAVAILABLE: the pool did not produce a bound connection: RuntimeError: the warehouse did not answer
ok    personalization  marts fresh, derived_at 2026-08-27T04:11:00+00:00; get_recommendations withheld, nothing to restart
--    photo            not wired on this deployment; its tools are not offered to the model
ok    action

Down: account. Every other lane answers.
```

Note the personalization line, because it is a third state and not a shade of
the first two. `get_recommendations` reads `CHIP_CHAT.MARTS.recommendations`,
which RFC-001 §04 does not fix — the RFC names four serving marts and this would
be a fifth — so the table has never existed on the account. The lane itself is
up: `get_usual_order` reads a mart that does exist and answers normally. Only the
one tool is withdrawn, and it is withdrawn rather than offered, because a tool
definition the model can see and nothing can answer is worse than an absent one.

That distinction is what "nothing to restart" is there to say. `DOWN` is a
dependency that should be answering and is not, and it is the line that should
send somebody to the runbook. A withheld tool is a mart that was never published;
no restart, no failover and no amount of waiting will change it, and reporting it
as an outage would spend an operator's attention on a schema decision.
`docs/decisions/withheld-tools.md` is that argument in full.

Four properties worth stating, because each is a decision:

**It asks the lanes, not the services.** There is no ping to AI Search and no
`SELECT 1`. The question worth answering during a demo is not "is the search
service up" but "will `search_menu_knowledge` answer", and those differ in every
way that matters — an alias pointing at a deleted index, an expired token, a
semantic view that will not compile. Each lane already returns a structured
decline that knows the difference, and the probe reads the same one a turn would.

**Four states, not two.** `not_wired` is not an outage: a deployment that never
had a photo lane is working exactly as configured, and reporting it red would
train whoever reads this to ignore red. `unprobed` is the photo lane specifically
— describing an image is a vision completion, and spending one to find out
whether the lane is up would make the health surface the most expensive thing on
the deployment.

**Each lane is printed with the tools it answers**, so "the account lane is down"
arrives with "so `ask_account_question` and `get_points_balance` will decline",
which is the half an operator can act on.

**The exit code is about lanes, not freshness.** `python -m chip_chat.agent`
exits non-zero when a wired lane is not answering and zero when a mart is merely
stale — a readiness probe that failed on staleness would take a working
deployment out of rotation. Staleness is printed loudly either way.

### What it can see today, which is the honest part

Lanes are assembled by the request path and handed to the agent as a value;
nothing in `agent/` builds a retriever, a pool or a vision client, and
`chip_chat.agent.lanes` carries the argument for why. So run bare, the report
describes the deployment's configured wiring — which today is the week-one slice:
no lane wired, every tool that needs one withdrawn, and the three hardcoded reads
still answering. That is not placeholder output. It is the correct answer to "is
the account lane down", and the answer is that there is no account lane on this
deployment yet (`chip_chat.api.app.build_service` names the seam and the beads
behind each one).

The HTTP form is `GET /healthz/lanes` and it is not mounted. Mounting it is four
lines in `create_app`, where the assembled `Lanes` already lives — the seam is
deliberately this way round, so that `chip_chat.agent.health` has no opinion
about HTTP and the request path has everything it needs.

## Where the timeouts are

Each lane owns its own deadline, because each has a different one worth having:

| Lane | Deadline | Where |
| --- | --- | --- |
| Knowledge | the search client's own timeout and retry | `chip_chat.search.client` |
| Account | `Thresholds.timeout_seconds`, default 15s | `chip_chat.snowflake.analyst` |
| Account, personalization | the warehouse's 60s statement timeout | `snowflake/sql/01_warehouses.sql` |
| Vision | the describe call's own | `chip_chat.vision.describe` |
| Action | two attempts, then the path is called down | `chip_chat.api.ops` |
| Everything | the spend ceiling, inline in the request path | `chip_chat.api.guard` |

The account lane's fifteen seconds is chosen from two numbers rather than from a
distribution, and `chip_chat.snowflake.analyst` shows the working: the serving
warehouse kills any statement at sixty, and a turn that has not answered inside a
minute has already failed as a conversation.

## Where every rule came from

| Rule | Source |
| --- | --- |
| A lane may fail, the conversation may not | RFC-001 §10, §02 |
| The seven rows and their blast radii | RFC-001 §10's table |
| Never a fallback hand-written query | RFC-001 §10, PRD A4 |
| Stale marts carry `derived_at` | RFC-001 §10, `docs/nightly-publish.md` |
| The stop state is designed, not an error | PRD S4 |
| A tool whose lane is down returns a decline, not an exception | RFC-001 §06, issue #61 |
| Per-lane health surfaced somewhere operable | issue #65 scope |
