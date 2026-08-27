# The action surface

**Issue:** [#23](https://github.com/gganssle/chip_chat/issues/23) (bead `cc-25z`) · **Written:** 26 August 2026
**Authoritative for:** the arguments, preconditions and validation rules of the four write
tools — `place_order`, `cancel_order`, `redeem_points`, `update_preferences` — and for what
is deliberately not in the action surface at all.
**Derived from:** the harvested menu of [#19](https://github.com/gganssle/chip_chat/issues/19)
and the harvested policy corpus of [#21](https://github.com/gganssle/chip_chat/issues/21),
both re-harvested live on 26 August 2026 for this document.
**Blocks:** [#24](https://github.com/gganssle/chip_chat/issues/24) (`menu_catalog`),
[#46](https://github.com/gganssle/chip_chat/issues/46) (stored procedures for the write actions).

---

Issue #23 exists because Phase 1 decides the action surface, and because the tempting way
to decide it is to imagine one. The list below is not imagined. Every rule in it is either
read off a document Chipotle publishes — the per-restaurant ordering menu, the rewards
landing page, the rewards terms, the FAQ endpoint the contact page renders from — or it is
marked **invented**, with the reason it had to be, in [§10](#10-everything-invented-in-one-list).

There are exactly two kinds of statement here:

- **Observed.** Someone can check it. Every observed claim names the table it came from,
  which carries `source_url` and `harvested_at` on every row, because #18 captured those at
  the edge for precisely this purpose.
- **Invented.** The published record does not answer the question, and V0 needs an answer.
  Each one is flagged inline as **[INVENTED]** and repeated in §10.

The document reconciles against the tool contracts already fixed in
[RFC-001 §06](rfc-001.md). **It does not widen them.** Where the real ordering flow implies
an action outside the four write tools, §5 says so and leaves the tool list alone, because
changing that list is a decision for the RFC and not a detail of this specification.

---

## 1. What the real ordering flow offers

### 1.1 Two order types, and a third that is not one

**Observed.** The FAQ describes the entry point in one sentence: pickup asks the customer to
search for a nearby restaurant, delivery asks for an address (`faq_entries`, Ordering /
General). The menu API carries the same distinction as
data rather than prose: every priced row has both a `unit_price` and a `unit_delivery_price`,
and every item has an `eligibleForDelivery` flag. At restaurant 0679 the delivery price is
consistently ~30% above the pickup price — a Chicken Burrito is $11.15 for pickup and $14.50
for delivery.

Catering is a third path and **not** a variant of the first two: a different host, a
different subscription key, a different menu endpoint, package minimums, and its own
modification window. It is out of scope — see [§5](#5-what-is-out-of-reach).

**Timing is not a customer choice for either order type.** Asked whether a delivery time can
be scheduled, Chipotle answers *"Kinda yes, kinda no"*: online and mobile orders arrive as
soon as possible, and only a catering order lets the customer choose when (`faq_entries`,
Delivery / Delivery - General, a top question). There is no published evidence of a
scheduled-pickup affordance either, and the ordering SPA could not be walked to check for one
— see [§11](#11-what-was-not-checked).

### 1.2 The catalogue

**Observed**, from `menu_items` and `item_prices` at restaurant 0679, harvested 26 August 2026:

| | Count | Max quantity per line | Carries modifiers |
| --- | ---: | ---: | --- |
| Entree | 65 | 1 | yes |
| Side | 31 | 5 | **no** |
| Drink | 21 | 5 | **no** |
| Non Food Items | 3 | 5 | **no** |
| *modifier-only items* | 72 | — | — |
| **Total rows in `menu_items`** | **192** | | |

Two facts in that table do real work.

**Every entrée has `max_quantity` 1.** Two burritos are two lines on the order, not one line
with a quantity of two. A draft that says `{item_id: "CMG-1", qty: 2}` is invalid against the
published menu, and the ops API should reject it rather than normalise it, because the price
of the second burrito depends on modifiers the visitor has not chosen yet.

**Sides, drinks and hardware carry no modifiers at all.** Not "few" — zero rows in
`modifiers` name any of the 55 of them as a parent. Chips are chips. This makes the
validation split cleanly in two: 65 entrées go through the grammar of §1.3, and the other
55 need only an id, an availability check and a quantity between 1 and 5.

The 72 modifier-only items are the ones with a null `category`: black beans, guacamole, a
soft flour tortilla. They exist in `menu_items` because they have identity and a price, and
they are not orderable on their own. The correspondence is exact, and worth asserting on every
harvest: the set of null-`category` items and the set of ids appearing anywhere in `modifiers`
are the same 72 rows, with nothing in either that is not in the other.

Ordering *a* guacamole therefore means ordering `CMG-1009` ("Side of Guacamole", a Side,
$2.95), which is a different row from `CMG-1001` ("Guacamole", the modifier, also $2.95 on a
burrito). Conflating the two is the first mistake a naive matcher makes.

### 1.3 The modifier grammar

This is the part issue #23 asks for by name: *every modifier a real bowl/burrito/taco/salad/
quesadilla order can carry, and the rules governing them.* The published menu answers it
completely and in a shape worth taking seriously, because it is a small type system rather
than a flat list of toppings.

**Observed.** Four tables describe it: 215 rows in `modifier_groups`, 1,385 in `modifiers`
and 1,385 in `portion_options`, across 72 distinct modifier items.

**A slot is a `modifier_group` with a minimum and a maximum.** Slots are per item, and the
slot layout is a function of the item's `item_type`:

| `item_type` | Slots (`min`, `max`) | Ungrouped modifiers also offered |
| --- | --- | --- |
| `Burrito`, `Bowl`, `Salad` | `Rice` (1,1) · `Beans` (1,1) | Toppings, ExtraPortion, HalfPortion, Option |
| `Tacos` | `Tortilla` (1,1) · `Toppings` (1,5) · `Premium` (0,999) | — |
| `Quesadilla` | `Dip` (1,3) · `Addon` (0,999) · `Fillings` (0,999) | HalfPortion |
| `KidsBYO` | `Tortilla` (1,1) · `Side` (1,1) · `Drink` (1,1) · `Option` (1,2) | — |
| `KidsQuesadilla` | `Rice` (1,1) · `Beans` (1,1) · `Tortilla` (1,1) · `Side` (1,1) · `Drink` (1,1) | — |
| `BYOProtein` | `Rice` (1,1) · `Beans` (1,1) · `ExtraProtein` (0,1) · `Included` (0,4) · `Optional` (0,3) · `Premium` (0,1) | — |
| `BYOChips` | `AlwaysIncluded` (2,2) · `Salsa` (1,1) | — |

A `max` of 999 is the published value and means "no practical limit"; a `min` of 1 means the
slot is **required**, which is why a bowl with no rice selection is not an under-specified
bowl but an invalid one. "No Rice" (`CMG-5003`) and "No Beans" (`CMG-5053`) are real
modifiers occupying the required slot — the way to order a bowl without rice is to select
the absence, not to omit the choice. The same pattern covers `No Drink`, `No Included Sides`
and `No Included Toppings` on the kids and taco items.

**Modifiers outside a slot are optional and unbounded** except through the per-item caps
below. On a burrito these are the eleven toppings, the six extra-protein items, the five
half-protein items, and Double Wrap.

**Which are single-select and which are not** is `max_quantity` on the slot: rice, beans,
tortilla and drink are single-select; taco toppings allow up to five; quesadilla dips up to
three; the ungrouped toppings are unrestricted.

**Portions are a closed four-value vocabulary**, published as ids, and permitted per
*(item, modifier)* pair rather than globally:

| Portion | `option_id` | Toward `max_customizations` | Toward `max_on_the_side` | Toward `max_contents` |
| --- | ---: | ---: | ---: | ---: |
| Light | 1 | 1 | 0 | 0 |
| Extra | 2 | 1 | 0 | 0 or 1 |
| Side | 3 | 0 or 1 | 0 or 1 | 0 |
| Half | 4 | 0.5 | 0 | −0.5 |

The weights are per row in `portion_options`, not constants — which is why the table has two
values in three of its cells. Guacamole on a burrito allows only `Side`; cheese allows
`Light` and `Extra` but not `Side`; rice and beans allow all four. **`Half` appears on rice
and beans only, and only on burritos, bowls and salads** — that is the half-white-half-brown
affordance, and its 0.5 weight is exactly what lets two halves fit under a cap of one.

**Six per-item caps bound the whole line.** They are columns on `menu_items`; `-1` means
unlimited and `null` means the item takes no modifiers:

| `item_type` | `max_customizations` | `max_contents` | `max_extras` | `max_halfs` | `max_extras_plus_halfs` | `max_on_the_side` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Burrito / Bowl / Salad | −1 | −1 | 1 | 1 | 1 | 3 |
| …the four Veggie variants | −1 | −1 | 0 | 0 | 0 | 3 |
| Tacos | 3 | −1 | 0 | 0 | 0 | −1 |
| Quesadilla | −1 | 6 | 0 | 1 | 1 | −1 |
| Cheese Only Quesadilla | −1 | 6 | 0 | 0 | 0 | −1 |
| KidsBYO | 1 | 5 | 0 | 0 | 0 | −1 |
| KidsQuesadilla | 0 | −1 | 0 | 0 | 0 | −1 |
| BYOProtein | −1 | −1 | 1 | 0 | 1 | 3 |
| BYOChips | −1 | −1 | 0 | 0 | 1 | 3 |

`max_extras` and `max_halfs` track the *extra-protein* and *half-protein* modifier items
exactly. Checked across all 65 entrées: an item offering `ExtraPortion` or `ExtraProtein`
modifiers has `max_extras` 1 and an item offering none has 0, with no exceptions, and the same
biconditional holds for `HalfPortion` and `max_halfs`. The Veggie Burrito is offered neither
and has both caps at 0. `max_extras_plus_halfs` equals `max(max_extras, max_halfs)` on every
entrée, which reads as **one second protein per entrée, extra or half, not both**. That last
reading is inferred from the columns rather than confirmed in the ordering UI — see §11.

**A modifier's identity is per-parent, not global, and so is its price.** "Add queso"
resolves to three different rows depending on what it is going on:

| Where | Queso Blanco | Guacamole | Cilantro Lime Sauce |
| --- | --- | --- | --- |
| Burrito, bowl, salad | `CMG-1029` · $1.80 | `CMG-1001` · $2.95 | `CMG-5412` · $1.00 |
| A single taco | `CMG-1034` · $1.35 | `CMG-1207` · $1.35 | `CMG-5414` · $1.50 |
| Three tacos | `CMG-1029` · $1.80 | `CMG-1001` · $2.95 | `CMG-5414` · $1.50 |
| A quesadilla | `CMG-4134` · $2.95 | `CMG-1001` · $2.95 | `CMG-5414` · $1.50 |

Three menu words, seven distinct item ids between them, and five distinct prices. A matcher
that resolves "queso" to one id and reuses it will price a taco wrong and fail rule 7 of §7.1
on a quesadilla.

Twenty of the 72 modifier items cost money at restaurant 0679. Extra protein ranges $4.40
(chicken, sofritas) to $6.40 (steak, barbacoa) on a single entrée, and $8.00–$10.00 on a
Build-Your-Own family meal, which is a different set of item ids again. Double Wrap is
$0.50. Everything else — every rice, bean, salsa, lettuce, cheese, sour cream, fajita
veggie, tortilla choice — is $0.00.

**Twenty-one modifiers are defaults** (`is_default` true): guacamole is included on all four
Veggie entrées and on the Cheese Only Quesadilla, and Chipotle-Honey Vinaigrette on every
salad. A default that the visitor did not ask for still belongs on the confirmation card,
because it is on the food.

### 1.4 What the grammar does *not* vary by

Identity and structure are national; money is local. `menu_items`, `modifier_groups`,
`modifiers` and `portion_options` carry no `restaurant_id`. `item_prices` does, and prices
move nearly twenty percent between stores — see [`decisions/menu-pricing.md`](decisions/menu-pricing.md).
So a draft can be *composed* without knowing the store and cannot be *priced* without it,
which is why `propose_order` must resolve a store before it returns a total, and why every
quoted price in a confirmation card carries a store and a harvest date.

---

## 2. The rewards surface

### 2.1 The published catalogue

**Observed**, from `rewards` (the flip tiles on `https://www.chipotle.com/rewards`, hand-checked
against the rendered page in [`chipotle-policy-spot-check.md`](chipotle-policy-spot-check.md)):

| Points | Reward, as published |
| ---: | --- |
| 85 | SIDE TORTILLA |
| 350 | Chips |
| 400 | Fountain Drink |
| 500 | Guac |
| 700 | DOUBLE PROTEIN |
| 815 | 50% OFF AN ENTRÉE |
| 1,625 | ENTRÉE |
| 1,775 | ENTRÉE AND CHIPS |

Eight rewards, and the signed-in Rewards Exchange — which is not public — is not consulted.
The mixed casing is what the markup says; the page renders everything in capitals.

### 2.2 Redeeming points does not order anything

This is the fact most likely to be got wrong. The **REWARDS** section of the terms
(`policy_sections`, `rewards-terms`) says that once points are redeemed the corresponding
Reward is added to the participant's account — some immediately, some within about
forty-eight hours — and the points are deducted at the moment it is added.

Redemption **mints a Reward into the account**. Applying that Reward to food is a separate,
later act at checkout. Three consequences that the tool contract has to respect:

- **It is irreversible.** The terms state that redeemed points are *"gone"*, with no refunds,
  returns or exchanges — for points, cash or anything else — even if the reward itself is
  returned. The FAQ puts it in customer language: points cannot be returned once used, so
  *"choose wisely."* There is no un-redeem, so there is no `cancel_redemption` tool, and the
  confirmation card in front of `redeem_points` is doing more work than the one in front of
  `place_order`.
- **The minted Reward expires.** The terms give it sixty days from the day it lands in the
  account. A receipt that does not carry that date is incomplete.
- **Availability is checked at redemption time, not at display time.** The terms make
  redemption *"subject to availability"* at the moment of redeeming, and reserve the right to
  change what a reward costs in points at any time. A catalogue read a minute ago is a quote,
  not a guarantee.

One more published rule constrains *use* rather than redemption: the rewards page FAQ says
Chipotle typically allows **only one reward per order**, and that rewards, coupons and promo
codes cannot be combined. The terms say it more generally, ruling out redemption in
combination with other promotions, offers, discounts or coupons unless a promotion allows it.

### 2.3 How points are earned

**Observed**, from the rewards terms section **ACCUMULATING POINTS** and the FAQ. These are
the constraints [#27](https://github.com/gganssle/chip_chat/issues/27) reconciles the
generated ledger against; they are recorded here because `redeem_points` reads a balance that
has to have come from somewhere defensible.

- 10 points per $1 spent — published as prose in three places, and deliberately never turned
  into a number by the harvest.
- An account earns on at most **three qualifying purchases per day**, and several menu items
  bought in one transaction count as no more than one of the three.
- Excluded from the earning total: taxes, tips, donations, and every named fee — delivery,
  bag, service, convenience, recycling deposits — plus alcohol and gift-card purchases.
- Points expire after **365 days of account inactivity**.
- Chipotle reserves the right to **deduct the points from a qualifying purchase that is later
  voided or cancelled** — the one published sentence tying cancellation to the ledger. §7.2
  uses it.

### 2.4 The gap: a reward does not name a menu item

**Observed absence.** The published catalogue gives a name, a point cost and an image path,
and **nothing that identifies a SKU**. #21 declined to derive one, correctly: half the tiles
are marketing art, "ENTRÉE" is not the burrito it pictures, and the image behind SIDE
TORTILLA points at `cmg-5501-flour-tortilla` — a modifier item priced at $0.00 — while the
orderable side of that name is `CMG-4025` at $0.50.

So the reward → item mapping does not exist in the published record, and V0 has to supply
one. That is the largest invented artefact in this document; §7.3 and §10 specify it.

---

## 3. Cancellation: the product does not offer it

Issue #23 asks *what "cancel an order" actually means in the product, and within what window*.
The published answer is that it means nothing, and the window is zero.

**Observed.** Asked *"Can I cancel my online or mobile order after I've submitted it?"*,
Chipotle answers that a submitted order goes straight to the restaurant crew, so
*"we're unable to cancel"* (`faq_entries`, Ordering / General, flagged by Chipotle as a top
question).

Delivery is the same with an escape hatch that is not self-service (Delivery / Delivery -
General, also a top question): the order goes to the courier team and the crew, and a customer
who really must cancel is directed to Customer Service and warned they may incur a
cancellation fee. Modification after submission is refused outright (Delivery / Delivery - My
Order) — Chipotle does not accept modifications to a submitted delivery order, and says its
delivery partners hold the same policy.

The **only** published self-service window in the whole ordering surface belongs to catering
(Catering / Customization), which may be modified **up to 24 hours before** the scheduled
pick-up or delivery. Catering is out of scope, so that window is not ours to borrow.

### What this means for `cancel_order`

RFC-001 §06 fixes `cancel_order(order_id)` and PRD **T1** requires *cancel a pending order*.
Neither is wrong, because PRD **T5** already says the actions are simulated and the
confirmation card says so. But the honest reading has to be stated rather than glossed:

> **`cancel_order` has no unsimulated counterpart.** In the real product a submitted pickup
> order cannot be cancelled by the customer at all, and a submitted delivery order can only be
> cancelled by contacting Customer Service, possibly for a fee.

Three ways to respond, and the recommendation is the third:

1. **Drop the tool.** Contradicts PRD T1, which lists cancellation as one of the six supported
   actions. Not available without a PRD change.
2. **Simulate a window and say nothing.** This is the failure issue #23 was written to
   prevent: an invented affordance presented as if it mirrored the product.
3. **Keep the tool, keep the signature, and make the simulation explicit.** ✅ Recommended.
   `cancel_order` operates only on orders in the demo's own pre-handoff state, the window is
   named as ours, and the copy on the confirmation card says what the real product does. The
   tool list does not widen; one invented constant enters the design, in §7.2, and is listed
   in §10.

The demo is better for this, not worse. A visitor who asks to cancel and is told *"Chipotle
can't normally cancel an order once the crew has it — in this demo I can, because nothing here
is real"* has learned something true about the product.

---

## 4. Preferences: what a customer can actually persist

Issue #23 asks *which stated preferences a customer can actually persist*.

**Observed.** What the real product persists under the word "preferences" is
**communication opt-ins**. Two FAQ answers describe the same screen: an account holder opens
*preferences* and opts into SMS or email alerts (Gift Cards & Coupons / Coupons; Restaurants /
New Restaurants). The account also stores payment methods — at most one gift card at a time —
which is a stored credential, not a stated preference.

**Observed absence.** There is no published evidence that Chipotle persists a dietary
preference, a default store, or a default build. And there is a published answer that argues
directly against free text (Ordering / Ordering): asked whether special instructions can be
added to an app order, Chipotle says *"Unfortunately, there isn't"* — comment boxes were found
to cause confusion — and points the customer at modifying individual ingredients in the
ordering flow instead.

That is the real product saying that a preference which cannot be expressed as a modifier
should not be expressible at all. It is the best argument available for the shape §7.4 gives
`update_preferences`: **a closed vocabulary drawn from the modifier taxonomy, not a free-text
note.** A visitor may say "no dairy" because dairy names modifiers the grammar can act on;
they may not say "ask them to be generous with the rice."

Which three fields are editable is already settled and not reopened here:
[`decisions/persona-editing.md`](decisions/persona-editing.md) fixes `display_name`,
`home_store_override` and `stated_preferences` as the only editable columns, all on
`demo_visitors`, a table no gold mart reads. §7.4 specifies their arguments and validation,
nothing more.

**A stated preference is not an allergy**, and the ops API must not let it become one. PRD
**K3** requires unconditional honesty on allergen and dietary questions, and
[`decisions/allergen-absence.md`](decisions/allergen-absence.md) makes an unpublished allergen
a three-valued `NOT_PUBLISHED` rather than a false negative. A `no-dairy` preference filters a
candidate set. It is not a safety guarantee, it does not consult `item_allergens`, and the
receipt for a preference update says so in words.

---

## 5. What is out of reach

Issue #23 asks for this list explicitly. Everything here is real, published, and deliberately
**not** in the action surface. Each entry says what a V0 including it would have cost.

| Real affordance | Evidence | Why it is out of scope |
| --- | --- | --- |
| **Group ordering** | published as a real affordance in three FAQ entries — a group builds one order together | A fifth write tool plus a second identity in one draft. Identity is bound per session at the database connection (RFC §05); a draft two visitors write to is a hole in exactly the boundary that design exists to close. |
| **Catering** | 6 packages, 93 option rows, own host and key (`catering_packages`) | A separate ordering path with minimums, scheduling and a 24-hour modification window. A parallel action surface, not an extension of this one. |
| **Scheduling a pickup or delivery time** | delivery is as-soon-as-possible; only catering may choose a time | Nothing published to build against for the two order types we do support. |
| **Special instructions / free-text notes** | *"Unfortunately, there isn't"* (Ordering / Ordering) | The product removed it on purpose. Adding it would be inventing an affordance Chipotle deleted. |
| **Points Requests** (claiming missed points from a receipt) | a receipt-backed claim within 30 days, capped at 2 a day and 4 a month | A write to the ledger with a receipt-image evidence path. `decisions/persona-editing.md` keeps the ledger read-only to visitors for good reason. |
| **Donating points to a nonprofit** | points surrendered in exchange for a $1 Chipotle donation to a chosen charity | A published redemption target that is not a menu item and has no point cost on the public catalogue. `redeem_points` covers menu-item rewards only; see §7.3. |
| **Extras / challenges / badges, birthday and welcome rewards** | `SPECIAL OFFERS` in the terms; rewards page FAQ | Point-earning mechanics with opt-in and activation rules. They belong to the generated ledger of #27, not to a visitor-callable tool. |
| **Gift cards and payment methods** | Several FAQ entries | Stored credentials. Out of bounds for an agent by policy, not merely by scope. |
| **Third-party delivery** (DoorDash, Postmates) | named in the FAQ as earning no points | Not Chipotle's ordering flow; not ours to model. |
| **Refunds** | the published path is Customer Care, via the ASK PEPPER button on the contact page | The real refund path is a human. `search_menu_knowledge` can quote that answer; there is no refund tool and should not be. |
| **Reordering as a stored object** | no published FAQ entry | The *capability* survives — see §6 — but there is no published "saved order" object to model, so nothing is stored and nothing is invented. |

---

## 6. The six PRD actions, mapped

PRD **T1** names six actions. They map onto four write tools and three read tools with no
remainder, which is the check this section exists to perform.

| PRD T1 action | Tools | Notes |
| --- | --- | --- |
| Place an order | `propose_order` → `place_order` | Two steps by design: `propose_order` mints and prices the draft the card renders, `place_order` takes its id. |
| Reorder the usual | `get_usual_order` → `propose_order` → `place_order` | The usual is a gold mart, not a stored order. Nothing new is written; the draft goes through the same validation as any other. |
| Modify a proposed order | `propose_order` (again) | **No write tool, and this matches the product**: modification is possible only before submission, which is exactly what a draft is. PRD T3's editable card re-proposes; it does not mutate a placed order. |
| Cancel a pending order | `cancel_order` | The one action with no real counterpart — §3, §7.2. |
| Redeem points | `redeem_points` | Mints a Reward; does not place an order — §2.2, §7.3. |
| Update stated preferences | `update_preferences` | Three fields, closed vocabulary — §4, §7.4. |

Four write tools. The same four RFC-001 §06 fixes. Nothing implied by the real flow needs a
fifth, and the two candidates that would have — group ordering and Points Requests — are in
§5 with their reasons.

---

## 7. The four write tools, pinned down

Common to all four, and not repeated in each:

- **No tool takes a visitor identifier.** RFC-001 §05. Identity is bound to the Snowflake
  session and enforced by row access policy. Every `order_id`, `draft_id` and `reward_id`
  below is validated against the *bound* visitor; a well-formed id belonging to someone else
  is a not-found, not a forbidden, and not a leak.
- **Confirmation is enforced in the ops API.** RFC-001 §06. Each tool takes the identifier of
  something the visitor has already been shown, and the ops API rejects anything not marked
  confirmed by a request carrying the session.
- **Every action is simulated** (PRD T5) and every card says so.
- **Every failure is a typed rejection**, never a repaired call. The agent does not get to
  round a draft into validity.

### 7.1 `place_order(draft_id) → receipt`

**Argument.** `draft_id` — an opaque id minted by `propose_order`, which is where the
composition rules below are enforced. `place_order` re-checks them, because a draft can go
stale between proposal and confirmation.

**The draft.** A store, an order type, and one or more lines:

```
draft         restaurant_id, order_type ∈ {pickup, delivery}, lines[]
line          item_id, quantity, selections[]
selection     modifier_item_id, group_name | null, portion ∈ {null, Light, Extra, Side, Half}
```

**Validation, in the order the ops API should apply it.** Every rule cites the table that
decides it; all of them are observed.

1. `restaurant_id` exists in `stores`. **[INVENTED: the 50 harvested stores are the whole
   world for V0]** — see §10.
2. Every `item_id` is in `menu_items` with a non-null `category` — the 120 orderable rows.
   The 72 modifier-only items are not orderable alone.
3. Every `item_id` has a row in `item_prices` for this `restaurant_id` with `is_available`
   true. Availability is per store and the harvest keeps unavailable items rather than
   dropping them, so "we don't have that here" is answerable.
4. `1 ≤ quantity ≤ menu_items.max_quantity`. In practice: 1 for every entrée, up to 5 for
   sides, drinks and hardware.
5. If `max_customizations` is null, the line must carry no selections at all. This is the
   55 sides, drinks and hardware.
6. Every required slot is filled: for each row in `modifier_groups` for this `item_id`, the
   number of selections naming that `group_name` is within `[min_quantity, max_quantity]`.
   A missing rice choice on a bowl is a rejection, not a default.
7. Every selection is a published pairing: `(item_id, modifier_item_id)` exists in
   `modifiers`, with the `group_name` the row carries.
8. Every portion is a published pairing: `(item_id, modifier_item_id, portion)` exists in
   `portion_options`. Extra guacamole on a burrito is invalid — guacamole allows only `Side`.
9. All six aggregate caps hold. Sum the weights that `portion_options` and `modifiers` carry
   for the selections on this line, and skip any cap set to `-1`:
   - Σ `counts_toward_customization_max` ≤ `max_customizations`
   - Σ `counts_toward_content_max` ≤ `max_contents`
   - Σ `counts_toward_on_the_side_max` ≤ `max_on_the_side_customizations`
   - `n_extra` = count of `ExtraPortion` / `ExtraProtein` selections ≤ `max_extras`
   - `n_half` = count of `HalfPortion` selections ≤ `max_halfs`
   - `n_extra + n_half` ≤ `max_extras_plus_halfs` — the "one second protein" rule of §1.3,
     and the one cap here whose reading is inferred rather than published
10. Delivery drafts: every item is `eligibleForDelivery`, and every price on the card is the
    `unit_delivery_price`. Mixing the two price columns on one card is a wrong total, not a
    rounding difference.
11. The draft is marked confirmed for the bound session, and has not expired.
    **[INVENTED: draft TTL]** — see §10.
12. The spend cap for the session has room (`cc-fv1`).

**Pricing.** `Σ over lines ( base price + Σ selection prices ) × quantity`, all from
`item_prices` at this `restaurant_id`, at the `unit_price` or `unit_delivery_price` column
the order type selects. Defaults (`is_default` true) are priced at whatever `item_prices`
says, which is $0.00 for all 21 of them, and are shown on the card whether or not the
visitor named them.

**Receipt.** `order_id`, store (id, name, address), order type, every line with its
selections and portions, per-line and order totals, the `harvested_at` of the price rows
used, and the simulation notice.

**Rejections** are typed and name the rule: `ITEM_NOT_ORDERABLE`, `ITEM_UNAVAILABLE_AT_STORE`,
`QUANTITY_EXCEEDS_MAX`, `REQUIRED_SLOT_EMPTY`, `SLOT_OVERFILLED`, `MODIFIER_NOT_OFFERED`,
`PORTION_NOT_OFFERED`, `CAP_EXCEEDED`, `NOT_ELIGIBLE_FOR_DELIVERY`, `DRAFT_NOT_CONFIRMED`,
`DRAFT_EXPIRED`, `BUDGET_EXCEEDED`.

### 7.2 `cancel_order(order_id) → receipt`

Read §3 first. This tool models something the real product does not do.

**Argument.** `order_id` — an order the bound visitor placed, and was shown.

**Validation.**

1. `order_id` exists and belongs to the bound visitor. Otherwise `ORDER_NOT_FOUND` — the
   same answer for someone else's valid id.
2. The order's `status` is `pending`. Any other status is `ORDER_NOT_CANCELLABLE`.
3. The order was placed within the cancellation window. **[INVENTED: the window]** — see
   §10. Outside it, `CANCELLATION_WINDOW_CLOSED`, and the copy explains that the real
   product's window is zero.
4. Confirmed for the session; the spend cap is irrelevant here but the action is still
   rate-limited under PRD S3.

**Effect on the ledger.** Cancelling reverses the points the order earned. This is the one
part of `cancel_order` that is *not* invented: the terms reserve Chipotle's right to deduct
the points from a qualifying purchase that is later voided or cancelled. The reversal is a
`loyalty_ledger` row with a negative `delta` and a reason naming the order, not an edit to
the original row, so #27's reconciliation still sees an append-only ledger.

**Receipt.** `order_id`, new status, what was cancelled, the points reversed and the
resulting balance, and — required, not optional — a sentence saying that Chipotle does not
normally allow this.

### 7.3 `redeem_points(reward_id) → receipt, new balance`

**Argument.** `reward_id`.

**`reward_id` is ours, and that has to be said.** The published catalogue has a name, a point
cost and an image path, and no identifier — `rewards.reward_id` is null on all eight rows
because #21 refused to invent one at harvest time. V0 mints a stable slug per published
reward (`side-tortilla`, `chips`, `fountain-drink`, `guac`, `double-protein`,
`half-off-entree`, `entree`, `entree-and-chips`), keyed to the published `name` and
`position`. **[INVENTED: the ids, and the reward → menu-item mapping]** — see §10.

**Validation.**

1. `reward_id` names a row in the current `rewards` table. A reward that has left the
   catalogue since the visitor last looked is `REWARD_UNAVAILABLE` — the terms require this
   check to happen at redemption time.
2. The bound visitor's balance from `loyalty_ledger` is `≥ point_cost`. Otherwise
   `INSUFFICIENT_POINTS`, and the answer says how many are missing, which is a genuinely
   useful thing for P3 to volunteer.
3. `point_cost` is re-read at redemption, not taken from the card. The terms let Chipotle
   change it at any time; if it moved since the card was rendered, the call is rejected as
   `REWARD_COST_CHANGED` and re-proposed rather than silently charged the new price.
4. Confirmed for the session. This is the confirmation that matters most, because there is no
   undo.

**Effect.** Two writes: a `loyalty_ledger` row with a negative `delta` equal to `point_cost`
and a reason naming the reward, and a minted Reward on the account with an expiry 60 days
out, per the terms.

**Receipt.** The reward's published name, the points deducted, the new balance, the expiry
date, and — because this is the difference between mirroring the product and inventing one —
a plain statement that the reward is now on the account and will be applied to a future
order, that only one reward may be used per order, and that redemption cannot be undone.

**Not covered by this tool:** nonprofit donations, birthday and welcome rewards, Extras
challenges, and promo codes. All published, all in §5.

### 7.4 `update_preferences(prefs) → acknowledgement`

**Argument.** `prefs`, a partial object over exactly the three editable fields of
[`decisions/persona-editing.md`](decisions/persona-editing.md). Absent keys are unchanged;
an explicit null clears.

```
prefs.display_name           string
prefs.home_store             store_id
prefs.stated_preferences[]   { modifier_item_id, stance }
                             stance ∈ { always, never, light, extra, side }
```

The five stances are not a design; four of them are the published portion vocabulary of §1.3
(`Half` is deliberately absent — it exists only on rice and beans and is a choice made per
order, not a standing one), and `never`/`always` are the presence axis the slot grammar
already has in `No Rice` and `No Beans`.

**Validation.**

1. `display_name` is 1–40 characters after trimming, and is rendered as text, never as
   markup. **[INVENTED: the length]** — see §10.
2. `home_store` is a `store_id` in `stores`. It changes where the *next* order is priced and
   nothing about past orders; where it disagrees with `customer_360.favourite_store`, the
   serving layer says so rather than reconciling silently — that is settled in the persona
   decision and repeated here only because the ops API is where it is enforced.
3. Every `modifier_item_id` in `stated_preferences` is a real modifier — a row in
   `menu_items`, appearing at least once in `modifiers`. The 72 modifier items are the whole
   vocabulary. **This is the rule that makes the preference actionable**: a preference that
   cannot name a modifier cannot be stored, which is the closed-vocabulary consequence of the
   product's own refusal of free-text instructions (§4).
4. `stance` is one of the five values, and a portion stance is only valid where
   `portion_options` permits that portion for the modifier on at least one item. Checked
   against the harvest: guacamole and queso admit `side` and nothing else; cheese, romaine and
   fajita veggies admit `light` and `extra` but not `side`; rice and beans admit all four;
   cilantro lime sauce and the vinaigrette admit none, so they are `always`/`never` only. A
   `light` stance on guacamole is rejected, because no item on the menu offers light
   guacamole.
5. At most 20 preference entries. **[INVENTED: the ceiling]** — see §10.
6. Confirmed for the session, like any other write (PRD T2).

**How a preference is applied**, which belongs here because it is a validation concern in
disguise: preferences **filter and annotate; they never rewrite a derived value.** A `never`
stance on cheese removes cheese from a *proposed* draft and says out loud that it did; it does
not change what `usual_order` reports the visitor's usual to be. The persona decision fixes
this and the copy example it gives is the one to use.

**Not an allergen check.** The acknowledgement says so. See §4.

**Rejections:** `NAME_TOO_LONG`, `STORE_NOT_FOUND`, `MODIFIER_NOT_RECOGNISED`,
`STANCE_NOT_AVAILABLE_FOR_MODIFIER`, `TOO_MANY_PREFERENCES`.

---

## 8. What this changes elsewhere

**RFC-001 §06 — the tool table is unchanged.** Four write tools, same names, same arguments,
same confirmation column. This document fills in what "arguments" means for each and adds a
pointer from §06 to here, in the same commit, per the convention in
[`docs/README.md`](README.md).

**Two semantic pins the RFC left implicit**, both recorded above rather than as schema changes:

- `redeem_points` returns *"Receipt and new balance"*, which is accurate but reads as though
  redemption were terminal. It is not: it mints a Reward with a 60-day life (§2.2, §7.3).
- `cancel_order` models an affordance the real product does not offer (§3). The signature is
  unchanged; the honesty requirement is new and lives in the receipt copy.

**[#24](https://github.com/gganssle/chip_chat/issues/24) `menu_catalog`** inherits §1.3 as its
requirement: the catalogue must keep slots, per-pair portion permissions and the six per-item
caps, because rules 6–9 of §7.1 cannot be evaluated without them. A flattened item-plus-
toppings table would make most of this document unenforceable.

**[#46](https://github.com/gganssle/chip_chat/issues/46) stored procedures** implements §7 —
one procedure per write tool, each rejecting rather than repairing, each returning a typed
rejection code from the lists above.

> **Landed, and where it lands short.** `snowflake/sql/12_procedures.sql` and
> `snowflake/sql/13_cancel_order.sql`, all four `EXECUTE AS CALLER`, none of them taking a
> visitor identifier, each idempotent on a retry key. §7.1's rules 1, 2, 3, 4 and 7 and the
> pricing are enforced at the database, which is what #46 asks for by name. Rules **6, 8 and
> 9** — required slots, per-pair portions, the six caps — and §7.4's rule 4 are **not**, and
> cannot be: the serving projection of the catalogue carries none of the columns they are
> about (`CHIP_CHAT.CATALOGUE.modifiers` is five columns and there is no `portion_options`
> table). They are enforced at proposal time in `api/drafts.py` against `chip_chat.catalog`,
> which does carry them. Rules 11 and 12 are the ops API's by design. The gap is recorded as
> data in `chip_chat.snowflake.procedures.ENFORCED_ELSEWHERE` and tracked as a bead.
>
> `cancel_order` is in a file of its own, so that the exit §10 row 1 records stays a
> deletion: three deletions, one `DROP PROCEDURE`, and no migration. Its receipt carries both
> published sentences — that Chipotle cannot normally cancel a submitted order, and that the
> real delivery path is Customer Service and possibly a cancelation fee.

**[#27](https://github.com/gganssle/chip_chat/issues/27) loyalty ledger** gains two writers
from this document: `redeem_points` (negative delta, reason names the reward) and
`cancel_order` (negative delta reversing an order's earnings, per the terms). Both append.

---

---

## 9. Where every fact came from

| Section | Table | Document |
| --- | --- | --- |
| §1.1–1.4 | `menu_items`, `item_prices`, `modifier_groups`, `modifiers`, `portion_options` | `services.chipotle.com/menuinnovation/v1/restaurants/0679/onlinemenu` |
| §1.1, §3, §4, §5 | `faq_entries` | the persisted GraphQL FAQ query behind `chipotle.com/contact-us` |
| §2.1 | `rewards` | `chipotle.com/rewards` |
| §2.2–2.3 | `policy_sections` | `chipotle.com/rewards-terms` |
| §5 | `catering_packages`, `catering_package_options` | `services.chipotle.com/cateringorder/v1/menu/tiered` |
| §7.1, §7.4 | `stores`, `store_profiles` | `locations.chipotle.com`, `restaurant/v3/restaurant` |

Every row in every one of those tables carries `source_url` and `harvested_at`. The figures
in this document are from a harvest run on **26 August 2026**; prices and availability are
per restaurant **0679** and are the most volatile thing here.

---

## 10. Everything invented, in one list

Issue #23's third acceptance criterion. Nothing outside this list is invented, and everything
in it is here because the published record does not answer the question.

| # | Invented | Why it had to be | Smallest way to remove it |
| --- | --- | --- | --- |
| 1 | **The cancellation window** in `cancel_order` (§7.2 rule 3). Proposed: orders stay `pending` for a fixed simulated interval after placement and are cancellable until it lapses. | PRD T1 requires the action; the product's real window is zero (§3). | A PRD change dropping T1's cancellation clause. Then the tool goes too. |
| 2 | **`reward_id`** — a stable slug per published reward (§7.3). | The published catalogue has no identifier at all. | Nothing public to replace it with; the signed-in Rewards Exchange is not accessible. |
| 3 | **The reward → menu-item mapping** — what "ENTRÉE" or "SIDE TORTILLA" entitles you to (§2.4). | The published record deliberately does not say, and the image paths mislead. | Same as #2. Until then, state the mapping in the demo's own data with a comment saying it is ours. |
| 4 | **The `stated_preferences` vocabulary** — `{always, never, light, extra}` over modifier ids (§7.4). | Real Chipotle persists communication opt-ins, not food preferences (§4). PRD T1 requires the action anyway. | None available; this is the closest thing to the product, since it is drawn from the real modifier taxonomy and refuses what the product refuses. |
| 5 | **Numeric ceilings**: display name 40 characters, 20 preference entries, the draft TTL. | Ordinary product limits nobody publishes. | Not worth removing; they are stated so a reviewer can argue with the numbers. |
| 6 | **"The 50 harvested stores are the world"** (§7.1 rule 1). | #21 harvested 50 of ~3,800 by design; see [`decisions/store-selection.md`](decisions/store-selection.md). | Harvest more. It is a parameter, not a schema change. |
| 7 | **The reading of `max_extras_plus_halfs`** as "one second protein, extra or half, not both" (§1.3). | The column is published; its interaction is not documented. The reading is consistent across all 65 entrées. | Walk the ordering UI and try to add both. |

---

## 11. What was not checked

- **The ordering SPA itself.** `chipotle.com/order/*` is a client-rendered shell — #21 found
  the same thing — and three attempts to drive it in a browser timed out before the page
  became readable. Everything in §1 therefore comes from the endpoints that flow feeds on,
  which is the more stable source but is not the same as watching the flow. Specifically
  unverified: whether a pickup time can be chosen, whether the UI enforces
  `max_extras_plus_halfs` the way §1.3 reads it, and how group ordering is entered.
- **The signed-in surfaces.** The Rewards Exchange, order history, the account preferences
  screen and the reorder affordance are all behind a login this project does not have and
  does not want. Everything in §2 and §4 comes from public pages and the terms.
- **Prices at any store but 0679.** Per [`decisions/menu-pricing.md`](decisions/menu-pricing.md),
  and the reason the modifier prices in §1.3 are labelled with a restaurant.
- **Most of the 136 FAQ answers.** The Rewards Program category was read through, along with
  every answer this document quotes and every answer matching a search over cancellation,
  refunds, modification, scheduling, group ordering, preferences, points and redemption. The
  remainder were not read. A published rule stated only in an unread answer would have been
  missed — the risk is small and it is not zero.
