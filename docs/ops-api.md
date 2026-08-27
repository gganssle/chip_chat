# The ops API — the only path that writes

Four write actions, one service, and a rule it enforces rather than trusts.

> **The ops API rejects any draft that has not been marked confirmed by a request
> carrying the visitor's session.**
> — RFC-001 §06, and the second of the PRD's two launch gates.

Confirmation is not a prompt instruction and not a UI convention. It is a
precondition checked in code, on every write, before a database session is
acquired. An agent that decides to skip the confirmation step produces a
rejection and an eval failure, not an order. That sentence is only worth writing
down because it is checkable, and this document is about where each half of it
is checked.

| Action | Takes | Claims | Procedure |
| --- | --- | --- | --- |
| `place_order` | `draft_id` | a `Draft` (#62) | `CHIP_CHAT.ACCOUNTS.place_order` |
| `cancel_order` | `order_id` | a `Confirmation` | `CHIP_CHAT.ACCOUNTS.cancel_order` |
| `redeem_points` | `reward_id` | a `Confirmation` | `CHIP_CHAT.ACCOUNTS.redeem_points` |
| `update_preferences` | `prefs` | a `Confirmation` | `CHIP_CHAT.ACCOUNTS.update_preferences` |

## Three tiers, and what each one is allowed to know

| Tier | Where | Holds |
| --- | --- | --- |
| The record | `api/drafts.py`, `api/confirmations.py` | the confirmation flag, which no model output can reach |
| The service | `api/ops.py` | the gate, the retry key, the `ops.<action>` span |
| The host | `api/functions/` | the only credentials with the Snowflake write role |

The split is the point. The flag lives in the app tier because a flag the model
can reach is not a confirmation (#62). The write role lives in the Functions app
because a credential every tier holds is a credential every tier can misuse. And
the rule that joins them lives in the middle, in ordinary Python, where it can be
tested without an Azure subscription — `api/tests/test_ops.py` runs the whole
gate against a recording double in a tenth of a second, and
`api/tests/test_ops_routes.py` runs the same gate through the host's own routes.

## What a write actually does, in order

1. **Open `ops.<action>`**, carrying the reference id. The span is opened
   *before* the gate, so a refused write is a span rather than a silence.
2. **Claim the record.** Missing, unconfirmed, expired, or somebody else's, and
   the call ends here. Nothing is asked of the database — it does not hold the
   flag and must not be given an opinion about it.
3. **Record the confirmation state.** `confirmed`, or `rejected` for the four
   codes that mean nobody agreed to this, or `unconfirmed` for the two that mean
   consent aged out.
4. **Assemble the arguments** from the *claimed record*, not from the call. The
   draft's own lines, the draft's own restaurant, the card's own point cost.
5. **Call the procedure**, retrying once with the same retry key on a transport
   failure.
6. **Return the procedure's receipt**, verbatim.

Step 4 is the one worth arguing about. It means there is no argument anywhere in
the write path through which a model could alter an order between the card the
visitor read and the row that gets written — not because the service compares
them, but because it never looks at the second one.

## The confirmation record for the other three

`place_order` had a record already: a draft is a priced card with a `confirmed`
flag, and #62 built it. The other three had nothing, and "apply the same
principle to the other three" is issue #63's own wording, so
`api/confirmations.py` is that record with the pricing taken out —
minted by the app, confirmed only by a request carrying the session, scoped to
one visitor, and expiring.

Two of them name a row that already exists, so the record is keyed by the
`order_id` or the `reward_id`. `update_preferences` names nothing, so **the card's
content is its own identifier**: `preferences_reference()` is a digest of exactly
what was shown. Change one field after the visitor confirmed and there is no
confirmation for the result — the same refusal, by a different route.

## Idempotency: the key is the record, never the caller

Every procedure takes a `RETRY_KEY` first and spends it inside its own
transaction with a `MERGE` (see `sql/12_procedures.sql`). The ops API supplies
the draft id or the confirmation id — an identifier the app minted, unique to one
card, and not reusable, because claiming a record retires it.

Two different mechanisms are therefore doing two different jobs, and it is worth
being explicit about which is which:

| Failure | What stops the second write |
| --- | --- |
| A caller places the same draft twice | the draft was retired when it was claimed |
| A connection dies *after* the procedure committed | the retry key: the second attempt replays the stored receipt |

The second is why the key is threaded through at all. A retry that minted a fresh
key would be a second order; a retry that carried the same one is told what the
first attempt did. `api/tests/test_ops.py` drives exactly that case —
`commit_then_fail()` — and asserts two calls and one write.

## When the write path is down

RFC-001 §10 gives this one row and it is specific: *confirmation card renders but
reports that ordering is temporarily unavailable; nothing is half-written. Blast
radius: writes only.*

- `OpsService.available()` is asked **while a card is being composed**, which is
  what makes "the card renders and reports it" possible at all. A card that only
  discovered the outage when Confirm was pressed could not report anything.
- `unavailable_card()` is that card: the same card, plus `ordering_available`
  false and `OPS_UNAVAILABLE_MESSAGE`.
- `OpsUnavailableError` is what a write raises. It is not
  `STOP_STATE_MESSAGE` — the budget's stop is a *designed state* and says
  nothing failed, whereas this one is a failure and says so.
- Every read lane is untouched, because nothing in them goes through this
  service.

## Auditing the gate

Gate 2 is auditable in traces because every write emits `ops.<action>` carrying
`chip_chat.ops.reference_id` and `chip_chat.ops.confirmation_state`. The span is
emitted even when the write is refused — a turn that quietly emitted nothing
would hide the very thing the gate exists to catch.

| State | Means | Span status |
| --- | --- | --- |
| `confirmed` | the record was claimed | ok |
| `rejected` | no such record, or it was never confirmed | error |
| `unconfirmed` | the record expired | error |

`rejected` is the launch-gate violation and the thing an eval counts. Expiry is
deliberately not one: a visitor who went to make a cup of tea is not an agent
that skipped a step, and a dashboard that could not tell them apart would be
useless within a day.

`ops.*` is a child of `tool.*` in the span schema. In the deployed system those
are two processes, so the Functions host rejoins the agent's tool span from the
W3C trace context on the request (`continue_turn(..., parent=SpanName.TOOL)`) and
**refuses the write if it is not there** — a write nobody can find in a trace is
a write this service declines to make, and the app always sends the headers.

## What the host adds

`api/functions/function_app.py` is the edge, and everything it does that
`api/ops.py` does not is about being reachable from outside:

1. **The ops key.** This is the only path that writes, so an unauthenticated
   caller who found the hostname could write as anybody. Compared with
   `hmac.compare_digest`; an unset key refuses every request rather than allowing
   them all.
2. **Trace context**, as above.
3. **The visitor**, on `x-cilantro-session` — the `demo_id` the app resolved from
   the session cookie, server-to-server, never seen by a browser or a model.

It writes no SQL. The statement is `CALL <procedure>(...)`, and which procedure,
in what argument order, with which arguments needing `PARSE_JSON` all come from
`chip_chat.snowflake.procedures` — issue #46's declaration. A procedure that
grows an argument fails a wiring check rather than being called with a value in
the wrong slot.

**A rejection is a 200.** `sql/12_procedures.sql` says it in its own header —
reject, never repair; a rejection is a returned object with `ok` false and a code
— and the edge keeps that contract. An unconfirmed draft is not a malformed
request and not a server fault. It is the answer.

## Verifying the gate at the edge, and not one layer inside it

Issue #63's acceptance criteria ask for the rule to be *tested directly against
the API, bypassing the UI*, and for a while this repository had two halves of
that and not the whole. `api/tests/test_ops.py` drives `OpsService`, which is one
layer inside the edge. `api/tests/test_ops_host.py` reads `function_app.py` as
text, the way `infra/tests` read the Terraform, which establishes its shape and
nothing about what it does.

Between them sat the layer a caller actually meets, and it is where a gate is
lost — not by deleting a rule, but by an edge that never reaches one. A route
that catches the wrong exception, a 500 where a 200 carrying a rejection belongs,
a service resolved before the caller was authenticated: every one of those leaves
the service tests green.

`api/tests/test_ops_routes.py` closes it by calling the route functions —
`place_order(request)` — with a real `OpsService` behind them and a recording
backend where Snowflake would be. `api/tests/azure_functions_stub.py` is what
makes that importable without putting the Functions SDK into the workspace
lockfile, and it is deliberately no more forgiving than the real thing: headers
are case-insensitive, a body that is not JSON raises `ValueError`, and a response
body is bytes. A stub that relaxed any of those would turn a green test into a
claim about the stub.

What that file establishes, by driving the edge rather than by reading it:

| Criterion | How it is observed |
| --- | --- |
| An unconfirmed `draft_id` is rejected | 200, `DRAFT_NOT_CONFIRMED`, and `backend.calls == []` — the database was never asked |
| A confirmed draft from another session is rejected | same draft id, different `x-cilantro-session`, `DRAFT_NOT_FOUND`, no write |
| The same key writes once | `commit_then_fail()` → two calls, one write, `replayed` true; and a second POST of the same draft finds it retired |
| The app being down produces the specified message | 503, `OPS_UNAVAILABLE_MESSAGE`, `ordering_available` false — including with no service installed at all, which is the state the deployed host is in today |
| Every write emits `ops.<action>` with its confirmation state | read off the span, along with the trace id from the inbound `traceparent`, so the rejoin is asserted rather than assumed |

The edge's own three preconditions are driven too, in order: an unauthenticated
caller learns nothing about the body or the trace, because the key is checked
first.

What is still not covered, so that nobody reads more into it: the Functions
worker's dispatch and its `FUNCTION` auth level are Azure's code, and the
Snowflake driver is exercised nowhere in this workspace — the same argument
`chip_chat.snowflake.snow` makes about shelling out to the CLI.

## What is not wired yet, and why that is the honest state

**The catalogue.** The draft store prices against a built catalogue and the
production loader is #66's, exactly as `chip_chat.api.app.build_service` records
for its photo lane. Until it exists the host answers 503 with the message §10
specifies, which is the behaviour for an ops API that is not there.

**The topology.** A draft minted in the chat app's process lives in that
process's memory (#62), so an ops service in a *different* process cannot see it.
V0 therefore runs the ops service behind the app, and `api/functions/` is the
deployment shape it moves into when both ledgers move behind a shared store —
the same honest limitation `BudgetLedger` carries, with the same one obvious
place for a shared implementation to land.

**The agent.** `chip_chat.agent.orders` still holds the week-one order desk
against three hardcoded items, and says in its own docstring that it goes away
when the ops API lands. Moving the agent's four write tools onto this service is
its own change, and this one deliberately does not make it — the gate is worth
landing and testing before the switchover, not during it.

## Where every rule came from

| Rule | Source |
| --- | --- |
| Confirmation is enforced here, not in the prompt | RFC-001 §06 |
| The ops API is the only path that writes | RFC-001 §03 |
| No tool or procedure takes a visitor identifier | RFC-001 §05, `docs/action-surface.md` §7 |
| What each action validates and rejects | `docs/action-surface.md` §7.1–7.4 |
| Writes go through stored procedures | `snowflake/sql/12_procedures.sql`, `13_cancel_order.sql` (#46) |
| The confirmation flag lives in the app tier | `api/drafts.py` (#62) |
| `ops.<action>` carries draft id and confirmation state | RFC-001 §09 |
| Ops API unavailable → the card says so, nothing half-written | RFC-001 §10 |
