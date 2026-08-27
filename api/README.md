# `api/` — the request path, and the cap it cannot be built without

This package holds the thing that stands between a public URL with no login and
an invoice. RFC-001 §11 and the system design are both explicit that it ships
**before the link is shared**, not when a Phase 10 hardening checklist is finally
reached.

The cap arrived first (`cc-fv1`) and the service it sits inside arrived second
(`cc-n6j`). For one commit in between, this package held a guard that was
correct, tested, and **called by nothing** — which stops nobody spending
anything. `app.py` and `turns.py` are that gap closed.

## The distinction this rests on, which is easy to blur

Azure budget alerts notify *after* the fact. Arize reports what *was* spent.
Neither prevents anything.

The check in `src/chip_chat/api/guard.py` runs **inline and synchronously, in the
request path, before the agent is invoked**, and its refusal is the reason no
tokens are bought. If a future change to this package could fairly be described
as observability, it is the wrong change.

## Four layers

| Layer | Where | Refusal reason |
| --- | --- | --- |
| Circuit breaker | `killswitch.py` | `kill_switch` |
| Per-source-address rate limit | `ratelimit.py` | `source_rate_limit` |
| Global daily token ceiling | `ledger.py` | `daily_ceiling` |
| Per-session turn and token caps | `ledger.py` | `session_turn_cap`, `session_token_cap` |

They are evaluated in that order. The reasons are a schema — they land on
`chip_chat.guard.reason` and Phase 9's evaluations group on them — so they are
stable tokens rather than sentences.

## Uploads get their own ceiling underneath

