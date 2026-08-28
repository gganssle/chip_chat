# Retrieval eval — baseline

- Source: `corpus`
- Corpus release: `corpus-20260827t053000z-2` (31 chunks)
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

35 of 37 labels resolve against `corpus-20260827t053000z-2`.

These labels name nothing in this corpus. Each is **unscored** — in no numerator and no denominator — rather than counted as a miss: a retriever cannot return a passage the corpus does not hold. If one of these resolved in the previous baseline, that is a chunking regression rather than a gap.

| Question | Place |
|---|---|
| `ing-barbacoa` | MENU_ITEM primary_filling=Barbacoa |
| `alg-caveat` | ALLERGEN_CAVEAT contains='allerg' |

## Did the vector half actually run

Azure AI Search's Free tier drops the vector half of a query and returns HTTP 200 with an empty vector result, so a hybrid response that is silently lexical-only is well formed and looks exactly like a hybrid one. The tell is arithmetic: reciprocal rank fusion gives a document placed by one ranker at most `1/60`, so a result set with no score above that was found by one half. See `docs/retrieval.md` §9 and `chip_chat.search.fusion`.

| Configuration | questions | vector half dropped | comparable |
|---|---:|---:|---|
| keyword only | 40 | 0 | yes |
| vector only | 40 | 8 | **NO** |
| hybrid | 40 | 13 | **NO** |
| hybrid + reranker | 40 | 12 | **NO** |

**The arms marked NO are not measurements and their rows are not comparable with anything.** Each degraded question is **unscored** — in no numerator and no denominator, exactly like a label this corpus does not hold — because the retriever was not asked the question the arm's name says it was asked. What a degraded `hybrid` row would show is the `keyword only` row, and that is how three previous sweeps came to report a service fault as a finding about embeddings.

Re-run the sweep against a rested service. The rate climbs with query volume within a run, so an arm that degraded late did not degrade randomly — `make search-vector-arm` measures the rate before a sweep is worth spending.

Degraded under `vector only`:

- `neg-vegetarian` — what's vegetarian here
- `nut-chicken-bowl-calories` — how many calories are in a chicken bowl
- `neg-calories-last-year` — how many calories were in a chicken bowl last year
- `alg-white-rice` — does the white rice have anything in it i should know about
- `neg-peanut-safe` — which items are safe for a peanut allergy
- `rew-accumulating` — which purchases earn points
- `ord-refund-delivery` — how do i get a refund on a delivery order
- `neg-store-hours` — what time does the store on market street close today

Degraded under `hybrid`:

- `ing-bowl-contents` — what's actually in a burrito bowl
- `ing-guacamole` — do you have guacamole
- `ing-barbacoa` — what is barbacoa
- `neg-bean-pot` — are the black beans cooked in the same pot as the chicken
- `nut-chicken-bowl-calories` — how many calories are in a chicken bowl
- `nut-guacamole-calories` — how many calories does guac add
- `alg-guacamole-dairy` — is the guacamole dairy free
- `alg-no-dairy` — what can i get without any dairy
- `alg-no-dairy-bowl` — can i get a bowl with no dairy
- `neg-gluten-cross-contact` — is the kitchen free of gluten cross contact
- `rew-redeem` — what can i redeem my points for
- `ord-cancel-online` — can i cancel an order after i've placed it
- `ord-catering-contact` — who do i talk to about a catering order i already placed

Degraded under `hybrid + reranker`:

- `ing-barbacoa` — what is barbacoa
- `neg-halal` — is the chicken halal
- `neg-vegetarian` — what's vegetarian here
- `alg-white-rice` — does the white rice have anything in it i should know about
- `alg-steak-soy` — will the steak be safe for my severe soy allergy
- `alg-no-dairy` — what can i get without any dairy
- `alg-caveat` — how do i know if something is safe with my allergy
- `rew-eligibility` — who is allowed to join chipotle rewards
- `rew-accumulating` — which purchases earn points
- `ord-cancel-delivery` — can i cancel a delivery once it's on its way
- `ord-gift-card` — can i get cash back for the balance on my gift card
- `neg-store-hours` — what time does the store on market street close today

