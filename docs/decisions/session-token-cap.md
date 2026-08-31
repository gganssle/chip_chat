# Decision: the session cap becomes 800,000 tokens, and the turn cap becomes a backstop

**Issue:** [#108](https://github.com/gganssle/chip_chat/issues/108) · **Decided:** 31 August 2026, by Graham · **Measured:** 31 August 2026
**Changes:** `api/src/chip_chat/api/limits.py` (the three defaults), `infra/terraform/variables.tf` (`spend_caps`), `api/src/chip_chat/api/outcome.py` (the stop copy), `docs/cost.md` §14.1
**Does not change:** the mechanism. The cap stays inline, synchronous, and in front of every model call

---

A visitor in the user-testing session of 31 August reported hitting *"Cilantro's
had a busy day — come back tomorrow"* after five to ten turns, and asked the two
questions that make this a bug report rather than a feature request: **what is
that, and is there a row we can bump it up?** Both are fair, and the fact that a
person driving the demo could not answer either from the screen is half of what
was wrong.

The sentence is `STOP_STATE_MESSAGE`, and PRD requirement S4 is emphatic that it
is a designed state rather than a failure — no 4xx, no apology, no leaking of
the mechanism. It was doing precisely what it was written to do. What it was not
doing was telling the truth: at five to ten turns the ceiling being reached is
never the daily one, and a visitor stopped by a *session* cap can start another
conversation immediately. Told to come back tomorrow, several testers did.

## What was actually firing, measured

The ticket named two candidates and insisted the second be ruled out before any
number was touched, because raising a cap to paper over a settlement bug would
be the wrong fix twice over. Either turns really cost 12k–24k each and the
120,000 session cap is simply too small for the system as it now exists, or
`record_usage` is not settling on the deployed app — in which case every turn
charges the flat 8,000-token reservation forever, 120,000 / 8,000 = 15 turns,
and the ceiling is a bookkeeping error wearing a ceiling's clothes.

Five consecutive `chat.turn` spans of one conversation in Application Insights,
`chip_chat.tokens.total`, between 14:03 and 14:09 UTC on 2026-08-31:

| Turn | tokens | cumulative |
| ---: | ---: | ---: |
| 1 | 30,339 | 30,339 |
| 2 | 33,708 | 64,047 |
| 3 | 18,125 | 82,172 |
| 4 | 18,074 | 100,246 |
| 5 | 36,938 | **137,184** |

**It is the first candidate, and the settle path is fine.** The rollups are not
five copies of 8,000; they vary from 18,074 to 36,938 in a way only real usage
varies, they are far above the reservation rather than pinned to it, and the
cumulative column crosses 120,000 on the fifth turn — which is the bottom of the
reported "five to ten" exactly. Nothing needs fixing in `ledger.py`. The number
above it was wrong.

The mean is **27,437 tokens a turn**, and the prompt is nearly all of it:
**28,620 prompt against 1,719 completion** on turn 1, and **35,998 prompt** on
turn 5. That second figure is the finding underneath the finding — *prompt
tokens grow with the conversation, so late turns cost more than early ones*, and
any cap sized by multiplying a flat average is a cap that binds earlier than its
arithmetic promises.

For contrast, `docs/cost.md` §3.1 measured the same thing four days earlier and
reads 4,603 tokens for a turn with no tool call and 8,732 for a
`search_menu_knowledge` turn. **A turn costs between three and six times what it
cost on 27 August.** §3.1 predicted this in the same paragraph that reported it
— *"a real chunk-carrying prompt will be larger and the number will move"* —
written while `/healthz/lanes` still reported every lane `not_wired`. [#106]
wired them; the prediction came true; nobody went back to the cap.

## The second thing the arithmetic showed

The two session ceilings contradicted each other, and had done since they were
written.

```
session_token_cap  120,000  /  session_turn_cap  40   =  3,000 tokens per turn
                                turn_token_reservation =  8,000 tokens per turn
```

**The permitted average was below what a single turn claims before the model is
even called.** A turn reserves 8,000 pessimistically and settles the real figure
afterwards, so even a conversation of one-word turns costing nothing at all is
refused at the sixteenth reservation. Forty turns was not a generous cap or a
tight one. It was unreachable — dead configuration that looked like a control,
in the one module of this repository whose entire premise is that the control is
real and synchronous rather than observability.

## The decision

**Twenty turns is the conversation this demo is built to hold, and both caps are
now sized to that same conversation.**

| | was | is | how it was reached |
| --- | ---: | ---: | --- |
| `session_token_cap` | 120,000 | **800,000** | 20 turns × 40,000 |
| `session_turn_cap` | 40 | **22** | the 20-turn target, plus a little |
| `daily_token_ceiling` | 2,000,000 | **8,000,000** | 10 × the session cap |

**40,000 rather than the measured mean of 27,437**, because of the growth curve.
Multiplying the mean by twenty gives 548,740, which would have produced a cap
binding somewhere around turn sixteen — the same mistake as the original, made a
second time with better evidence. The margin between 27,437 and 40,000 is what
pays for a prompt that is bigger on turn 19 than it was on turn 1, and 40,000 is
also above the largest turn actually observed (36,938), which is the weaker of
the two reasons but the easier one to check.

**22 rather than 20**, so that the cap a visitor meets is the one that knows
what turns cost. The turn cap is now explicitly a backstop for the case the
token cap cannot see — a pathologically *cheap* loop, twenty-two one-word turns
that spend almost nothing while still holding a persona out of the roster and a
row in the ledger. Which of the two binds first depends on how expensive the
conversation is, and that is the intended behaviour: an expensive conversation
is stopped by cost, a cheap one by length.

The reconciliation to redo whenever either number moves is the quotient that
caught the old pair:

```
800,000 / 22  =  36,364 tokens per turn      well above the 8,000 reservation
                                             inside the measured range 18,074-36,938
```

It is asserted rather than written down —
`api/tests/test_limits.py::test_the_two_session_caps_describe_the_same_conversation`
holds both ends of that range, so the next inconsistent pair fails a test in
`make ci` instead of sitting unreachable for a month.

**8,000,000 as ten times the session cap**, chosen for legibility rather than
from a traffic model there is no data for. Note the direction it moves the thing
that actually matters, because the headline number is misleading on its own: the
old pair admitted about sixteen capped conversations a day and the new pair
admits ten. *The ceiling quadrupled and the number of visitors it serves went
down*, because the conversation it is sized for is nearly seven times longer.

## What it costs

Honestly and without softening it: **the worst-case day is now four times more
expensive.**

At `docs/cost.md` §3.2's prices — `gpt-5-mini` at $0.25/M input, $0.025/M cached
input, $2.00/M output — and the 95%-prompt split measured above, one conversation
that spends its entire allowance is 760,000 prompt and 40,000 completion:

```
list, no cache        760,000 × $0.25/M  +  40,000 × $2.00/M   =  $0.27
at the 67.6% cache rate §3.2 reverses out of the meter          =  $0.15
```

| | old ceiling | new ceiling |
| --- | ---: | ---: |
| a capped conversation, at list | $0.041 | **$0.27** |
| a day at the ceiling, at list | $0.68 | **$2.70** |
| a month of days at the ceiling | $20 | **$81** |

Against a $150/month budget whose steady state `docs/cost.md` §13 already
estimates at $104–$132, a month spent continuously at the new ceiling would blow
it. That is the true statement, and here is the other true statement beside it,
because quoting either alone is dishonest: **the entire model spend of this
project to date is $0.0941**, one day at the new ceiling is twenty-eight times
everything Cilantro has ever cost, and the busiest day on record produced
thirteen conversations. The ceiling is a bound on catastrophe, not a forecast,
and the layers that make catastrophe unlikely — the per-address rate limit, the
upload ceilings, the kill switch — are all untouched by this decision.

The number that is *not* bounded by any of this is the account lane. A 20-turn
conversation asking three account questions spends $0.60 on Cortex Analyst,
more than twice its whole model bill, and the token ledger cannot see a credit
of it. Making conversations longer has made that gap wider. It is `docs/cost.md`
§14's standing open guardrail and this decision does not close it.

## The copy, which is the other half of the ticket

`STOP_STATE_MESSAGE` was one sentence for every `StopReason`. Its docstring
defended that, and the defence was right about the register and wrong about the
count. What S4 asks for is a designed state: no error framing, no apology, no
naming of the mechanism to whoever is probing it. It does not ask that a stop
lasting until midnight and a stop lasting until the visitor clicks "new
conversation" say the same thing — and when they do, one of them is false.

There are now two sentences, and `stop_message()` picks between them from the
reason rather than from the layer that refused:

> **Cilantro's had a busy day — come back tomorrow**
> — the daily ceiling, the kill switch, the rate limiters

> **That's a good long conversation — start a new one to keep going**
> — `SESSION_TURN_CAP` and `SESSION_TOKEN_CAP`

Both are in the same register. Neither says "quota", neither apologises, neither
carries a 4xx, and `api/tests/test_api_package.py` asserts all of that across
both of them so the split cannot quietly erode what S4 was protecting. The
second sentence names a remedy that works instead of one that costs the visitor
until midnight, and it is deliberately warm about the cause: reaching the end of
a long conversation is a compliment to the conversation, not a fault.

`UPLOAD_RATE_LIMIT` is session-scoped and deliberately keeps the *first*
sentence. "Start a new one" is exactly the move that limit exists to defeat, and
`SESSION_SCOPED_REASONS` in `outcome.py` carries that reasoning at the one place
the two sets could be confused for each other.

## What could not be measured, and is therefore not in any table above

Recorded at length because the tables above look far more solid than the
evidence under them, and the next person to touch these numbers should know
exactly how thin it is.

**There is no p95, because there is no distribution.** Every figure here comes
from **five turns of one conversation, held by one person, over six minutes of
one afternoon**. `docs/cost.md` §3 could at least say "93 turns across thirteen
sessions"; this cannot. The mean of 27,437 has no error bar, and a conversation
that leans harder on the knowledge lane — retrieved passages are the largest
variable part of the prompt — could plausibly run well above it. The three
numbers chosen here would not survive being called a measurement of *the*
per-turn cost. They are a measurement of *these* turns.

**The growth curve past turn five is extrapolated, not observed.** The whole
argument for 40,000 rather than 27,437 rests on prompt tokens rising with the
history, and the evidence for the rise is two points — 28,620 on turn 1 and
35,998 on turn 5 — with a non-monotonic middle (turns 3 and 4 cost about half of
turns 1 and 2, almost certainly because they called no tool). Whether turn 20
carries 45,000 prompt tokens or 90,000 is not known. **It could not have been
observed**, and the reason is circular in a way worth stating plainly: no
conversation on the deployed app has ever reached twenty turns, because until
today the cap made twenty turns impossible. The first honest measurement of a
20-turn conversation becomes available only *after* this change ships, which is
also the argument for re-reading these spans in a week rather than treating this
document as finished.

**The daily ceiling is not sized against traffic.** 8,000,000 is ten times the
session cap because ten is a number a person can hold in their head. There is no
model of how many visitors a day this demo will see, no arrival distribution,
and no measured relationship between the two ceilings — thirteen conversations
have ever existed. If real traffic arrives, this is the first number that should
be re-derived, and it should be re-derived from arrivals rather than from the
session cap.

**Nothing here was measured from inside the deployment.** The token counts are
the provider's own, reported through `llm.token_count.*` and rolled up into
`chip_chat.tokens.*`, so they transfer; the dollar figures are those counts
multiplied by list prices taken off the Azure Retail Prices API on 2026-08-26,
not read off a meter. `docs/cost.md` §9 already documents that the spans and the
meter disagree by a lag, and none of the dollar rows above has been reconciled
against a bill.

## What this does not change

**The mechanism.** Invariant 3 of `CLAUDE.md` is that the spend cap is a
synchronous check in the request path, structurally impossible to bypass because
`FundedTurn` cannot be constructed without it. This decision moves three
integers. It does not touch `guard.py`'s shape, `SpendGate`'s private hold on
the model, or the tests in `api/tests/test_spend_gate.py` that assert nothing
else can spend. A cap that has been retuned is still a cap.

**Where the number lives.** `SpendLimits.from_env` still reads
`CHIP_CHAT_SESSION_TOKEN_CAP`, `CHIP_CHAT_SESSION_TURN_CAP` and
`CHIP_CHAT_DAILY_TOKEN_CEILING`, and `infra/terraform/variables.tf` still passes
all three as application settings. So the answer to the tester's *"is there a
row we can bump this up"* remains **yes, without a code change** — the defaults
moved because the defaults were wrong, not because the knob was missing.

**The reservation.** `turn_token_reservation` stays at 8,000. It is a
pessimistic charge held for the few seconds between the check and the settle,
and its job is to stop concurrent turns collectively overshooting; the measured
per-turn cost being three times larger does not change that job, because the
settle corrects it within the same request. Raising it would make the daily
ceiling refuse turns it can afford.

**The other four layers.** Per-address rate limiting, the two upload ceilings,
the upload token charge and the kill switch are all untouched and all still
bound what they bounded before.

[#106]: https://github.com/gganssle/chip_chat/issues/106
[#108]: https://github.com/gganssle/chip_chat/issues/108
