# Decision: the answer is streamed from the provider, and the parsed reply still has the last word

**Issue:** [#112](https://github.com/gganssle/chip_chat/issues/112), [#109](https://github.com/gganssle/chip_chat/issues/109) · **Decided:** 31 August 2026, by Graham · **Measured:** 31 August 2026
**Changes:** `ChatModel.complete` grows an `on_text` callback, `AzureChatModel` gains a streamed path, `_held_open` carries fragments, the widget paints them
**Does not change:** what a turn costs, what a span records, or what the visitor ends up reading

---

The same user-testing session that produced #108–#111 opened with latency, ahead
of every functional bug: *"Latency is very high."* It was the first thing said,
which is the signal worth taking seriously — a turn slow enough to comment on is
the demo's first impression whatever the answer turns out to be.

## What was measured, on 31 August 2026

From Application Insights, the deployed app, spans from the tester's own session
between 14:03 and 14:09 UTC. Durations in milliseconds:

| Span | n | p50 | p95 |
| --- | --- | --- | --- |
| `chat.turn` | 11 | 517 | 21,104 |
| `agent.step` | 10 | 7,150 | 13,689 |
| `llm.completion` | 8 | 6,521 | 13,689 |
| `tool.get_points_balance` | 1 | 2,882 | 2,882 |
| `tool.get_usual_order` | 1 | 2,680 | 2,680 |
| `tool.search_menu_knowledge` | 3 | 517 | 628 |
| `retriever.search` | 3 | 517 | 628 |
| `guard.content_safety` | 7 | 0 | 310 |
| `guard.budget_check` | 7 | 0 | 0 |

**The model call is the whole of it.** `agent.step` p50 of 7,150 ms against an
`llm.completion` p50 of 6,521 ms means a step is model time plus rounding.
Retrieval at 517 ms is not the problem, the guard at 0 ms is not the problem, and
the tools at under 3 s are not the problem. A turn is two or three sequential
model calls, which is the 13–21 s a visitor was sitting through.

## The decision

**Stream the completion. Do not try to make the model faster.**

Two or three round trips at ~6.5 s each is what an agent with eleven tools costs,
and the ways to cut it — fewer tools offered, fewer steps allowed, a smaller
deployment — all trade an answer's quality for its speed. What was actually wrong
is subtler and cheaper to fix: **none of that time was visible.** `model.complete`
returned a finished reply, and `_frames` then chopped it up with `_chunks` and
sent it as NDJSON, so the route *looked* like a streaming route and was not.
`_stream`'s own docstring was honest about it:

> The *tokens* are not: `ChatModel` has one method and it returns a finished
> reply, so the prose is chunked here after the turn rather than forwarded from
> the provider as it arrives.

So the visitor waited the entire 6.5 s of the final model call before a single
character appeared, in front of a waiting indicator that was a literal `…` and
did not move (#109). Slow and apparently dead are different problems, and only
one of them needed an architecture change.

## The two things that made this more than a plumbing job

**Streaming reopened `chip-2ky` from the other side.** The provider writes the
model's raw output, and this deployment's model ends every food answer with the
D9 envelope as a line of JSON. Forwarding fragments verbatim puts
`{"claim_class":"food","citations":[...]}` in front of the visitor — the exact
bug the envelope parser exists to prevent, arriving by a route the parser never
sees. `envelope.parse` scans *backwards* from the end of a finished reply, so it
cannot be run over a stream at all.

The answer is two-layered rather than clever. `ProseStream` is a display filter
that holds back everything from the first `{` or backtick onward, so a brace is
never painted; and `_frames` then sends the parsed reply as a `text_final` frame
which the widget uses to *replace* what it painted. The filter can be wrong in
one direction only — it can withhold prose that turned out to be innocent — and
`text_final` repairs that a moment later. `agent/tests/test_prose_stream.py`
holds both halves, including the case where the filter withholds too much.

**Streaming can silently break the spend cap, which is invariant 3.** A streamed
response reports usage only in a final chunk, and only when `include_usage` is
asked for. A provider or an API version that declines leaves the counts at zero —
and zero is indistinguishable from *free* to a ledger that settles it. So
`ModelReply.usage_reported` carries whether the number is real, `TurnResult`
folds it across steps, and `FundedTurn.run` charges the pessimistic reservation
rather than believing a zero. Over-counting by less than one turn is the safe
direction; the other direction is how an inline cap quietly stops being one.

## What was not measured

- ~~Time to first token after the change.~~ **Now measured, and the result is
  worse than the expectation this section originally recorded. See below.**
- **Total turn duration is not expected to improve at all**, and nothing here
  tries to. `chat.turn` should measure the same after this change as before it;
  what changes is when the visitor starts reading. If a later reader finds this
  document while looking for why turns still take 20 s: that is why.
- **The sample is small and from one afternoon.** Eight `llm.completion` spans,
  one conversation, one tester, no concurrency. There is no p99 here and no
  weekday-afternoon baseline.
- **Whether prompt caching would help**, which was the tester's own suggestion.
  The prompt is 28–36k tokens and mostly a stable prefix, so it plausibly would,
  on both cost and first-token latency — but it was not tried, not measured, and
  is not part of this change.
- **How often `ProseStream` withholds prose it did not need to**, i.e. how often
  a `{` appears in a genuine answer. Expected to be rare for a restaurant
  assistant and not counted.


## Measured again after the deploy, and the honest result

Revision `ca-chip-chat-web--0000045`, 31 August 2026, three questions, streamed
shape, timed from request to first `text` frame:

| Question | First token | Whole turn | Fragments | First token at |
| --- | --- | --- | --- | --- |
| what is in a burrito bowl? | 27.17 s | 28.72 s | 111 | 95% of the turn |
| do you have vegan options? | 10.39 s | 11.85 s | 80 | 88% of the turn |
| is the barbacoa spicy? | 12.62 s | 13.56 s | 48 | 93% of the turn |

**The streaming is real and it does not fix the reported problem.** The
fragments are genuine — 48 to 111 of them, arriving continuously, with no
envelope leaking into any of them — but they all arrive in the last 5–12% of the
turn. A visitor still waits ten to twenty-seven seconds before a single
character appears.

The reason is structural and was visible in the first table without anybody
reading it correctly, this author included. A turn is two or three *sequential*
model calls: the model is asked what to do, a tool answers, and only then is the
model asked to write prose. Streaming can only begin when the last call begins,
so it compresses the final 1.5 s of an answer and can do nothing at all about
the 10–27 s in front of it. The `llm.completion` p50 of 6.5 s is the cost of a
*step*, and the wait a visitor experiences is the sum of the steps before the
last one.

An earlier revision of this document is worth keeping in view: it predicted
first paint would move "from ~6.5 s to roughly the provider's first-chunk
latency". That was wrong, and it was wrong because it reasoned about one span
instead of the sequence. The measurement is the correction.

**What was gained, stated without inflation.** The waiting indicator now stops
and prose flows continuously once it starts, so the end of a turn reads as an
answer being written rather than a block of text appearing. Combined with #109's
animated dots, the app no longer *looks* dead. It is still slow.

**What would actually move the number**, none of it done here and each its own
piece of work:

- **Tell the visitor what the turn is doing.** The 10–27 s of silence is spent
  in `tool.search_menu_knowledge`, `tool.get_points_balance` and the reasoning
  around them, all of which the app knows about as they happen. A frame saying
  "checking the published menu" would not make the turn faster and would change
  what the wait feels like more than streaming did.
- **Fewer round trips.** Two or three steps is what an eleven-tool agent costs
  when it plans, calls, and then answers. Whether a turn can routinely be done
  in one is a prompt and tool-design question, not a transport one.
- **Prompt caching**, still untried, still the tester's own suggestion, and now
  the most attractive remaining lever: at 28–36k prompt tokens against ~1.7k of
  completion, the prefix dominates every one of those sequential calls.