## The demo bar: top-3 recall on the allergen questions

> *"top-3 recall on your allergen questions, measured, with numbers."* — issue #50

| Configuration | recall@3 | hit@3 | MRR | P@1 | scored | unscored |
|---|---:|---:|---:|---:|---:|---:|
| keyword only | 100% | 100% | 100% | 100% | 6 | 1 |
| vector only | 80% | 80% | 80% | 80% | 5 | 2 |
| hybrid | 100% | 100% | 100% | 100% | 4 | 3 |
| hybrid + reranker | 100% | 100% | 100% | 100% | 4 | 3 |

⚠ The vector half was dropped on 3 of these questions under `hybrid + reranker`, so the number above is taken over the 4 that ran the configuration production actually sends. It is not the baseline; re-run it.

**The baseline the rest of the project is held to: 100% on 4 allergen questions, under `hybrid + reranker` — the configuration production runs.** A chunking change, a prompt change or an index rebuild that moves this number down has broken something, whatever else it improved.

## The ablation

Every category under every configuration. `recall@3` is the proportion of a question's published places that came back in the top three, meaned over questions; `hit@3` is whether any did. They differ only on the questions answered in more than one place.

### keyword only

BM25 over the five searchable fields. The arm RFC-001 §08 expects to win on proper nouns and to fail on a question phrased in none of the corpus's words.

| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored | degraded |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ingredients | 100% | 100% | 100% | 100% | 100% | 5 | 1 | 0 |
| nutrition | 80% | 80% | 80% | 80% | 100% | 5 | 0 | 0 |
| allergens | 100% | 100% | 100% | 100% | 100% | 6 | 1 | 0 |
| rewards_policy | 62% | 86% | 80% | 71% | 100% | 7 | 0 | 0 |
| ordering_policy | 83% | 83% | 83% | 83% | 100% | 6 | 0 | 0 |
| **all categories** | **84%** | | | | | | | |

### vector only

The index's own vectorizer alone. The arm expected to survive a paraphrase and to place `barbacoa` by what the embedding thinks Spanish words mean.

> **Not a measurement of `vector only`.** The service dropped the vector half on 8 of 40 questions, so this arm ran two different configurations and the degraded ones are unscored. Read the section above before any cell below.

| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored | degraded |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ingredients | 100% | 100% | 100% | 100% | 100% | 5 | 1 | 0 |
| nutrition | 100% | 100% | 100% | 100% | 100% | 4 | 1 | 1 |
| allergens | 80% | 80% | 80% | 80% | 100% | 5 | 2 | 1 |
| rewards_policy | 83% | 83% | 87% | 83% | 100% | 6 | 1 | 1 |
| ordering_policy | 100% | 100% | 100% | 100% | 100% | 5 | 1 | 1 |
| **all categories** | **92%** | | | | | | | |

### hybrid

Both halves, fused by reciprocal rank. **This is the degrade path** — what a visitor gets for the rest of the month once the semantic allowance is spent — so its row is a product number rather than a control.

> **Not a measurement of `hybrid`.** The service dropped the vector half on 13 of 40 questions, so this arm ran two different configurations and the degraded ones are unscored. Read the section above before any cell below.

| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored | degraded |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ingredients | 100% | 100% | 100% | 100% | 100% | 3 | 3 | 3 |
| nutrition | 100% | 100% | 100% | 100% | 100% | 3 | 2 | 2 |
| allergens | 100% | 100% | 100% | 100% | 100% | 4 | 3 | 2 |
| rewards_policy | 86% | 100% | 83% | 67% | 100% | 6 | 1 | 1 |
| ordering_policy | 100% | 100% | 83% | 75% | 100% | 4 | 2 | 2 |
| **all categories** | **96%** | | | | | | | |

