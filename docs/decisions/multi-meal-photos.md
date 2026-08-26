# Decision: V0 detects several meals in one photograph and asks which one — it does not build any of them

**Issue:** [#58](https://github.com/gganssle/chip_chat/issues/58) (bead `cc-d97`) · **Decided:** 26 August 2026
**Resolves:** PRD §11 Q4
**Unblocks:** [#55](https://github.com/gganssle/chip_chat/issues/55) (multi-meal branch), [#56](https://github.com/gganssle/chip_chat/issues/56) (labeled photo set)

---

## The decision, in one paragraph

**Option 1: detect and decline gracefully.** When stage 4 returns `meals_visible > 1`,
V0 says what it saw, says plainly that it builds one meal at a time, and asks the
visitor to point at one — by re-sending a photo of just that meal, or by describing it
in words, which routes into the ordinary order flow. It does **not** build a draft from
the frame, and it does not pick a meal on the visitor's behalf. This is the design's
"handle gracefully" reading of *handle several meals in one frame*, chosen deliberately
over "handle fully," and it keeps V0 consistent with
[#93](https://github.com/gganssle/chip_chat/issues/93), where group ordering is already
deferred to V1.

---

## The argument that actually decides it

Option 2 — build the most prominent meal and offer to switch — sounds like the
generous middle, and on the surface it costs nothing extra. It does, and the cost is in
the schema.

The stage-4 output in RFC §07 returns **one** slot set: one `vessel`, one `protein`,
one `rice`, one `beans`, and arrays of salsas and toppings — plus `meals_visible` as a
count. When four bowls are in frame, those slots do not describe the most prominent
meal. They describe *the picture*, and nothing in the schema says which meal each
value came from. A frame with a chicken bowl and a steak burrito can legitimately
return `vessel: bowl`, `protein: steak` — a meal that is not on the table. The
resulting draft would be composed entirely of real catalogue items, so V6 holds and
nothing looks wrong; it is simply an order nobody in the photograph is eating.

That is the worst failure shape this product has: confident, well-formed, and only
detectable by the visitor. It is also exactly what D3 was written to prevent one layer
down.

So option 2 is not a cheaper option 1. It requires either slots becoming an array of
per-meal slot sets, or the model selecting a primary meal — and the second is a
judgement about visual prominence that we cannot verify, made by the component whose
output we deliberately refuse to trust as a product identifier. Both are schema changes
plus new evaluation surface, in a lane that already carries a component-level F1 target
of 0.80 on single meals.

**Option 1 is the only option whose behaviour the current schema can actually support.**
`meals_visible` is enough to know we should stop. It is not enough to know what to
build.

## Why not full multi-order support

Option 3 is the honest version of "supporting it well," and the ticket already
identifies it as V1. Two reinforcing reasons to leave it there:

**It is most of #93.** Multiple drafts held simultaneously, each independently editable
and confirmable, is the first bullet of the group-ordering scope. Deciding #58 as
"build the multi-order flow" while #93 stays deferred would be deciding the same
feature two ways in two tickets — the inconsistency the assignment specifically warns
about. The group orderer is out of scope for V0 in PRD §02 and group ordering is a
non-goal in §04. This decision does not get to quietly overturn both.

**V0 has no confirmation story for it.** T2 requires a structured confirmation card
before anything happens, and the launch gate is *zero account writes executed without
explicit confirmation*. Four drafts means four cards, partial confirmation states, and
"place order" meaning something new. That is a real design, and it belongs in the
release that also designs per-person attribution and group loyalty accrual.

## What V0 does, precisely

Trigger: `meals_visible >= 2` from stage 4, evaluated **before** the resolve stage. No
SKU resolution runs on a multi-meal frame, because resolving ambiguous slots is how an
ambiguous frame turns into a confident draft.

The response, in the shape of PRD Flow 4 and satisfying V3's requirement to say what it
believes it saw:

> Looks like about four meals on that table — I build one order at a time, so I'd get
> it wrong if I guessed. Send me a photo of just the one you want, or tell me which it
> is and I'll build it from there.

Three properties that are requirements, not copy suggestions:

- **It names the count it saw.** "Several meals" is a category; "about four" is an
  observation the visitor can correct, which is what V3 asks for.
- **It offers a concrete next step**, in both available modalities. A bare decline is
  the failure #55 already rules out for the non-Chipotle case, and the same standard
  applies here.
- **It never silently builds.** No card, no draft, no proposal — because a proposal the
  visitor did not ask for is the thing option 2 was rejected for.

Two edge cases worth writing down before they are discovered in Phase 6:

**One meal plus a side.** A bowl next to a bag of chips is one order, not two.
`meals_visible` counts orderable *meal-sized compositions*, and #53's prompt has to say
so or the decline fires on the most ordinary photo anyone will send. This is the
likeliest false positive and the labeled set should carry it.

**Spatial references do not resolve in V0.** "The one on the left" is not a thing the
pipeline can act on — the slots were never per-meal. If the visitor answers with a
position rather than a re-crop, ask for the meal in words ("the chicken bowl?") and
build from that. It is an ordinary order turn at that point, and the vision lane is
out of it.

## What we are giving up, stated plainly

A visitor who photographs a table gets a question instead of an order. That is one turn
of friction on a genuinely plausible input, and it is a worse demo moment than the
alternative reads on paper. We are accepting it because the alternative is a bowl
nobody ordered, and because *photo orders confirmed with one edit or fewer ≥ 70%* is a
metric that a blended-slot draft would quietly poison — every such draft needs edits,
and some get confirmed anyway.

The detection still earns its place in the schema, exactly as the ticket says: this
decision spends `meals_visible` on knowing when to stop rather than on knowing what to
build.

## Consequences for open tickets

- **[#55](https://github.com/gganssle/chip_chat/issues/55) — the three photo cases.**
  Case 3 is now specified: gate on `meals_visible >= 2` before resolve, respond as
  above, never produce a draft. Its acceptance criterion about reliable detection is
  unchanged and now load-bearing, since detection is the whole behaviour.
- **[#56](https://github.com/gganssle/chip_chat/issues/56) — labeled photo set.** At
  least three multi-meal frames, per #58's own criterion, plus at least one
  meal-with-a-side frame asserted as `meals_visible == 1`. False positives here cost a
  working order; false negatives cost a fabricated one. Both directions get scored.
- **[#53](https://github.com/gganssle/chip_chat/issues/53) — describe stage.** The
  prompt must define `meals_visible` as orderable meal-sized compositions, and must not
  be asked to rank prominence.
- **[#62](https://github.com/gganssle/chip_chat/issues/62) — draft store.** V0 holds one
  open draft per session, but the store is keyed by `draft_id` and must not *assume*
  one. That is #93's "check now, before it is built" item, and the answer is that the
  key already allows several; only the UX is single-draft.
- **[#93](https://github.com/gganssle/chip_chat/issues/93) — V1 group ordering.** The
  multi-meal photo case stops being a decline and becomes the entry point, unchanged
  from that ticket's scope.
- **PRD** — §11 Q4 resolved; new requirement **V7** so the behaviour is covered by the
  launch criterion that every requirement is met or explicitly deferred.
- **RFC-001** — §07 records the gate.

## Revisit

When #93 is picked up. The trigger to revisit early would be the labeled set showing
multi-meal frames are common enough that a turn of friction is a real cost — but note
that "common" is measured on what strangers actually send, which we will not know until
[#77](https://github.com/gganssle/chip_chat/issues/77) is feeding production traces
back. Until then this is the shape that cannot be wrong in the expensive direction.

## Sources

PRD §02, §04, §05 metrics, §11 Q4, V3, V5, V6, T2, Flow 4. RFC-001 §07, D3. Issues
[#58](https://github.com/gganssle/chip_chat/issues/58),
[#55](https://github.com/gganssle/chip_chat/issues/55),
[#93](https://github.com/gganssle/chip_chat/issues/93).
