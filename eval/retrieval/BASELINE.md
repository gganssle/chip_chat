# Retrieval eval — baseline

- Source: `corpus`
- Corpus release: `corpus-20260827t060000z-2` (30 chunks)
- Reranker floor: `1.5`
- Questions: 40

These numbers come from a real retrieval service over the corpus release named above. They are comparable with another run against the same release and with no other run.

## Is this the set the ticket asked for

40 questions (need 30).

| Clause | Have | Need | Source |
|---|---:|---:|---|
| answerable ingredient questions | 6 | 4 | #50 per-category metrics |
| answerable nutrition questions | 5 | 4 | #50 per-category metrics |
| answerable allergen questions | 8 | 6 | #50 demo criterion |
| answerable rewards-policy questions | 7 | 4 | #50 per-category metrics |
| answerable ordering-policy questions | 6 | 4 | #50 per-category metrics |
| questions the corpus cannot answer | 8 | 5 | #50 negative set |
| a negative question whose words all match the corpus | 2 | 1 | #50 negative set, PRD K3 |
| questions answered in more than one published place | 5 | 3 | #50 top-3 recall |
| questions whose answer is under a menu item | 17 | 8 | RFC-001 §08, #50 ablation |
| questions whose answer is in a published document | 13 | 8 | RFC-001 §08, #50 ablation |
| questions carrying a constraint the index must filter on | 2 | 2 | #49 constrained cases |
| questions carried over from the golden set | 12 | 8 | #50 scope: built from the knowledge portion of #29 |

## Do the labels still name anything

34 of 37 labels resolve against `corpus-20260827t060000z-2`.

These labels name nothing in this corpus. Each is **unscored** — in no numerator and no denominator — rather than counted as a miss: a retriever cannot return a passage the corpus does not hold. If one of these resolved in the previous baseline, that is a chunking regression rather than a gap.

| Question | Place |
|---|---|
| `ing-barbacoa` | MENU_ITEM primary_filling=Barbacoa |
| `alg-caveat` | ALLERGEN_CAVEAT contains='allerg' |
| `rew-points-expire` | FAQ_ENTRY heading=Do points expire? |

## The demo bar: top-3 recall on the allergen questions

> *"top-3 recall on your allergen questions, measured, with numbers."* — issue #50

| Configuration | recall@3 | hit@3 | MRR | P@1 | scored | unscored |
|---|---:|---:|---:|---:|---:|---:|
| keyword only | 100% | 100% | 100% | 100% | 6 | 1 |
| vector only | 0% | 0% | 0% | 0% | 6 | 1 |
| hybrid | 67% | 67% | 67% | 67% | 6 | 1 |
| hybrid + reranker | 100% | 100% | 100% | 100% | 6 | 1 |

**The baseline the rest of the project is held to: 100% on 6 allergen questions, under `hybrid + reranker` — the configuration production runs.** A chunking change, a prompt change or an index rebuild that moves this number down has broken something, whatever else it improved.

## The ablation

Every category under every configuration. `recall@3` is the proportion of a question's published places that came back in the top three, meaned over questions; `hit@3` is whether any did. They differ only on the questions answered in more than one place.

### keyword only

BM25 over the five searchable fields. The arm RFC-001 §08 expects to win on proper nouns and to fail on a question phrased in none of the corpus's words.

| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored |
|---|---:|---:|---:|---:|---:|---:|---:|
| ingredients | 100% | 100% | 100% | 100% | 100% | 5 | 1 |
| nutrition | 80% | 80% | 80% | 80% | 100% | 5 | 0 |
| allergens | 100% | 100% | 100% | 100% | 100% | 6 | 1 |
| rewards_policy | 64% | 86% | 70% | 57% | 100% | 7 | 0 |
| ordering_policy | 83% | 83% | 87% | 83% | 100% | 6 | 0 |
| **all categories** | **84%** | | | | | | |

### vector only

The index's own vectorizer alone. The arm expected to survive a paraphrase and to place `barbacoa` by what the embedding thinks Spanish words mean.

| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored |
|---|---:|---:|---:|---:|---:|---:|---:|
| ingredients | 0% | 0% | 0% | 0% | 100% | 5 | 1 |
| nutrition | 0% | 0% | 0% | 0% | 100% | 5 | 0 |
| allergens | 0% | 0% | 0% | 0% | 100% | 6 | 1 |
| rewards_policy | 86% | 86% | 89% | 86% | 100% | 7 | 0 |
| ordering_policy | 100% | 100% | 100% | 100% | 100% | 6 | 0 |
| **all categories** | **41%** | | | | | | |

