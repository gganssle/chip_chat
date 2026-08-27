# The labeled retrieval set

Issue [#50](https://github.com/gganssle/chip_chat/issues/50). Forty questions,
the published places that answer each one, and a sweep that runs them through the
retriever under four configurations with **no model anywhere in the loop**.

Read this before adding a question. [`BASELINE.md`](BASELINE.md) is the measured
run: `srch-chip-chat-4cy39i`, 2026-08-27, with the demo bar at **100% top-3
recall on the allergen questions** under the configuration production sends.

```bash
make retrieval-check      # free: is this the set the ticket asked for
make retrieval            # free: resolve the labels, sweep an in-memory index
make retrieval-baseline   # the measured sweep. Spends 40 of the month's 1,000
```

## Why this exists in this order

> *"Evaluate the retriever on its own before it ever touches the agent;
> retrieval bugs are nearly impossible to diagnose once a model is paraphrasing
> over them."*

That is the whole argument, and it is about **sequencing** rather than coverage.
A model handed three wrong passages writes a fluent, cited, confident answer from
them, and every signal downstream — task completion, groundedness, a human
reading the transcript — reports a plausible response. The retrieval failure is
invisible from there. It is trivially visible from here, where there is no model
to hide it.

So this package sits below `eval/golden`, not beside it. The golden set runs
whole turns and can say a menu question reached `search_menu_knowledge`; it
cannot say whether what came back was the right thing. This runs the retriever
directly and every number in it is a property of the index, the chunking and the
query construction — nothing here can be fixed or broken by a prompt.

## Three design decisions, and what each one is defending against

### A label names a place, not a chunk id

`chunk_id` is a content hash. Re-chunk the corpus and every id in it changes, so
a set keyed on ids would go uniformly, silently wrong on **exactly the change it
exists to detect** — #50's fourth acceptance criterion is *the ablation is
repeatable after any chunking change, so a chunking regression is caught
immediately*, and a set that dies on that change catches nothing.

A label is therefore a selector over published chunk metadata:

```json
{"kind": "MENU_ITEM", "item_id": "CMG-5252", "why": "Cheese's row carries the published dairy mark."}
{"kind": "POLICY_SECTION", "document_id": "rewards-terms", "heading": "ELIGIBILITY", "why": "..."}
{"kind": "POLICY_SECTION", "document_id": "rewards", "contains": "Every dollar spent", "why": "..."}
```

It names what the restaurant published, not how this repository sliced it.
`contains` is there for the passages with no heading — plenty of published policy
sections have none — where a distinctive published sentence is the only stable
handle. A *position* would not be one, and that is the point.

`test_the_labels_survive_a_rechunk` is this property under test: every chunk id
in the fixture is changed, and the resolution does not move.

### Recall is counted over labels, not over chunks

`recall@3` is *how many of a question's published places came back in the top
three*. One label is one place. The alternative — count chunks — fails twice: a
label like `item_type=Bowl` resolves to every bowl row, so one question would put
a nine-row denominator into the category mean; and any question with more than
three relevant chunks would carry a ceiling below 1.0 that reads as a failing
score.

This has one failure mode of its own, and it is worth stating: a label matching
nine interchangeable rows is satisfied by any one of them. That is the correct
reading of *"what beans do you serve"* and the wrong reading of a question where
all nine were genuinely needed. The set holds none of the latter, and the
coverage clauses are where that stays true.

`hit@3` is printed beside `recall@3` and neither subsumes the other. They differ
only on the questions answered in more than one place, and where they differ they
say different true things: for *"which has fewer calories, the chicken bowl or
the steak burrito"* a hit is a half-answer; for *"can I cancel an order"*, which
two published FAQ entries answer about two channels, a hit is enough and both is
better.

### Unscored is a third verdict, and it is load-bearing

A label the corpus under test does not hold is **unscored** — in no numerator, in
no denominator, printed by name above the rates. A retriever cannot return a
passage that is not there, and scoring it as a miss would blame a model for a
harvest.

Two of the set's labels are unresolved against the committed 31-chunk corpus
fixture, on purpose. `ing-barbacoa` names the item RFC-001 §08 uses as *its own*
example of a token embeddings place badly; `alg-caveat` names Chipotle's
published allergen caveat, which is a chunk kind of its own in the schema. The
fixture is a slice of the published pages and holds neither. That is a fact the
report prints rather than a hole in the set.

The measured run found a **third**, and it is the mechanism earning its keep. The
live index holds thirty of the fixture's thirty-one chunks — the one it is
missing is the FAQ entry *"Do points expire?"* — so `rew-points-expire`, which is
answered in three published places, is scored over the two the index actually
holds. Resolving against the export instead would have marked the retriever as
missing a passage nothing could have returned. That is why `--from-index` exists
and why a measured sweep uses it: what a resolution answers is *can the retriever
return this place*, and that is a question about the index.

**How a slice is told apart from a regression**: by diffing `BASELINE.md`. A
label that resolved in the committed baseline and does not resolve now is a
chunking regression, and it is one line of a diff. Nothing in the code can tell
the two apart from a single run, and pretending otherwise would be the fudge.

## The ablation

Four arms, in #50's own order. The first three vary which halves of the hybrid
query are sent; the fourth adds the semantic ranker on top of the third.

| Arm | What it is for |
|---|---|
| keyword only | RFC-001 §08's claim that *item names are proper nouns that embeddings handle poorly* |
| vector only | the other side of it: paraphrase, inside a policy document |
| hybrid | **the degrade path** — what a visitor gets for the rest of the month once the Free tier's semantic allowance is spent |
| hybrid + reranker | what production sends |

The interesting cell is never the aggregate. A hybrid that beats both halves
everywhere is a nice number; a hybrid that beats the vector half on menu rows and
the keyword half inside the rewards terms is RFC-001 §08's argument, confirmed.

The two single-half arms are not expressible by anything on the serving path, so
`chip_chat.search.query.Halves` was added for them — in the search package rather
than here, because an eval that built its own request bodies would be measuring a
copy of `chip_chat.search.query` rather than `chip_chat.search.query`. That
enum's docstring is the argument for its own existence and says who may pass it.

`hybrid` is a **product number**, not a control. It is already shipped, it is
already what happens past the ceiling, and it has never been scored — so the gap
between it and `hybrid + reranker` is what the fallback costs, known in advance
rather than discovered on the 1,001st request of the month.

## The negative set, and the constrained questions

Eight questions the published corpus genuinely cannot answer. They are **not** a
sixth category: a negative allergen question and a positive one are about the
same corpus surface and belong in the same part of the report, scored
differently. Correct behaviour is a retrieval that is not grounded, and it is
kept out of the recall tables because a retriever that returned nothing for
everything would otherwise score perfectly on half the set.

Two of them are shaped deliberately. *"Is the kitchen free of gluten cross
contact"* and *"which items are safe for a peanut allergy"* are questions every
word of which matches the corpus and none of which the corpus answers — a
negative set made entirely of *"how much do you pay your crew"* would measure
nothing, because any retriever refuses those.

Two questions carry a **constraint** instead of, or as well as, places. #49
answers *"what can I get without any dairy"* with an OData filter, and a filter
is a different mechanism from a ranking — so scoring it as a ranking question
would measure the wrong half of the design. Breaches are reported as a **count**
and never as a rate, for the reason the adversarial suite keeps its gates as
counts: one item carrying a published dairy mark, offered to somebody who said
they cannot have dairy, is not made acceptable by nineteen that did not.

## What the first measured sweep found

Against `srch-chip-chat-4cy39i`, index `corpus-20260827t060000z-2`, 2026-08-27.
[`BASELINE.md`](BASELINE.md) is the document; this is what is in it.

**The demo bar: 100% top-3 recall on the allergen questions**, under the
configuration production runs. That is the number the rest of the project is held
to — a chunking change, a prompt change or an index rebuild that moves it down
has broken something, whatever else it improved.

**RFC-001 §08's argument is confirmed, and sharply.** `recall@3`:

| category | keyword | vector | hybrid | hybrid + reranker |
|---|---:|---:|---:|---:|
| ingredients | 100% | 0% | 0% | 100% |
| nutrition | 80% | 0% | 0% | 80% |
| allergens | 100% | 0% | 67% | 100% |
| rewards_policy | 64% | 86% | 79% | 93% |
| ordering_policy | 83% | 100% | 100% | 100% |
| **all** | **84%** | **41%** | **53%** | **95%** |

*"Keyword recall matters here more than usual, because item names are proper
nouns that embeddings handle poorly."* The two halves come out exactly
complementary along the line the RFC drew: keyword-only is at 100% on the
menu-row categories and weakest inside the policy documents; vector-only is at
**zero** on all three menu-row categories and 86–100% on the two policy ones.
The design choice now has data under it rather than an argument.

**And the degrade path is worse than either half on menu questions.** `hybrid`
without the reranker scores 0% on ingredients and nutrition where keyword-only
alone scores 100% and 80%. Fusing a half that scores 0 with a half that scores
100 produces 0 — reciprocal rank fusion scores *rank*, the vector half
contributes `VECTOR_CANDIDATES = 50` neighbours to a 30-chunk corpus, and its
order crowds the keyword half's correct hits out of the five that come back. The
semantic ranker rescues it by reordering the union, which is why the column
beside it is at 100%.

That matters because `hybrid` **is** the product past the ceiling. It is what a
visitor gets for the rest of the month once the Free tier's 1,000 semantic
requests are spent, and until this run nobody knew what it cost. Tracked as
`cc-t1o1`, whose most interesting candidate is to fall back to *keyword* rather
than to *hybrid* — a trade the table above now prices.

**The negative set is where it is bad, and the floor is why.** Restraint —
answering an unanswerable question without confidence — measured 25% / 50% / 38%
/ **12%** across the four arms. Production's arm is the worst of them: seven of
eight questions the corpus cannot answer came back grounded, including *"which
items are safe for a peanut allergy"*, about four published marks that do not
include peanut.

That is the measurement `PROVISIONAL_RERANKER_FLOOR` was waiting for. Its own
docstring calls it *the one number in this package that was not measured* and
names #50 as where the real one comes from; this says 1.5 is too low, and it says
there is headroom to raise it because the answerable side is at 100%. Tracked as
`cc-sans`. Picking the number needs three or four sweeps at three or four floors,
because one run at one floor cannot see where a boundary is — `--floor` is a run
parameter and is recorded at the top of every report for exactly that.

`cc-mpdu` is the same question on the degrade path, where confidence comes from
the lexical floor instead: `max(overlap) > 0.0` grounds *"is the kitchen free of
gluten cross contact"* on the word `free`, from *"free Chipotle"*. It is second
in line behind `cc-sans` and the two should be chosen together — they are two
thresholds on the same question.

**Constraint breaches: zero, on every arm.** The filter is exact and it held.

## Adding a question

1. Write it as a visitor would type it. Lower case, unpunctuated, proper nouns
   intact — normalising those away is scoring a query nobody sends.
2. Give every label a `why`. The loader refuses one without it: a label with no
   argument behind it is somebody's guess about relevance that nobody can dispute
   later.
3. Give the question a `why` too, saying what it is in the set *for*. The set is
   read by whoever is debugging a bad number, and "why is this here" is their
   first question.
4. Carry the `golden_case` if it comes from one. #50's scope says the set is
   built from the knowledge portion of [#29](https://github.com/gganssle/chip_chat/issues/29),
   and the id is what makes that checkable rather than claimed.
5. Run `make retrieval-check`, then `make retrieval` — the second one rewrites
   `BASELINE.md`, and its resolution section is the thing to read.

An unanswerable question may not name a place, an answerable one needs either a
place or a constraint, and two identical labels on one question are refused —
they would put the same place in the denominator twice and quietly halve its
recall.
