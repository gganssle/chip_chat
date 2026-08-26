# Decision: food variety is measured against the catalogue, not counted

**Issue:** [#28](https://github.com/gganssle/chip_chat/issues/28) (bead `cc-aho`) · **Decided:** 26 August 2026
**Builds on:** [#25](https://github.com/gganssle/chip_chat/issues/25) (bead `cc-6fj`), [#26](https://github.com/gganssle/chip_chat/issues/26) (bead `cc-5jk`), [#27](https://github.com/gganssle/chip_chat/issues/27) (bead `cc-rjs`)
**Unblocks:** [#29](https://github.com/gganssle/chip_chat/issues/29) (the golden evaluation set), and Phase 2 generally
**The measurement itself:** [`docs/synthetic-population-texture.md`](../synthetic-population-texture.md)

---

Trap 1 in the system design is thin synthetic data. Issue #28 is the check that catches
it, and it is the gate on Phase 2 because thinness stops being cheap to fix the moment a
lakehouse is built on top of it.

Two merged tickets had already refused to make the claim, and both named this one.
Issue #25 shipped the generator saying that "proving the population is not thin against
a real catalogue is #28 — this catalogue has two entrees, so variety of *food* is not
assertable here; variety of *behaviour* is". Issue #26 said the same in four places about
the persona fixtures. Both refusals were correct. Together they left *"the population is
not thin"* asserted by nobody, deliberately, which is what this records the resolution to.

## The problem both tickets ran into

The committed fixture catalogue publishes **nine orderable things**: two entrees, one
rice, one bean, two toppings, one paid extra, one side, one drink. It exists so that #25's
generator and #54's matcher have something to resolve against on a laptop with no network,
and it is deliberately small.

That makes the obvious form of the check unwritable. *"At least twelve distinct entrees
were ordered"* fails against this catalogue for a reason that has nothing to do with the
generator, and passes against a real harvest even if the generator only ever composes
three baskets out of the six hundred available. It is a threshold that is wrong in both
directions: it fails honest work and it certifies dishonest work.

Three options were open:

| Option | Why not |
| --- | --- |
| Assert absolute counts, and skip the suite when the catalogue is small | A suite that skips is a suite that is skipped. The fixture catalogue is the one every machine and every CI run has, so "skip on small catalogues" means the check never runs anywhere it would be seen. |
| Commit a bigger fixture catalogue | A second harvest to keep true, and it moves the problem rather than solving it — the bound would then be tuned to *that* catalogue's size. |
| Measure relative to what the catalogue makes possible | Chosen. |

## The decision

**Every food-variety check is a coverage, a ratio, a share or an effect size — never a
count of foods.** The catalogue supplies the denominator, so the same bound means the same
thing at nine orderable items and at nine hundred.

The two claims are not the same claim, and the difference is the whole decision:

> *"Twelve different entrees were ordered."* — a claim about Chipotle's menu.
>
> *"Every entree the catalogue publishes was ordered by somebody."* — a claim about this
> generator.

The second one holds against the fixture, holds against a real harvest, and **bites** on a
real harvest in the case the first one misses: a generator reaching three of six hundred
items scores 0.005 and stops the run.

So `item_coverage` is ordered-over-orderable, `protein_coverage` is chosen-over-published,
and `allergen_state_coverage` is exercised-over-carried. The report prints the ceiling
beside each of them, so that where the catalogue is the limit the document says the
catalogue is the limit rather than crediting the generator for it.

## Where the long tail went

Issue #28 asks that "item popularity has a long tail, so `item_affinity` has something to
learn". Measured on the aggregate popularity curve that is not checkable here: the
catalogue publishes nine orderable things and the published menu makes two of them
compulsory — a bowl always has rice — so the curve's shape is set by the harvest whatever
the customers do. The shipped population's Gini over item popularity is 0.24, and it would
be 0.24 for any generator at all.

What a recommender actually learns is not the aggregate curve but the **gap between one
customer's mix and everybody's**, and that gap is scale-free. So `palate_divergence`
measures the median Jensen–Shannon divergence of a customer's item mix from the
population's. It is zero exactly when everyone orders the population average — which is
the thin population the check exists to catch — and the shipped population scores 0.049
bits with a widest of 0.177. The Gini is reported beside it, labelled as capped by the
catalogue.

## Personas: the check the others cannot substitute for

The bead's framing is sharper than the ticket's: *"show that personas are actually
distinguishable from each other rather than three labels on the same distribution."*

A population can have a wide spread on every distribution above and still be one
undifferentiated blob with seven names written on it, which is a demo where switching
persona changes nothing a visitor can see. Spread does not imply separation, so it is
measured directly.

`persona_separation` scores every one of the twenty-one archetype pairs with **Cliff's
delta** — the probability that a customer drawn from one archetype outranks one drawn from
the other, minus the reverse — on the best of ten measured facts, and reports the *worst*
pair. Cliff's delta rather than a difference of means because it is scale-free and assumes
nothing about either distribution's shape, which is the thing under test. The worst pair
rather than the average because averaging lets twenty good pairs hide a twenty-first that
is indistinguishable.

**The shipped population separates every pair completely: delta 1.00, twenty-one pairs of
twenty-one.** The bound is 0.80 — a large effect, with headroom for a retune.

`test_texture_suite.py` asserts the check can fail, by reshuffling `persona_id` across
customers while leaving every history untouched. That population has identical
distributions to the shipped one and scores below the bound, which is the demonstration
that the check measures separation rather than spread.

## Why the gate is inside the generator

`generate_population` calls `check_texture` and raises `ThinPopulationError` rather than
returning. The ticket asks that the suite "runs on every generation and fails the build on
a degenerate distribution", and the two obvious alternatives both fail that.

A test in the suite checks whatever the fixtures generate, not what a real run against a
real harvest produced. A separate command checks whatever somebody last remembered to
check. Only a call inside the generator makes the claim about **the run that wrote the
files**, which is the run a demo will be given from.

There is no flag to skip it. A thin population is the one failure in this package that
produces no error of its own — it generates, prices, writes, and passes referential
integrity — so it would be discovered a phase later by somebody reading gold marts.

## The bounds are config, and every check must carry one

`[texture]` sits in `population.toml` beside `[personas]`, for the reason PRD §09 gives:
account realism is bounded by the generator, which makes what counts as enough variety a
product decision rather than a statistical one. *"At least ten per cent of customers have
no usual order"* is a statement about the demo.

`config.TEXTURE_CHECKS` is closed, and the reader refuses two things rather than one:

- a bound naming a check nothing measures — the ordinary misspelling;
- a check the file leaves **unbounded** — which is worse, because the run still prints
  nineteen checks and eighteen of them still pass, so the population ends up certified by
  a suite with a hole in it.

## Findings worth recording

**The allergen unknown path is not exercised, and that is the catalogue's doing.** The
catalogue models three allergen states and `NOT_LISTED` explicitly does not mean "does not
contain", so a population that only ever ordered items with published allergen data would
look healthy on every variety count while never putting the honest *"Chipotle does not
publish this"* answer in front of the assistant. The fixture catalogue publishes allergen
data for **every** orderable item; its one `NOT_PUBLISHED` row is `Napkins & Utensils`,
which `[catalogue].excluded_categories` keeps out of baskets. So the population exercises
one state of one available, scores 1.00, and the report names the state. Against a real
harvest carrying unpublished foods, the same check requires them to be ordered.

**The largest unclaimed balance now belongs to an Office Manager who ordered yesterday.**
Before `cc-5si` removed an invented one-redemption-per-visit cap, that superlative belonged
to a Lapsed Regular. It is a real consequence of a real fix, and it is the kind of thing a
committed, regenerated report surfaces and a hand-written list of interesting customers
does not.

**Perfect separation is a finding, not just a pass.** Twenty-one pairs at delta 1.00 says
the archetypes as tuned are *completely* disjoint on at least one measure each. That is
more than the demo needs and is worth knowing: it means a retune has a long way to fall
before the personas stop being distinguishable, and it also means the population is, on
these measures, tidier than a real one would be. Nothing downstream depends on the
untidiness, so it is recorded rather than acted on.

## What this does not claim

**That the population is varied in *food* against a real harvest.** It claims the
generator reaches everything the catalogue offers, whatever the catalogue offers, and that
the claim is checked on every run — including the first run against a real harvest, which
will fail loudly if it does not hold. Nobody has run it against a real harvest yet, and
this document does not pretend otherwise.

**That `usual_share` is the `usual_order` mart's `confidence`.** It is not, for the reason
`docs/decisions/persona-fixtures.md` gives, and `[texture]`'s two windows
(`no_usual_above`, `strong_usual_above`) are this document's thresholds rather than that
mart's.
