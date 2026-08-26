# Decision: visitors edit three fields, and those three live in a table the marts never read

**Issue:** [#59](https://github.com/gganssle/chip_chat/issues/59) (bead `cc-d97`) · **Decided:** 26 August 2026
**Resolves:** PRD §11 Q2
**Unblocks:** [#69](https://github.com/gganssle/chip_chat/issues/69) (persona switcher), [#66](https://github.com/gganssle/chip_chat/issues/66) (session store)

---

## The decision, in one paragraph

Switching between fixtures stays the primary mechanism, exactly as E5 has it. On top of
it, a visitor may edit **three things and nothing else**: their display name, their home
store, and their stated preferences. Those three are stored on `demo_visitors` —
app-owned, visitor-scoped, and **never read by the Databricks medallion pipeline**.
Order history, the loyalty ledger, and every gold mart derived from them are read-only
to the visitor. The degradation the PRD warns about is therefore not prevented by a
policy anyone has to remember; it is prevented by the editable fields living in a
different table from the mart inputs, so **there is no edit a visitor can make that a
gold mart was computed against**.

---

## Why "no editing at all" was never actually available

The obvious conservative answer is switch-only, and it would be the right instinct if
the requirements permitted it. They do not: **T1 already lists *update stated
preferences* as one of the six supported actions.** Preference editing is a shipped
V0 requirement with a confirmation card and a receipt attached to it.

So a switch-only decision on #59 would have contradicted a requirement in the same
document, and the interesting question is not *whether* visitors change persona state —
it is which changes are safe and why. Framed that way the middle path the ticket
proposes is not a compromise, it is the only coherent reading.

## Which fields, and why each is safe

The safety test is precise: **is this field an input to a nightly gold mart?** Marts are
keyed on `demo_id` and computed from `orders`, `order_items` and `loyalty_ledger`. A
field none of those jobs reads cannot make a mart stale, because the mart was never a
function of it.

| Field | Editable | Why |
| --- | --- | --- |
| `display_name` | **Yes** | Appears in copy only. E1 already lets the visitor supply it; letting them change it is the same act, later. |
| `home_store_override` | **Yes** | No mart reads it. `customer_360.favourite_store` is derived from `orders.store_id` and is unaffected — see below. |
| `stated_preferences` | **Yes** | Required by T1. Applied as a serving-time filter, never as a mart input. |
| Order history | No | The input to every mart, and the evidence P1 cites when it explains a usual order. |
| Points balance / ledger | No | System of record for redeem-points (T1), and reconciled against published rewards terms in [#27](https://github.com/gganssle/chip_chat/issues/27). A visitor who can mint points makes the whole action lane a toy. |
| `usual_order`, `customer_360`, `item_affinity`, `spend_summary` | No | Derived. Editing a derived value is not editing an account, it is editing an answer. |
| `persona_id` | Switch, not edit | E5's mechanism. A switch is a new `demo_id` on a clean connection ([#69](https://github.com/gganssle/chip_chat/issues/69)), not a mutation. |

**Home store deserves its own paragraph**, because it is the one edit that looks
dangerous and is not. Changing it does not rewrite where past orders happened; it
changes where the *next* one would. Two consequences, both good. Prices move, because
`item_prices` is keyed by restaurant — a Steak Burrito is $11.15 at one store and
$13.15 at another (see [menu-pricing.md](menu-pricing.md)) — so a home-store edit
demonstrates a design decision the demo otherwise only asserts. And it can put stated
home in visible disagreement with derived `favourite_store`, which is not degradation
but a real customer situation: someone who moved. The rule is that we **say it** rather
than reconcile it silently — *"most of your orders are at Ballard; I've set Fremont as
home, so I'll price there."*

## The structural guarantee, and the schema change that buys it

RFC §04 gains two columns:

```
demo_visitors   demo_id, display_name, persona_id, thread_id,
                home_store_override, stated_preferences,
                created_at, last_seen
```

`demo_visitors` is app-owned session state. Bronze ingestion
([#33](https://github.com/gganssle/chip_chat/issues/33)) reads the harvested corpus and
the generated account stream; it does not read this table, and after this decision it
must not. That single containment is what makes the PRD's failure mode —
*"a visitor construct[ing] a state the gold marts were never computed against"* —
structurally unavailable rather than merely discouraged.

It is the same move as D4 and D3: remove the code path rather than promise nobody takes
it. A reviewer checking that this holds does not read the serving logic; they check
that nothing under `databricks/` selects from `demo_visitors`, which is a grep and a
CI-able assertion rather than a judgement.

## Preferences filter and annotate; they never rewrite a derived value

The interesting collision is a stated preference that contradicts a derived mart: the
visitor says *no dairy*, and their `usual_order` — computed from eighteen months of
orders — contains cheese. Two wrong answers are available. Silently dropping the cheese
makes P1 lie about what their usual is. Ignoring the preference makes T1's edit
decorative.

The rule is that the mart is reported as computed and the preference is applied on top,
out loud:

> Your usual is a barbacoa bowl with white rice, black beans and cheese. You've told me
> no dairy, so I've left the cheese off this one — say the word if you want it back.

The derived value keeps its meaning, the visitor's edit does something, and the
disagreement is visible rather than buried. This also protects P2: recommendations stay
grounded in actual ordering behaviour, with preferences as a filter over the candidate
set, not as a substitute for the evidence.

## What the acceptance criterion about staleness turns into

#59 asks that if editing is allowed, we specify the behaviour when marts are stale
relative to the edit — most likely by surfacing `derived_at` rather than pretending
freshness. Because no editable field is a mart input, **an edit cannot make a mart stale
relative to it**. That criterion is satisfied by construction, and the paragraph above
covers the case it was reaching for.

Mart staleness in the ordinary sense is unchanged and already specified: RFC §10 says
serve stale marts with their `derived_at` and alert, never silently as fresh. Two places
that surfaces:

- P1 explains how it worked the usual order out; when `usual_order.derived_at` predates
  the visitor's most recent order, that explanation says so — *"based on your orders
  through Tuesday."*
- `usual_order.confidence` is already load-bearing for the Explorer fixture
  ([#26](https://github.com/gganssle/chip_chat/issues/26)), which is deliberately a
  low-confidence usual. The honest "I'm not sure what your usual is" path exists and
  the edit surface does not get to route around it.

## The durability constraint from #9

[#9](https://github.com/gganssle/chip_chat/issues/9) decided visitor state persists via
cookie, and the assignment is right that an edit which was cheap for one session is a
different proposition when it survives. It is cheap here for a reason that is worth
being explicit about: **the edits persist in the same row, with the same lifetime, as
everything else about the visitor.** They are aged out by the same last-seen policy the
nightly reset ([#47](https://github.com/gganssle/chip_chat/issues/47)) applies to the
visitor, because they are columns on the row that policy already deletes. No new
retention question, no second lifetime to reason about.

What durability *would* have made expensive is a mutable order history — a divergence
compounding on every visit, against marts recomputed nightly from a population that no
longer matches. That is the thing this decision keeps read-only.

## Consequences for open tickets

- **[#69](https://github.com/gganssle/chip_chat/issues/69) — persona switcher.** Its
  open question is answered: personas can be edited *and* switched, but editing is
  three fields. Switching is unchanged — new `demo_id`, clean connection, fresh thread,
  and it says so. An edit is **not** a switch: it does not restart the conversation and
  does not mint a new `demo_id`.
- **[#66](https://github.com/gganssle/chip_chat/issues/66) — FastAPI session store.**
  Owns the two new columns and the write path to them. Edits are visitor-scoped writes
  under the same identity binding as everything else (RFC §05); the edit endpoint takes
  no visitor identifier, per D4.
- **[#68](https://github.com/gganssle/chip_chat/issues/68) — chat widget.** A preference
  update is an action, so T2 applies: it renders a confirmation card and returns a
  receipt like any other write.
- **[#36](https://github.com/gganssle/chip_chat/issues/36) — gold marts.** Unchanged,
  and now with an explicit contract: the marts read `orders`, `order_items` and
  `loyalty_ledger`, and do not read `demo_visitors`.
- **[#47](https://github.com/gganssle/chip_chat/issues/47) — nightly reset.** Unchanged;
  the new columns age out with their row.
- **[#82](https://github.com/gganssle/chip_chat/issues/82) — cross-visitor red team.**
  The edit path is a new write surface and belongs in the suite. It is visitor-scoped
  and takes no identifier, so it should be uninteresting — which is the claim worth
  testing.
- **PRD** — §11 Q2 resolved; new requirement **E7** naming the three editable fields.
- **RFC-001** — §04 schema.

## Revisit

If the demo shows visitors reaching for edits we did not allow — most likely wanting to
adjust points to see the redemption flow — the smallest next step is a **scripted
scenario** rather than a writable ledger: a control that switches to a fixture built to
show that state. Fixtures are cheap and always produce a good demo; a writable ledger
is a mart input with a confirmation gate in front of it, and that is the door this
decision is holding shut.

## Sources

PRD §11 Q2, E1, E5, T1, T2, P1–P3, §09 constraints. RFC-001 §04, §05, §10, D3, D4.
Issues [#59](https://github.com/gganssle/chip_chat/issues/59),
[#9](https://github.com/gganssle/chip_chat/issues/9),
[#69](https://github.com/gganssle/chip_chat/issues/69),
[#26](https://github.com/gganssle/chip_chat/issues/26).
