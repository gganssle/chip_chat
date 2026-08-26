# Decision: citations are inline by default, and they are a field rather than a sentence

**Issue:** [#57](https://github.com/gganssle/chip_chat/issues/57) (bead `cc-d97`) · **Decided:** 26 August 2026
**Resolves:** PRD §11 Q3
**Unblocks:** [#68](https://github.com/gganssle/chip_chat/issues/68) (chat widget), [#60](https://github.com/gganssle/chip_chat/issues/60) (agent definition), [#75](https://github.com/gganssle/chip_chat/issues/75) (citation-presence eval)

---

## The decision, in one paragraph

Citations show **inline, by default, on every response that makes a food or policy
claim** — rendered as PRD Flow 2's quiet trailing source line, one line per response
rather than a marker per clause. The citation is a **structured field on the response
envelope**, not prose the model writes: the retrieval payload hands the agent a set of
passages with ids, the agent names which ids support the answer, and the renderer draws
them. What is on demand is the *detail* — the passage text and the harvest date expand
when tapped. Presence is never on demand. Allergen and dietary answers get a stricter
rule: their citation renders adjacent to the claim, with `harvested_at` visible without
interaction.

Said as a phrase, because it is the part worth remembering: **inline presence,
on-demand detail.**

---

## Why inline wins on this product specifically

The PRD frames this as a taste question — inline is "more trustworthy and noisier," on
demand is "cleaner and easier to miss" — and on most products it would be. Three things
make it not a taste question here.

**The metric has to measure the thing the visitor sees.** *Menu claims made without a
citation* has a target of **zero**, and it is one of the two headline groundedness
numbers. Under on-demand presentation the citation still exists in the payload, so the
eval would still pass — it would be measuring a field in a JSON object that a visitor
may never open. That is a metric measuring an internal data structure and reporting it
as trust. Inline collapses the two: what #75 counts is what a stranger reads. A demo
whose whole argument is *"we can prove groundedness"* should not have a headline
groundedness metric scored against a hidden field.

**The receipts are the demonstration.** This is a proof of concept whose stated goal is
to show that a conversational assistant can be grounded in published data. Hiding the
grounding behind an interaction hides the thing being demonstrated. The five-minute
scripted demo ([#90](https://github.com/gganssle/chip_chat/issues/90)) either shows a
source on screen or it shows a chatbot being confident.

**The noise cost is smaller than it sounds, because of how we render it.** "Noisier"
describes per-clause footnote markers — the presentation that turns a two-sentence
answer into a legal document. That is not what Flow 2 does. Flow 2 appends one quiet
trailing note, dimmed, after the answer:

> Moderately. It's braised with chipotle chiles and cumin, so it carries more heat than
> the carnitas but less than the hot salsa. If you want to dial it up, the
> tomatillo-red chili salsa is the hottest thing on the line.
> — *Menu · Barbacoa*

One line. Deduplicated by source, so an answer drawing on three chunks of the same page
cites the page once. That is inline presentation at close to on-demand's visual cost,
and it is the reason the PRD's own copy already reads this way. The ticket was right to
call Flow 2 the starting proposal; it is also the right answer.

## Why the citation is a field and not a sentence

This is the half of the decision that is engineering rather than presentation, and it
is the half that makes K2 enforceable.

If the model writes "— Menu · Barbacoa" as text, then a citation is a string a language
model produced, which means it can be produced for a claim the retrieval never
supported, or produced with a plausible page name that was not in the payload. We would
then be checking a model's self-report with another model — and the ticket asks
specifically whether the uncited-claim eval "can measure what it needs to under either
presentation."

So the response envelope carries them:

```
{
  "text": "...",
  "citations": [
    { "id": "chunk_8f21", "label": "Menu · Barbacoa",
      "source_url": "...", "harvested_at": "2026-08-24T03:11:00Z" }
  ],
  "claim_class": "food" | "policy" | "allergen" | "account" | "none"
}
```

Every `id` must be an id the `retriever.search` span actually returned on that turn.
An id that was not retrieved is dropped by the renderer and recorded as a violation —
the model cannot mint a source, in the same way D3's constrained vocabulary means the
vision model cannot mint a menu item. Same move, same reason: make the failure
structurally impossible rather than statistically rare, and #49 already carries
`source_url` and `harvested_at` through to the caller, so the data is there for free.

The consequence for #75 is the one that matters: **citation presence becomes a rule,
not a judgement.** `claim_class` is in `{food, policy, allergen}` and `citations` is
empty → fail. Deterministic, cheap, runs on every live turn rather than on a sample.
The groundedness judge stays a judge, because "is this claim supported by that passage"
genuinely is one. Splitting the deterministic half out is what lets the zero target
mean zero.

## Allergen and dietary answers cite differently

The ticket anticipated this and it is correct. For allergen and dietary questions the
citation is doing safety work, not decoration:

- **Adjacent, not trailing.** The source renders with the claim rather than at the end
  of the response, so that in an answer covering three items it is unambiguous which
  source backs which.
- **`harvested_at` visible without interaction.** Published allergen data goes stale,
  the corpus is re-harvested weekly ([#38](https://github.com/gganssle/chip_chat/issues/38)),
  and "as published on 24 August" is a materially different claim from "as published."
  This is the one place a date earns permanent screen space.
- **Never collapsed, never deduplicated away.** The one-line dedup rule above does not
  apply here.
- K3 is unchanged and still governs: when the published data does not answer the
  question, the answer is that it does not, unconditionally. A citation is not a
  substitute for a refusal, and #75 already scores over-refusal in the other direction.

## What this does not do

It does not weaken K2 and it does not touch it. K2 says every food or policy claim
carries a citation; that was true before this decision and is true after. This decision
only settles **where the citation is drawn and who draws it**. If anything it
strengthens K2, because the previous reading left "carries a citation" satisfiable by a
string in a model's output.

It also does not make everything cite. Account answers ("you have 1,250 points") are
grounded in Snowflake, not in the corpus, and a source link on them would be
meaningless — `claim_class: "account"` exists precisely so the rule does not fire
where there is no published page to point at. Confirmation cards, receipts and the
opening persona message cite nothing.

## Consequences for open tickets

- **[#68](https://github.com/gganssle/chip_chat/issues/68) — chat widget.** Renders the
  trailing source line from `citations`, dimmed, dedup by `source_url`; tapping expands
  passage and harvest date. Allergen answers render adjacent with the date visible. The
  widget never parses citations out of `text`.
- **[#60](https://github.com/gganssle/chip_chat/issues/60) — agent definition.** The
  response format gains `citations` and `claim_class`. The system prompt asks for
  supporting ids, not for formatted source text — and per that ticket's own framing,
  this stays a formatting instruction and is not load-bearing for security. The
  enforcement is the renderer's id check.
- **[#75](https://github.com/gganssle/chip_chat/issues/75) — evals.** Citation presence
  is implemented as a deterministic rule over the envelope, plus an id-validity check
  against the `retriever.search` span. Allergen answers reported as their own category
  and additionally checked for date visibility.
- **[#49](https://github.com/gganssle/chip_chat/issues/49) — retrieval.** No change
  required; the passage ids it already returns are what the envelope references. Worth
  confirming ids are stable across an alias-swap rebuild, since a rebuilt index that
  renumbers chunks would invalidate ids mid-conversation.
- **PRD** — §11 Q3 resolved; new requirement **K5** records the presentation rule.
- **RFC-001** — §08 and new decision **D9**.

## Revisit

If the trailing line measurably hurts readability on a phone — the surface most of the
demo's traffic will arrive on — the fallback is to keep presence inline but shrink it
to a source icon that expands, **not** to move presence behind an interaction. The
thing that must not change is that a visitor can see, without acting, that the answer
came from somewhere.

## Sources

PRD §05 metrics, §11 Q3, K2, K3, Flow 2. RFC-001 §08, D3. Issues
[#57](https://github.com/gganssle/chip_chat/issues/57),
[#49](https://github.com/gganssle/chip_chat/issues/49),
[#75](https://github.com/gganssle/chip_chat/issues/75).