### hybrid + reranker

What production sends while the allowance lasts. The only arm whose ordering is a relevance score rather than a rank fusion, and therefore the only one whose confidence comes from the reranker floor.

> **Not a measurement of `hybrid + reranker`.** The service dropped the vector half on 12 of 40 questions, so this arm ran two different configurations and the degraded ones are unscored. Read the section above before any cell below.

| Category | recall@3 | hit@3 | MRR | P@1 | ceiling | scored | unscored | degraded |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ingredients | 80% | 80% | 84% | 80% | 100% | 5 | 1 | 1 |
| nutrition | 100% | 100% | 90% | 80% | 100% | 5 | 0 | 0 |
| allergens | 100% | 100% | 100% | 100% | 100% | 4 | 3 | 3 |
| rewards_policy | 90% | 100% | 90% | 80% | 100% | 5 | 2 | 2 |
| ordering_policy | 100% | 100% | 100% | 100% | 100% | 4 | 2 | 2 |
| **all categories** | **93%** | | | | | | | |

## The negative set

Questions the published corpus genuinely cannot answer. The correct behaviour is a retrieval that is **not grounded** — RFC-001 §10 and PRD K3 both say the honest reply is that the published data does not cover it. Scored apart from recall, because a retriever that returned nothing for everything would score perfectly here and nowhere else.

| Configuration | restrained | scored | degraded | rate |
|---|---:|---:|---:|---:|
| keyword only | 2 | 8 | 0 | 25% |
| vector only | 1 | 4 | 4 | 25% |
| hybrid | 1 | 6 | 2 | 17% |
| hybrid + reranker | 1 | 5 | 3 | 20% |

The **degraded** column is the one to read first here, because restraint is the only metric in this document that a broken retriever makes look better: a retriever returning less is a retriever declining more. Those questions are unscored — neither restrained nor overconfident — since a lexical-only retriever did not look with half of itself, and both readings of that are unearned.

Reported as grounded under `keyword only`, and unanswerable:

- `neg-bean-pot` — are the black beans cooked in the same pot as the chicken
- `neg-halal` — is the chicken halal
- `neg-vegetarian` — what's vegetarian here
- `neg-calories-last-year` — how many calories were in a chicken bowl last year
- `neg-peanut-safe` — which items are safe for a peanut allergy
- `neg-crew-pay` — how much do you pay the people who work here

Reported as grounded under `vector only`, and unanswerable:

- `neg-bean-pot` — are the black beans cooked in the same pot as the chicken
- `neg-halal` — is the chicken halal
- `neg-crew-pay` — how much do you pay the people who work here

Reported as grounded under `hybrid`, and unanswerable:

- `neg-halal` — is the chicken halal
- `neg-vegetarian` — what's vegetarian here
- `neg-calories-last-year` — how many calories were in a chicken bowl last year
- `neg-peanut-safe` — which items are safe for a peanut allergy
- `neg-crew-pay` — how much do you pay the people who work here

Reported as grounded under `hybrid + reranker`, and unanswerable:

- `neg-bean-pot` — are the black beans cooked in the same pot as the chicken
- `neg-calories-last-year` — how many calories were in a chicken bowl last year
- `neg-peanut-safe` — which items are safe for a peanut allergy
- `neg-crew-pay` — how much do you pay the people who work here

## Constraints

Questions answered by a filter rather than by a ranking. Two things are checked: that the constraint was read out of the sentence at all, and that every returned passage honours it. The second is a **count**, and it does not average — one item carrying a published dairy mark, offered to somebody who said they cannot have dairy, is not made acceptable by nineteen that did not.

| Configuration | constraint read | asked | passages in breach |
|---|---:|---:|---:|
| keyword only | 2 | 2 | 0 |
| vector only | 2 | 2 | 0 |
| hybrid | 2 | 2 | 0 |
| hybrid + reranker | 2 | 2 | 0 |
