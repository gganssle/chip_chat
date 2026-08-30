# A bound on one model call, and not on one turn

**Status.** Accepted, 28 August 2026. Implements `chip-ala`, which was filed
while re-reading `chip-901`.

## What was wrong

`chat_client` in `agent/src/chip_chat/agent/foundry.py` built `AzureOpenAI` with
no `timeout` and no `max_retries`. The library's defaults are six hundred
seconds and two retries, so a single completion could in principle run for half
an hour, and `loop.py` makes up to `DEFAULT_MAX_STEPS` of them per turn. There
was no number anywhere in this repository that bounded how long a visitor could
wait.

That had been true since the module was written, and it had never shown, for a
reason worth stating plainly: **Container Apps ingress was acting as the timeout
nobody had configured.** It closes a response idle for sixty seconds, so a turn
that hung was cut at sixty seconds and the visitor at least saw an end.

`chip-901` removed that accident, correctly. `_held_open` in
`api/src/chip_chat/api/app.py` now writes a heartbeat byte every ten seconds for
as long as the turn runs, so genuinely slow turns — p95 was 72.8 seconds — survive
instead of dying at sixty. The consequence is the reason this decision exists:
the missing timeout became **less visible rather than less real**. After
`chip-901` a hung deployment holds the conversation open indefinitely, with the
application cheerfully writing whitespace at the visitor the entire time.

This is a general shape worth naming. A fix that removes an accidental
constraint inherits responsibility for whatever that constraint was accidentally
holding up. The heartbeat was right; it was also the moment this became urgent.

## What was decided

A per-call bound, configurable, stated on both construction paths.

`_DEFAULT_TIMEOUT_SECONDS` is ninety and `_DEFAULT_MAX_RETRIES` is one. Both are
overridable through `CHIP_CHAT_FOUNDRY_TIMEOUT_SECONDS` and
`CHIP_CHAT_FOUNDRY_MAX_RETRIES`, following the precedent `SpendLimits.from_env`
set: a number that might need to change against a live deployment should not
need a code change to change it.

`max_retries` is one rather than the library's two because retries multiply
against both the timeout and the step count, and because the deployment's
observed failure mode under concurrency is `RateLimitError` from a `gpt-5-mini`
provisioned at capacity 10 (`docs/deployment.md` §3.13). A third attempt into a
deployment that is refusing for want of capacity spends ninety more seconds to
be told the same thing.

An unreadable value is **refused rather than ignored**. Somebody who sets
`CHIP_CHAT_FOUNDRY_TIMEOUT_SECONDS=90s` believes they have set a timeout; falling
back to the default would leave them believing it. That is the original bug in a
new costume, so it raises `FoundryConfigError` at construction.

## What was deliberately not decided

**This is not a turn budget, and it must not be read as one.** A turn is several
calls, so the worst case a turn can reach is the per-call bound multiplied by the
attempt count and again by `DEFAULT_MAX_STEPS`. `test_the_worst_case_turn_is_
stated_rather_than_implied` computes that figure rather than asserting a
constant, so the number is reported whenever either constant moves.

Bounding the turn itself is a different mechanism and was not built here, because
it is not really a timeout question. A turn budget has to decide what the
assistant *says* when it fires — whether the partial answer goes out, whether the
tool results gathered so far are worth anything, whether the visitor is told the
truth or something kinder. That is a product decision and it does not belong in
a bug fix about an HTTP client.

## What is not measured

**Whether ninety seconds is the right number.** It is not derived from a
measurement and the code says so. `docs/deployment.md` §3.13 measured p50 41 s,
p95 72.8 s and a longest of 95.2 s across 34 turns — but those are *whole turns*,
several model calls plus tool time, and nothing in this repository has ever
measured a single completion in isolation. Ninety seconds is set above the
longest observed whole turn on the reasoning that a single call taking longer
than an entire slow conversation is a hang rather than a slow answer. That is an
argument, not a measurement, and it is the weakest part of this decision.

What would settle it: `llm.completion` span durations off Application Insights
over a normal day, which the span vocabulary already records and nobody has
queried for this purpose.

**How often the timeout will actually fire.** Unknown. If it fires on healthy
traffic the number is wrong and the correction is to raise it, not to remove it.

**What the visitor sees when it fires.** The call raises, `loop.py` handles it as
it handles any model failure, and the resulting message has not been read by
anybody in this state. Worth a look the first time it happens in a demo.

## References

- `chip-ala` — the bead.
- `chip-901`, and `docs/deployment.md` §3.12–3.13 — the ingress timeout, the
  heartbeat that replaced it, and the measurements quoted above.
- `agent/tests/test_model_call_timeout.py` — nine tests, including both
  construction paths.