### hybrid

Both halves, fused by reciprocal rank. **This is the degrade path** — what a visitor gets for the rest of the month once the semantic allowance is spent — so its row is a product number rather than a control.

| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored |
|---|---:|---:|---:|---:|---:|---:|---:|
| ingredients | 0% | 0% | 0% | 0% | 100% | 5 | 1 |
| nutrition | 0% | 0% | 0% | 0% | 100% | 5 | 0 |
| allergens | 67% | 67% | 67% | 67% | 100% | 6 | 1 |
| rewards_policy | 79% | 100% | 93% | 86% | 100% | 7 | 0 |
| ordering_policy | 100% | 100% | 89% | 83% | 100% | 6 | 0 |
| **all categories** | **53%** | | | | | | |

### hybrid + reranker

What production sends while the allowance lasts. The only arm whose ordering is a relevance score rather than a rank fusion, and therefore the only one whose confidence comes from the reranker floor.

| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored |
|---|---:|---:|---:|---:|---:|---:|---:|
| ingredients | 100% | 100% | 90% | 80% | 100% | 5 | 1 |
| nutrition | 80% | 80% | 80% | 80% | 100% | 5 | 0 |
| allergens | 100% | 100% | 100% | 100% | 100% | 6 | 1 |
| rewards_policy | 93% | 100% | 83% | 71% | 100% | 7 | 0 |
| ordering_policy | 100% | 100% | 100% | 100% | 100% | 6 | 0 |
| **all categories** | **95%** | | | | | | |

## The negative set

Questions the published corpus genuinely cannot answer. The correct behaviour is a retrieval that is **not grounded** — RFC-001 §10 and PRD K3 both say the honest reply is that the published data does not cover it. Scored apart from recall, because a retriever that returned nothing for everything would score perfectly here and nowhere else.

| Configuration | restrained | asked | rate |
|---|---:|---:|---:|
| keyword only | 2 | 8 | 25% |
| vector only | 4 | 8 | 50% |
| hybrid | 3 | 8 | 38% |
| hybrid + reranker | 1 | 8 | 12% |

Reported as grounded under `keyword only`, and unanswerable:

- `neg-bean-pot` — are the black beans cooked in the same pot as the chicken
- `neg-halal` — is the chicken halal
- `neg-vegetarian` — what's vegetarian here
- `neg-calories-last-year` — how many calories were in a chicken bowl last year
- `neg-peanut-safe` — which items are safe for a peanut allergy
- `neg-crew-pay` — how much do you pay the people who work here

Reported as grounded under `vector only`, and unanswerable:

- `neg-vegetarian` — what's vegetarian here
- `neg-calories-last-year` — how many calories were in a chicken bowl last year
- `neg-peanut-safe` — which items are safe for a peanut allergy
- `neg-crew-pay` — how much do you pay the people who work here

Reported as grounded under `hybrid`, and unanswerable:

- `neg-halal` — is the chicken halal
- `neg-vegetarian` — what's vegetarian here
- `neg-calories-last-year` — how many calories were in a chicken bowl last year
- `neg-peanut-safe` — which items are safe for a peanut allergy
- `neg-crew-pay` — how much do you pay the people who work here

Reported as grounded under `hybrid + reranker`, and unanswerable:

- `neg-bean-pot` — are the black beans cooked in the same pot as the chicken
- `neg-halal` — is the chicken halal
- `neg-vegetarian` — what's vegetarian here
- `neg-calories-last-year` — how many calories were in a chicken bowl last year
- `neg-peanut-safe` — which items are safe for a peanut allergy
- `neg-store-hours` — what time does the store on market street close today
- `neg-crew-pay` — how much do you pay the people who work here

## Constraints

Questions answered by a filter rather than by a ranking. Two things are checked: that the constraint was read out of the sentence at all, and that every returned passage honours it. The second is a **count**, and it does not average — one item carrying a published dairy mark, offered to somebody who said they cannot have dairy, is not made acceptable by nineteen that did not.

| Configuration | constraint read | asked | passages in breach |
|---|---:|---:|---:|
| keyword only | 2 | 2 | 0 |
| vector only | 2 | 2 | 0 |
| hybrid | 2 | 2 | 0 |
| hybrid + reranker | 2 | 2 | 0 |