Added by [#80](https://github.com/gganssle/chip_chat/issues/80). The layers above
are sized for typing — twenty requests a minute, because a person cannot type
faster. An upload is not typing: one accepted photograph is a Content Safety
call, a blob write with a forty-eight hour retention obligation, and a vision
completion. Twenty of *those* a minute is not a fast typist, it is an invoice.
So `uploads.py` counts uploads separately, and counts them twice:

| Ceiling | Default | What it stops |
| --- | --- | --- |
| Per session | 5 per 5 min | One conversation uploading in a loop. |
| Per source address | 10 per 5 min | The same loop with a fresh session per upload, which costs an attacker nothing. |

Both are sliding windows, both refuse with `upload_rate_limit` and the ordinary
stop state, and **neither refusal says which one it was** — told "your session is
out", an uploader mints a session. Which ceiling fired is on
`guard.budget_check`, in `metadata`, where the operator reads it. It is in
metadata rather than on `chip_chat.budget.tokens_used` because uploads are
counted in uploads: putting them on the token attributes would make every budget
dashboard read a photograph as spend.

Uploads count against the **budget** as well as against the rate limits, and the
two are different defences — the limit bounds how often, the budget bounds how
much. `TurnBudget.record_upload()` charges `CHIP_CHAT_UPLOAD_TOKEN_CHARGE` at
acceptance, because the vision call an accepted photograph implies is spend that
has already been committed to. It is the ledger's reserve-then-settle argument
one level up: `record_usage` replaces the estimate with what the model really
billed.

## Reserve, then settle

The check has to decide *before* the model answers, and what the turn will cost
is not known until after it has. So a turn reserves a pessimistic
`turn_token_reservation` against the ceiling up front and replaces it with the
real number afterwards.

This is the whole reason the concurrent case is safe. Read the counter, decide,
call the model, add what it cost — that shape passes every sequential test ever
written and still lets twenty simultaneous visitors a few tokens below the
ceiling all read a number under the limit and all proceed.
`api/tests/test_concurrency.py` starts its threads on a barrier for exactly this
reason.

## Not callable — unconstructable-without

"Is there a caller?" is the wrong question, because a caller can be forgotten by
the next route somebody adds. The question `turns.py` answers is **whether a
request path exists that can skip the check**, and the answer is no, for three
structural reasons:

| Fact | Where |
| --- | --- |
| A `FundedTurn` cannot exist for a turn the guard refused — its constructor raises | `turns.py` |
| A `SpendGate` cannot be built without a `SpendGuard` — required positional | `turns.py` |
| Nothing else in the package exposes a model; only `FundedTurn.run` reaches one | `turns.py`, `app.py` |

And a fourth that removes the other way to get this wrong: `FundedTurn.run`
settles the turn's real token cost itself, so the ceiling counts tokens rather
than turns whether or not the caller remembers.

`api/tests/test_spend_gate.py` is the half a future contributor feels. Those
tests fail when the *invariant* breaks rather than when the output changes:
deleting the constructor check, adding a public accessor that returns a model,
or adding a fifth route to the app each fail a test while leaving every happy
path green.

## Using it

```python
gate = SpendGate(
    SpendGuard(
        SpendLimits.from_env(),
        kill_switch=any_of(EnvironmentKillSwitch(), FileKillSwitch("/mnt/ops/stop")),
    ),
    lambda: AzureChatModel(FoundryConfig.from_env()),
)

# On entry, before a session exists. Emits no span; there is no turn yet.
if (stop := gate.entry_state()) is not None:
    return stop_state_page(stop.message)

with chat_turn(session_id=sid, turn_index=n, message=text) as turn:
    with guard.turn(session_id=sid, source_address=ip) as budget:
        if not budget.allowed:
            turn.record_output(budget.message)
            return stop_state_response(budget.message)  # 200, not an error
        if photo_attached:
            # Before the body is read: a refusal here has to cost the socket
            # and nothing else, which it cannot do once the bytes have arrived.
            if (stop := guard.upload(session_id=sid, source_address=ip)) is not None:
                return stop_state_response(stop.message)
            photo = await intake.accept_stream(upload_file)
            budget.record_upload()
        reply = agent.run(text)
        budget.record_usage(prompt_tokens=p, completion_tokens=c)
    with gate.turn(session_id=sid, source_address=ip) as funded:
        if isinstance(funded, Stop):
            return stop_state_response(funded.message)  # 200, not an error
        result = funded.run(conversation, text)  # settles its own tokens
```

`guard.budget_check` is a child of `chat.turn`, so `SpendGate.turn` must be
called inside one.

### The stop state is a designed state

> Cilantro's had a busy day — come back tomorrow

PRD requirement S4. One definition, `STOP_STATE_MESSAGE`, served on entry and
mid-conversation alike. Never a 4xx or 5xx, never the word "quota", and never an
apology for a failure — because nothing failed. The cap worked.

## The second thing in this package: the ops API

The cap is about money. The ops API is about consent, and it is the second
launch gate: **zero account writes executed without explicit confirmation.**

| File | Holds |
| --- | --- |
| `drafts.py` | the confirmation flag for orders (#62) |
| `confirmations.py` | the same record for the three actions that have no draft |
| `ops.py` | the gate, the retry key, and the `ops.<action>` span |
| `../functions/` | the Azure Functions host, and the only Snowflake write role |

The rule, in one sentence: **a write claims something the visitor was shown and
confirmed, and sends the procedure what was on that record rather than what
arrived with the call.** So there is no argument anywhere in the write path
through which a model could alter an order between the card the visitor read and
the row that gets written — not because the service compares them, but because it
never looks at the second one.

```python
service = OpsService(backend, drafts, confirmations)

# While the card is being composed — which is what lets the card render *and*
# report that ordering is off, per RFC-001 §10.
card = drafts.card(draft)
if not service.available():
    card = unavailable_card(card)

# The visitor presses Confirm. This is a request carrying the session cookie,
# and it is the only thing in the system that can set the flag.
drafts.confirm(demo_id, draft_id)

# The write. No method on OpsSession takes a visitor identifier.
receipt = service.session(demo_id).place_order(draft_id)
```

`docs/ops-api.md` has the whole argument: which tier is allowed to know what, why
the retry key is the record's own id, and what a trace has to carry for the gate
to be auditable in it.

### Not callable — unclaimable-without

The same shape the spend cap uses, for the same reason:

| Fact | Where |
| --- | --- |
| An `OpsService` cannot be built without both ledgers — required positional | `ops.py` |
| No write method takes an identifier; `test_ops.py` holds all four to `IDENTITY_VOCABULARY` | `ops.py` |
| The procedure name, argument order and arity come from #46's declaration | `ops.py`, `../functions/function_app.py` |
| A claimed record is retired, so one card is at most one write | `drafts.py`, `confirmations.py` |

## Configuration

Every ceiling is an environment variable, all optional, all defaulted small:

| Variable | Default |
| --- | --- |
| `CHIP_CHAT_DAILY_TOKEN_CEILING` | `2000000` |
| `CHIP_CHAT_SESSION_TURN_CAP` | `40` |
| `CHIP_CHAT_SESSION_TOKEN_CAP` | `120000` |
| `CHIP_CHAT_SOURCE_REQUESTS_PER_WINDOW` | `20` |
| `CHIP_CHAT_SOURCE_WINDOW_SECONDS` | `60` |
| `CHIP_CHAT_SESSION_UPLOADS_PER_WINDOW` | `5` |
| `CHIP_CHAT_SOURCE_UPLOADS_PER_WINDOW` | `10` |
| `CHIP_CHAT_UPLOAD_WINDOW_SECONDS` | `300` |
| `CHIP_CHAT_UPLOAD_TOKEN_CHARGE` | `1500` |
| `CHIP_CHAT_TURN_TOKEN_RESERVATION` | `8000` |
| `CHIP_CHAT_BUDGET_RESET_TIMEZONE` | `UTC` |
| `CHIP_CHAT_KILL_SWITCH` | unset |

The ops API's own settings live on the Functions app rather than on the
container, because that is where the write role is: `CHIP_CHAT_OPS_KEY` (the
shared secret the chat app presents, and whose absence refuses every write),
`SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER` and `SNOWFLAKE_PRIVATE_KEY` as a Key Vault
reference, and optional `SNOWFLAKE_WRITE_ROLE`, `SNOWFLAKE_WAREHOUSE`,
`SNOWFLAKE_DATABASE` and `SNOWFLAKE_SCHEMA`.

`CHIP_CHAT_BUDGET_RESET_TIMEZONE` is named rather than assumed because "daily"
means nothing without it, and a ceiling that rolls over at five in the afternoon
is the kind of subtly wrong that is first noticed on an invoice.

Set `CHIP_CHAT_TURN_TOKEN_RESERVATION` at or above the worst turn the agent can
produce. Too low and concurrent turns can collectively overshoot the ceiling,
which is the one thing this package exists to prevent.

## Runbook — stopping the app

The requirement is a minute from a phone, without a deploy. Any one of these is
enough; `any_of` combines them.

1. **Application setting.** Set `CHIP_CHAT_KILL_SWITCH=on` in the portal. The
   container restarts and comes back already stopped, because the value is read
   at the moment of the check and never at import.
2. **A file.** `touch /mnt/ops/stop` on the mounted share. No restart at all —
   the next check reads it. Write `off` into the file to disarm it without
   deleting it.
3. **The ops endpoint.** `ManualKillSwitch.throw()`, for the in-process switch.
   Definitive and instant, but a restart clears it, so pair it with one of the
   above.

Anything that is not recognisably "off" (`""`, `0`, `false`, `no`, `off`, `run`)
counts as thrown. An emergency stop nobody can parse should stop the spending,
not carry on.

An **unreadable** path, by contrast, is treated as *not* thrown. That is the
uncomfortable choice and it is deliberate: a typo in a path would otherwise take
the demo down permanently and silently, and the money is still bounded by the
daily ceiling either way.

`CachedKillSwitch` memoises any of them for a few seconds, so "cheap enough to
check on every request" and "responds within seconds" are both true rather than
one being traded for the other.

## The draft store — the other thing this package owns

`drafts.py` holds the order drafts, and it is here rather than in `agent/` for
one reason: **the confirmation rule is only enforceable if the flag it reads
lives somewhere the model cannot write to.** RFC-001 §06 rejects any draft not
marked confirmed by a request carrying the visitor's session, so the flag is set
by `DraftStore.confirm`, which is reached from the request handler — the
`confirm_draft_id` field that arrives beside the session cookie — and by no tool,
ever. An agent that decides to skip the confirmation step therefore produces a
rejected write and an eval failure rather than an order.

| Method | Who calls it | What it is for |
| --- | --- | --- |
| `propose` | `propose_order`, i.e. the model | Mint a priced draft. Writes nothing. |
| `revise` | `propose_order` again | The editable card of PRD T3: a new draft, unconfirmed, and the one it replaces is retired. |
| `confirm` | the request handler, on the visitor's Confirm | **The launch gate.** No tool reaches it. |
| `claim` | the ops API, before it writes | §7.1 rule 11 in one place: confirmed, unexpired, and removed as it is handed over, so one draft is at most one order. |

Four properties, each a test in `api/tests/test_drafts.py`:

- **Only real catalogue rows are mintable.** Every `item_id`, every
  `(item, modifier)` pairing and every portion word is looked up in
  `menu_catalog` at proposal time. A draft naming a SKU that does not exist is
  not rejected later — it cannot be minted.
- **Prices are the catalogue's.** Per restaurant, per order type, modifier
  deltas included, with the `harvested_at` of the rows used on the card. Money
  is a column on a restaurant; see `docs/decisions/menu-pricing.md`.
- **A draft belongs to one visitor.** It is bound to the `demo_id` the app
  resolved from the session. A well-formed id presented with the wrong one is a
  `DRAFT_NOT_FOUND` — the same answer as an id that never existed.
- **Drafts expire.** A quote from a harvest ages; fifteen minutes by default,
  and the number is `[INVENTED]` as §7.1 rule 11 says.

Rejections are typed and use `docs/action-surface.md` §7.1's own spellings, so
Phase 9's evaluations group on one vocabulary. Three of that section's rules
need columns the catalogue does not carry — the per-item `max_quantity` and the
aggregate-cap weights — so rule 4 is flattened to one entree or five of anything
else and rule 9 is not enforced here at all. That is `cc-of1`.

## The session store — where the identity comes from

`visitors.py` is [#66](https://github.com/gganssle/chip_chat/issues/66), and it
is the first clause of the sentence the pool below implements the second half
of. RFC-001 §05:

> Identity originates in the app's **server-side session store**, is applied to
> the Snowflake connection as a session variable, and is enforced by row access
> policies on every visitor-scoped table.

Until this module existed the store was a `VisitorSessions` protocol with a
placeholder behind it. Now it is three things that only make sense together.

| Piece | What it is |
| --- | --- |
| `VisitorSessionStore` | The bindings. The one object that answers `demo_id_for`, and therefore the only thing the pool will take an identity from. |
| `SnowflakeRoster` | `persona_fixtures`, read on a connection that has bound nobody — #43's `entry_roster` policy exists for this read and no other. |
| `VisitorDesk` | Where a browser becomes a synthetic customer. `admit(session_id, display_name=...)` and nothing else. |

### An empty account is how this demo dies

That is the ticket's own sentence, and it is the reason `SnowflakeRoster` does
not *prefer* populated fixtures — it **refuses to offer** an unpopulated one. A
row without order history, without a home store or without a points balance is
dropped at the roster, so there is no code path that assigns one.

The consequence is deliberate and worth stating: a deployment whose synthetic
population has not been loaded has an **empty roster**, and `admit` returns
`None`. The app then serves the demo unbound, which is exactly what it did
before this module existed. That is a declared state with a `WARNING` behind it,
not a visitor discovering a blank account.

### Two concurrent sessions get different personas

The second acceptance criterion, and it is a property of the *choice* rather
than of the roster's size. `VisitorDesk._choose` looks at what the live sessions
are already holding and picks in three tiers: an archetype nobody holds, then a
customer nobody holds, then — once the roster really is exhausted — the customer
whose holder has been idle longest. `api/tests/test_visitors.py` starts ten
admissions on a barrier, because a desk that collides only under contention
passes every sequential test ever written.

### Survives a restart, or degrades in a way that is decided

The fourth criterion offers a choice and both halves are here.
`CHIP_CHAT_SESSION_JOURNAL` names an append-only file on the mounted share;
`FileJournal` replays it at start-up, compacts it while it does, and ages
entries out after thirty days so its size tracks live visitors rather than
uptime. Unset, `journal_from_env` returns `NoJournal` **and logs a warning** — a
decision, announced at assembly. A path that cannot be written does the same
thing at `ERROR` rather than raising, because a file share that failed to mount
should not also take the demo down.

The journal keeps the binding and not the account: a session id, its `demo_id`,
the invented first name and the Foundry thread pointer. The roster row is
Snowflake's to state, and a journalled copy would let a restart serve an account
summary the nightly reset has since changed underneath it.

### No endpoint accepts a `demo_id`

The third criterion is about *what an endpoint accepts*, so it is enforced in
the schema rather than in a handler. Every request model in `app.py` sets
`extra="forbid"`, so a body carrying `demo_id` is a 422 rather than a field
somebody has to prove nothing reads. `api/tests/test_identity_binding.py` holds
every request model, every request helper and `VisitorDesk.admit` to
`IDENTITY_VOCABULARY` — the same absence the tool surface, the stored procedures
and the ops API are already held to, now at the tier where a request arrives.

`POST /api/entry` returns the assigned account and **does not return its
`demo_id`**. There is nothing in that payload a browser could replay into a
request to claim an account.

### What is not here, and why

`demo_visitors.thread_id` and `last_seen` are the app's per
`docs/decisions/foundry-agent-shape.md`, and nothing writes them. #46 declares
four stored procedures and none of them writes session state, and nothing in
this package opens a Snowflake connection — `pool.py`'s `SessionConnection` is
a protocol with no driver behind it in this lockfile, for the reason
`chip_chat.snowflake.snow` gives. So the pointer is durable in the journal
rather than in the column, and `build_visitors` takes the connection factory as
an argument so the adapter lands in one place when it exists. Both are filed.

## The connection pool — where the isolation guarantee breaks

`pool.py` is [#44](https://github.com/gganssle/chip_chat/issues/44), and RFC-001
§05 names it as the risk of the whole design rather than as one more component:

> Session variables and pooled connections are a classic combination for
> cross-tenant bleed. A connection returned to the pool with `demo_id` still set,
> then handed to another visitor's request before it's reassigned, defeats every
> policy above.

Defeats them *quietly*. [#43]'s policies keep filtering correctly, [#45]'s
procedures keep reading `GETVARIABLE('DEMO_ID')` correctly, no tool signature
grows a visitor argument — and the answer is somebody else's lunch, with no
error anywhere in the system.

### The clear is not what makes it safe

The obvious design clears the variable on the way back, and the obvious design
fails **open**: a clear that silently did not take effect leaves a connection
that looks returned and is not clean. So the release path here is an
optimisation, and the load-bearing check is on the other side.

| Step | What it is for |
| --- | --- |
| Read `GETVARIABLE('DEMO_ID')` **before** binding | **The guarantee.** A connection carrying anybody is closed, counted, logged and replaced. It is never handed out. |
| `SET DEMO_ID = ?` | The bind, from the server-side session and never from an argument. |
| Read it back **before any query runs** | A `SET` that succeeded is not evidence the session holds it. A mismatch refuses the request. |
| `UNSET` on release, from a `finally` | Gets the variable off at the earliest moment. A connection that will not clear is destroyed, not filed. |

`api/tests/test_pool.py` points a `ForgetfulSession` — one whose `UNSET` reports
success and changes nothing — at the pool and asserts the next visitor still
sees only their own rows, `stale_discarded` went up, and the pool said so at
`ERROR`. That is the difference between a return path that remembers and a
checkout that cannot forget.

### Four structural facts, and between them there is no unbound query

| Fact | Where |
| --- | --- |
| There is no `get_connection()` — two context managers and no accessor | `pool.py` |
| `for_session` takes a **session id**; no public signature accepts a `demo_id` | `pool.py` |
| A session with nothing bound raises before a slot is taken | `UnboundSessionError` |
| A handle stops working when its `with` block ends | `ReleasedConnectionError` |

`VisitorPool.unbound()` is the one deliberately unbound checkout, and it exists
because [#43]'s `entry_roster` policy requires it: the roster is read *before*
there is a visitor to bind. It is safe because `visitor_isolation` is default
deny — an unbound connection reads zero rows from all seven visitor-scoped
tables, so misusing it is a bug that returns nothing rather than a breach that
returns somebody.

### The test that matters is concurrent

> Sequential tests will pass regardless.

`api/tests/test_pool_concurrency.py` runs 32 visitors through a pool of 4 for 40
rounds — 1,280 checkouts, re-synchronised on a barrier every round so the pool
stays contended from the first request to the last — and asserts every response
contains only its own visitor's data.

It runs the same assertions twice, because a concurrency test never shown to
fail has proved nothing. `chip_chat.api.testing.NaivePool` **does what this
ticket's title says**: it sets on checkout and clears on return, in a `finally`,
every time. It keeps four connections and rotates through them without tracking
which are in use, so two requests can hold one connection and the second one's
`SET` lands between the first one's `SET` and its `SELECT`. Sequentially it is
perfect. Concurrently it discloses on roughly a thousand of those 1,280
requests, and the real pool discloses on none.

The fourth assertion is the one that keeps the third honest: the run records
peak overlap and how many distinct visitors were inside the pool at once, and
fails if they never interleaved. A round that did not overlap is a sequential
round wearing threads.

The suite half of this — `disclosure-concurrent-pool-bleed` and the
`BleedingTarget` that proves the harness catches a bleed — already exists in
`eval/adversarial` and is checked by `make adversarial-check`. Pointing that
harness at a deployment is [#82]. Both run on every pull request.

### What was checked against the live account

The trial account answered three questions on 2026-08-27, which is the first
empirical evidence any layer of this chain has produced:

- `SET DEMO_ID` then `GETVARIABLE('DEMO_ID')` returns exactly the value set;
  `UNSET` then reading it back returns `NULL`. The pool's readbacks mean what
  they are written to mean.
- `visitor_isolation`'s body, evaluated live: bound, a visitor's own row is
  visible and another's is not; **once cleared, neither is**. The clear provably
  returns a connection to default deny.
- `UNSET` on a variable that was never defined is a no-op rather than an error,
  so skipping the clear on the entry path is a saving rather than a necessity.

What is **not** evidence: no row was filtered, because every visitor-scoped
table on the account is empty ([#47] is the load). The policies are attached —
`POLICY_REFERENCES` names all seven plus the two marts — so what remains
unobserved is the filtering itself, on rows.

### Configuration

`DEFAULT_POOL_SIZE` is four. [#66] is the app that constructs the pool, and
`build_visitors` is where: it builds the store first, the pool **around** that
store, and the roster through the pool's one unbound checkout, so the ordering
that makes RFC-001 §05 true is written once rather than remembered per call
site. The size is still a constructor argument rather than an environment
variable, because the warehouses are X-Small and suspend after sixty seconds —
a larger pool would not serve anybody faster and would keep one awake.

`build_visitors` takes `connect=None` today and every deployment passes it,
which means no pool, an empty roster and every visitor served unbound. That is
not a placeholder for a decision: `SessionConnection` is deliberately a protocol
with no driver in the lockfile, and the adapter is one parameter away.

## What is not here yet

Every counter and every draft is process-local, which is honest for the
single-instance deployment this demo runs on — and is why the container runs
**one** uvicorn worker. A second worker would be a second ledger, and the daily
ceiling would quietly mean twice what it says. `BudgetLedger`,
`SourceRateLimiter`, `UploadLimiter` and `DraftStore` keep their state behind one
lock and one interface so that a shared store has exactly four places to land
when a second instance exists. `VisitorSessionStore` is the one that already has
somewhere to land — its `SessionJournal` is that seam, and `FileJournal` is the
first thing behind it. A forgotten draft costs a visitor one
re-proposal; it is never a draft placed unconfirmed.

Issue #85 trips the ceiling against the real deployment. `SpendLimits.from_env`
is how that is done without a code change.

[#43]: https://github.com/gganssle/chip_chat/issues/43
[#45]: https://github.com/gganssle/chip_chat/issues/45
[#47]: https://github.com/gganssle/chip_chat/issues/47
[#66]: https://github.com/gganssle/chip_chat/issues/66
[#82]: https://github.com/gganssle/chip_chat/issues/82
