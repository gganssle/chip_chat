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

## Configuration

Every ceiling is an environment variable, all optional, all defaulted small:

| Variable | Default |
| --- | --- |
| `CHIP_CHAT_DAILY_TOKEN_CEILING` | `2000000` |
| `CHIP_CHAT_SESSION_TURN_CAP` | `40` |
| `CHIP_CHAT_SESSION_TOKEN_CAP` | `120000` |
| `CHIP_CHAT_SOURCE_REQUESTS_PER_WINDOW` | `20` |
| `CHIP_CHAT_SOURCE_WINDOW_SECONDS` | `60` |
| `CHIP_CHAT_TURN_TOKEN_RESERVATION` | `8000` |
| `CHIP_CHAT_BUDGET_RESET_TIMEZONE` | `UTC` |
| `CHIP_CHAT_KILL_SWITCH` | unset |

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

## What is not here yet

The counters are process-local, which is honest for the single-instance
deployment this demo runs on — and is why the container runs **one** uvicorn
worker. A second worker would be a second ledger, and the daily ceiling would
quietly mean twice what it says. `BudgetLedger` and `SourceRateLimiter` keep their
state behind one lock and one interface so that a shared store has exactly two
places to land when a second instance exists.

Issue #85 trips the ceiling against the real deployment. `SpendLimits.from_env`
is how that is done without a code change.
