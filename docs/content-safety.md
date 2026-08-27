# Content Safety on inbound text, and the shield beside it

Issue [#79](https://github.com/gganssle/chip_chat/issues/79). Written 2026-08-27,
after implementing it against a specification the red team wrote first.

## 1. Why this was the gap it was

The image lane has moderated its input since #52. Text had never been moderated
at all. The asymmetry was not a decision anybody made — it is simply where the
work stopped, and it left the more exposed of the two surfaces uncovered: an
unauthenticated public LLM endpoint, on a personal Azure subscription, reachable
by anyone with the link.

What makes it worth writing down is how it was found. The Phase 10 red team does
not own `api/src`, and the right output of a red team is findings rather than
unilateral patches. So instead of patching, it wrote
`eval/tests/test_content_safety_gate.py`: four tests that failed, marked
`xfail(strict=True)`, each naming in its docstring the exact change that would
make it pass. Strict xfail means **the build breaks on the day they start
passing** — so the person implementing #79 had to come back and delete the
markers rather than being free to leave four criteria half-met.

That worked exactly as intended, and the tests are kept as written.

## 2. The four criteria, and how each is met

### Moderation runs before the model — structurally

The criterion is *nothing unmoderated reaches a model*, and the test asserts it
**on the clock**: `guard.content_safety` must have *finished* before
`llm.completion` *started*. Asserting only that both spans exist would pass on an
implementation that moderated after the answer came back.

The enforcement is not that `_run_turn` remembers to call the moderator first.
It is that `TextModerator` is **private to `SpendGate`**, and the only object in
the process that can call a model is a `FundedTurn`, which `SpendGate.turn()`
only yields after the budget check and the screen have both passed. A refusal
yields a `Stop`, which has no `run`. A route added next year cannot reorder the
check because there is no second door to go through.

This is the same shape `turns.py` already used for the spend cap, and it is
reused deliberately: the argument was already made and already tested.

### Shield detections are visible in the trace

`chip_chat.content_safety.shield_detections`, set on **every** screened turn,
including when it is empty. An absent attribute and a shield that ran and found
nothing are different facts, and on a public endpoint the span is the only record
that a jailbreak was attempted at all. A detection nobody recorded is a detection
nobody can audit afterwards.

Labels carry their subject: `user_prompt:persona_override` for the visitor's own
message, `document:0:attack_detected` for a retrieved passage. The second is the
cross-prompt half, and it is the half #81 plants its payloads for.

### Retrieved content is delimited by something it cannot forge

This is the criterion worth the most attention, because the obvious
implementation does not meet it.

A tool result used to reach the model as bare `json.dumps(result)`. Wrapping it
in a fixed `<document>` tag would look like a fix and would not be one: a corpus
document containing `</document>` closes it, and an attacker who can influence
the corpus — which PRD S2 assumes — can put anything in a document.

So the envelope carries a **nonce minted per call**:

```
<retrieved id="menu-CHIPS" nonce="a7f3…">…</retrieved:a7f3…>
```

The nonce did not exist when the corpus was written, so there is nothing for a
planted passage to close. Any occurrence of it is stripped from the passage
before wrapping — which removes nothing a real document contains, and is there so
the property is enforced by the code rather than argued from probability.

`chip_chat.agent.loop._delimited`. Note what this does **not** do: a passage that
corrupts an *answer* still corrupts it. That residual is #81's, measured there and
accepted deliberately in `docs/decisions/corpus-injection-residual.md`. What the
envelope removes is any ambiguity about which bytes were retrieved.

### A moderation outage disables the turn rather than bypassing it

Two outcomes that look identical to a visitor and must not be confused in a
trace:

| | Visitor sees | `chip_chat.guard.reason` | Conversation |
| --- | --- | --- | --- |
| Content Safety **flags** the message | `BLOCKED_MESSAGE` | `content_blocked` | continues |
| Content Safety **unreachable** | `BLOCKED_MESSAGE` | `moderation_unavailable` | turn refused, no model call |

Same sentence to the visitor, deliberately — a refusal that explains which word
tripped it teaches an attacker what to write instead. Different token on the
span, also deliberately: an operator has to be able to tell *somebody typed
something we declined* from *moderation has been down for an hour and every turn
is being refused*.

**The trap here is real and the red team named it in advance.** `app.py` wraps
the turn in a broad `except Exception`. A moderation call placed naively inside
that would be swallowed, and the visitor would get an apology having spent
nothing — which looks exactly like failing closed and is not, because the check
would simply be skipped again on the retry. So the outage is caught in
`turns.py`, before the handler ever sees it.

## 3. What is deployed, and what is not

`LocalTextAnalyzer` is the default and is **not a substitute for Content
Safety**. It recognises published jailbreak shapes by regular expression and
flags **no harm categories at all** — inventing a `hate` verdict from a regex
would produce exactly the false confidence #79 is written against, and would
block real visitors on a keyword.

It exists for two reasons. `make ci` runs free and offline, so the plumbing — the
span, the attributes, the fail-closed path — is exercised on every run. And a
deployment whose configuration silently loses its endpoint degrades to a weak
check rather than to none.

`AzureTextAnalyzer` is the real one: `text:analyze` through the SDK for harm
categories, and `text:shieldPrompt` over HTTP for the shield, because the SDK
does not cover the shield endpoint. It authenticates with the app's managed
identity, exactly as `AzureImageAnalyzer` does — there is no key here and nothing
that could authenticate with one.

**It is not wired into the deployment yet.** `build_service` does not pass one,
so the running app screens with the local analyzer. Connecting it is:

```python
SpendGate(..., moderator=TextModerator(AzureTextAnalyzer.from_env()))
```

plus `AZURE_CONTENT_SAFETY_ENDPOINT` in the Container App's settings — the same
variable the image lane already reads, pointing at the same
`cs-chip-chat-4cy39i` resource. It is one line and an app setting, and it is left
undone here rather than done badly: `AzureTextAnalyzer` has no test against the
live service, and shipping an unexercised client into the request path of a
public endpoint is how a moderation outage becomes a moderation absence.

That is the honest state. The criteria are met; the strongest available analyzer
is not the one running.
