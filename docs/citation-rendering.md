# Drawing the citation: D9 on the request path

**Beads:** `chip-2ky` (the leak), `chip-znk` (a withdrawn tool),
[#105](https://github.com/gganssle/chip_chat/issues/105) (the triple greeting) ·
**Written:** 28 August 2026
**Implements:** [D9](decisions/citation-presentation.md), PRD K2 and K5
**Touches:** `agent/envelope.py`, `agent/loop.py`, `agent/tools.py`,
`api/app.py`, `web/page.py`

---

## What was on the screen

Live on `ca-chip-chat-web--0000040`, every food or policy answer ended with a
line the visitor could read:

```
Moderately. It's braised with chipotle chiles and cumin, so it carries more heat
than the carnitas but less than the hot salsa.
{"claim_class":"food","citations":["53b556ab...","5e613323..."]}
```

That is not a bug in any one module. It is a bug in the *absence of a caller*,
and the shape is worth naming because this repository has now produced it three
times — `SpendGuard` shipped correct with nothing calling it, `uploads.py`
shipped correct with nothing calling it, and `chip_chat.agent.envelope` shipped
correct, tested, and imported only by `eval/`. Each time the module was right
and the deployment was wrong, and each time the tests passed.

`prompts/system-v1.md` asks the model to return supporting passage ids *"in the
`citations` field of your response"* and tells it not to write source lines into
its answer. The model obeyed. It wrote the field as a trailing line of JSON,
because a chat completion has no other place to put a field. Nothing parsed it,
`Conversation` returned `reply.content` verbatim, and the packaging arrived as
prose.

## The path, end to end

Five things now happen and none of them existed on the request path before.

**1. The retrieval's citations survive the tool boundary.**
`Retrieval.as_tool_result()` deliberately withholds `source_url` from the model —
that is the field a model could paste into an answer, and D9's whole mechanism
is that it never reaches one. So the citations cannot be recovered from the tool
result. `_search_menu_knowledge` therefore returns the `Retrieval` itself when a
lane is wired, and `_dispatch_inside_span` flattens it: citations to the turn,
`as_tool_result()` to the model. That is the same shape `PhotoMatch` already
had for token counts, for the same reason — something the span layer needs and
the model must not see.

**2. The loop collects them across the whole turn.** `run_turn` holds one
`dict[str, Citation]`, updated by every `search_menu_knowledge` call in every
step. It accumulates across steps, because a model may search twice and cite
from the first search in a reply written after the second. It does **not**
accumulate across turns: the mapping is local to the call, so an id from a
previous turn resolves against nothing.

**3. `envelope.parse` separates the prose from the field.** Three shapes are
accepted — the whole reply as one JSON object, a trailing JSON object after the
prose (fenced or not), and anything at all, which comes back as the text
untouched with no citations and `claim_class: none`. The third branch is the
load-bearing one; see *Failing towards the sentence* below.

**4. `envelope.render` resolves the ids.** Unchanged, and it was already
correct: an id the retriever did not return this turn is dropped into
`dropped_citation_ids` rather than resolved. This is the anti-minting mechanism
and the parser does not weaken it — a parsed id and a hand-constructed id go
through exactly the same resolution.

**5. The route carries it and the widget draws it.** `ChatReply` gained
`citations` and `claim_class`; the streamed shape gained a `sources` frame
between the text and the card; `renderSources` draws the trailing line. The
widget has no parser and no path from `text` to a rendered citation, which is
the property D9 asks for by name.

## Failing towards the sentence

A reply is a visitor-facing sentence first and a data structure second. Every
failure in `parse` therefore has to fail towards showing the sentence:

| what the model did | what the visitor reads | what the envelope says |
| --- | --- | --- |
| prose, then the field | the prose | the ids, resolved |
| prose only | the prose | nothing claimed |
| an unfinished field | the whole reply, JSON and all | nothing claimed |
| an unknown `claim_class` | the prose | nothing claimed |
| nothing at all | the loop's fallback sentence | no envelope |

The third row is deliberate and is the only one that can still show a visitor a
brace. A half-written object is indistinguishable from prose that happens to
contain one, and guessing wrong in the other direction would silently delete
part of an answer. Showing too much beats showing too little, once.

RFC-001 §10's rule is that a lane may fail and the conversation may not, and it
is not weaker for the failure being ours rather than a service's.

## Why a parser and not a `response_format`

The bead named both. A `response_format` on the completion is the tidier
mechanism and it was not taken, for two reasons.

The first is that it cannot be tested here. Nothing that costs money or needs a
credential is in `make ci`, so a JSON-schema response format would be a contract
with the provider that no gate in this repository exercises — and the failure
mode of getting it wrong is *every turn returns nothing*, which is worse than
the bug being fixed.

The second is that the parser is needed anyway. A `response_format` constrains
the deployed chat model; it does not constrain an eval experiment that swaps the
deployment, a future hosted Foundry agent (`docs/decisions/foundry-agent-shape.md`),
or a provider that ignores the field on a tool-calling turn. Something has to
read the reply defensively regardless, and once it does, the response format is
an optimisation rather than a mechanism.

`envelope.parse` accepts the whole-reply-as-object shape a `response_format`
would produce, so setting one later is a deployment change and not a second
parser.

## What was deliberately not changed

**`prompts/system-v1.md`.** The prompt is versioned, `PROMPT_VERSION` is
recorded on every `chat.turn`, and `eval/` holds baselines against it. The model
already emits the field; the defect was that nothing read it. Editing the prompt
to describe the wire format would cost a version and change every eval baseline
to fix a bug that is not in the prompt.

**The span vocabulary.** `otel/schema.py` is executable and the twenty-five span
names and their attribute namespaces are what every dashboard and eval is built
on. A `chip_chat.citations.*` attribute would be a schema decision taken inside a
bug fix, so what `render.response` records goes under OpenInference's `metadata`
key — the sanctioned escape hatch — carrying `claim_class`, the resolved ids, the
dropped ids and `uncited_claim`. That is enough to answer *which sources was
this answer drawn from, and did the model name one that was never retrieved*
from a trace, which was not answerable at all before.

**`eval/`'s offline slices.** `eval/grounding`, `eval/dietary` and `eval/golden`
each say in prose that `chip_chat.agent.envelope` has no caller and report their
citation findings as `UNSCORED`. Those slices do not run turns through
`api/app.py`, so wiring them to the now-live path is a separate piece of work
against the same bead family and is **not** done here. The scoring code is
correct and unchanged; what it needs is a source that reports citations.

## What is not measured

Recorded here because it would be easy to imply otherwise.

**Whether the deployed model actually emits the field on most turns.** The one
observation is the screenshot on `chip-2ky` and the sentence in its write-up
that *every* food or policy answer ended with the line. That is a report, not a
rate. Nothing in this change measures how often the model complies, and until a
turn is run against the live deployment the honest statement is that the parser
handles the shape that was observed and the two shapes near it.

**The uncited-claim rate.** PRD K2's target is zero and D9 makes it a rule
rather than a judgement, but the rule now has a *source* for the first time.
What that source says on real traffic is unmeasured. `uncited_claim` is on every
`render.response` span from this change onward, so the first number will come
from a trace query rather than from an estimate.

**The minted-source rate.** Same: `dropped_citation_ids` is recorded and has
never been counted on real traffic. Nobody should quote a figure for it.

**Whether the trailing line hurts readability on a phone.** D9 anticipated this
and named the fallback — shrink presence to an icon that expands, never move
presence behind an interaction. The line is one dimmed row inside the answer's
own bubble and has not been looked at on a handset.

**Latency.** Parsing a reply is string work on a completion that has already
been paid for, and the citation lookup is a dictionary. No measurement was taken
and none is claimed; if a number is ever needed it belongs beside `docs/cost.md`
§14 rather than as a guess here.

## The two smaller fixes shipped beside it

They are in this document because they were found in the same pass over the
request path, and each has its own note where the code is.

**The greeting drawn three times** ([#105](https://github.com/gganssle/chip_chat/issues/105)).
`POST /api/entry` is idempotent by design — issue #9 decided a returning browser
resumes its account rather than collecting a second one — so every extra
submission of the name gate answers with the *same* opening sentence. `enter`
had no re-entrancy guard, the gate stayed live until the response came back, and
`showPersona` appended. On a wired deployment the assignment is a Snowflake
checkout and a roster read, so the window is seconds wide and a held Enter key
fills it. Fixed at the cause (the gate closes and refuses re-entry before the
request goes out) and again at the effect (`sayOpening` replaces a greeting
rather than appending one). `web/tests/test_entry_gate.py` runs the page's own
script in Node against a small DOM and submits three times: three calls and
three bubbles on the old code, one and one on the new.

**`get_recommendations` offered and always declining** (`chip-znk`). Its own
decision record: [withheld-tools.md](decisions/withheld-tools.md).

## Sources

D9 (`docs/decisions/citation-presentation.md`), PRD §05 metrics, K2, K5, Flow 2.
RFC-001 §08, §09, §10. Beads `chip-2ky`, `chip-znk`, `cc-bap`. Issues
[#57](https://github.com/gganssle/chip_chat/issues/57),
[#68](https://github.com/gganssle/chip_chat/issues/68),
[#75](https://github.com/gganssle/chip_chat/issues/75),
[#105](https://github.com/gganssle/chip_chat/issues/105).
