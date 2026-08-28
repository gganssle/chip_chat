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

One measured run found a **third**, and it is the mechanism earning its keep.
That run's live index held thirty of the fixture's thirty-one chunks — the one it
was missing was the FAQ entry *"Do points expire?"*, because the index it was
querying was the smaller of the two `make search-verify` builds and the verify
run had left it live. So `rew-points-expire`, which is answered in three
published places, was scored over the two the index actually held. Resolving
against the export instead would have marked the retriever as missing a passage
nothing could have returned. That is why `--from-index` exists and why a measured
sweep uses it: what a resolution answers is *can the retriever return this
place*, and that is a question about the index.

It is also the reason a sweep should follow a `make search-build` rather than a
`make search-verify`. The committed baseline resolves 35 of 37 labels, which is
every label the fixture can support; a run that resolves 34 is a run against a
corpus that is one chunk short, and the report says which one.

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

## What three measured sweeps found

Against `srch-chip-chat-4cy39i` on 2026-08-27: one sweep against
`corpus-20260827t060000z-2`, and two more the same day against
`corpus-20260827t053000z` and `corpus-20260827t053000z-2`, which are the same
thirty-one published chunks rebuilt. [`BASELINE.md`](BASELINE.md) is the third
of them. Three runs of one set against one corpus is not a lot, and it turned
out to be exactly enough, because the arms sorted into two groups and the
division is the finding.

**The demo bar: 100% top-3 recall on the allergen questions**, under the
configuration production runs — and **the same 100% on all three sweeps**. That
is the number the rest of the project is held to. A chunking change, a prompt
change or an index rebuild that moves it down has broken something, whatever
else it improved, and three independent runs is enough to say that a move would
mean something.

**Two arms reproduce and two do not.** `recall@3`, all categories:

| Arm | sweep 1 | sweep 2 | sweep 3 |
|---|---:|---:|---:|
| keyword only | 84% | 84% | 84% |
| hybrid + reranker | 95% | 95% | 91% |
| hybrid | 53% | 84% | 84% |
| vector only | 41% | 7% | 83% |

BM25 is deterministic and reproduces exactly. The reranked arm moves by four
points, which is one question's worth on a forty-question set. The other two
move by a factor of eleven, and nothing about the retriever, the corpus or the
labels changed between the runs.

**What was changing is the service, and it is written up in
[`docs/retrieval.md`](../../docs/retrieval.md) §9.** A vector query against this
Free-tier service returns an empty result set with HTTP 200 and no warning, at a
rate that rises from about a quarter on a rested service to about six in seven
after a few dozen vector queries. It was eliminated against the vectorizer, the
embedding deployment, four API versions, the compression settings and every
service quota. The consequence here is direct: the vector arm measures the
service's availability that afternoon rather than the retriever's recall, and the
`hybrid` arm — whose fused response is *identical* to the keyword response when
the vector half returns nothing — came out equal to `keyword only` in every cell
of sweeps 2 and 3.

**So the ablation does not yet defend the design choice, and saying so is the
point of running it.** RFC-001 §08's argument — *item names are proper nouns that
embeddings handle poorly* — looked confirmed and sharply so by sweep 1, whose
vector arm scored **0%** on all three menu-row categories. Sweep 3 has the same
arm at 80% on ingredients, 100% on nutrition and 83% on allergens. Sweep 1 was
reading a service fault as a finding, and the only reason anybody knows that is
that the sweep was run again. A number that does not reproduce is not evidence,
whichever way it points; #50's fourth criterion is that the ablation is
repeatable, and repeating it is what this cost.

`hybrid` **is** the product past the ceiling — it is what a visitor gets for the
rest of the month once the Free tier's 1,000 semantic requests are spent — so
what the fallback costs is still unpriced. Tracked as `cc-t1o1`, whose most
interesting candidate is to fall back to *keyword* rather than to *hybrid*; on
this evidence that is already what happens.

**And `BASELINE.md` beside this file is now sweep 4, taken after all of the
above.** It is a *thinner* document than the one it replaced and that is the
improvement: `hybrid + reranker` reads 100% on the allergen questions over
**four** of them rather than eight, with the other four named as degraded and a
warning printed above the number. The eight were never eight. A baseline with
holes you can see is worth more than one with errors you cannot, and the
previous file — 100% over eight, silently including three lexical-only
retrievals — is the artifact `chip-wez` was filed about.

**A fourth sweep cannot produce a fourth row of that table.** `chip-wez` closed
on 2026-08-27 and what closed it was not a fix — the tier is the fault and the
tier is staying — but a detector. Reciprocal rank fusion gives a document placed
by one ranker at most `1/60`, so a result set with no score above that was found
by one half; `chip_chat.search.fusion` reads it, every retrieval carries the
verdict, and this harness scores a question whose vector half dropped as
**unscored** — in no numerator and no denominator, exactly as an unresolved
label already is. An arm with even one such question is marked *not comparable*
and stamped above its own table with the questions named. The section **Did the
vector half actually run** is printed above the demo bar on every report,
including clean ones, because a warning that appears only on a bad day is one
whose absence means nothing.

Run `make search-vector-arm` before `make retrieval-baseline`. It sends forty
hybrid queries through the ordinary retriever, costs no semantic requests, and
says what the drop rate is right now — which is worth knowing before a sweep
spends forty of the month's thousand producing a report with two unscored arms.
A run of it on 2026-08-27 measured **32 of 40 dropped**, with only three distinct
top scores across the whole run and the lowest of them `1/60` exactly.

The restraint numbers below are from sweeps taken before any of this existed, so
they are restraint *and* the service's availability mixed together. The report
now separates them: a degraded negative is neither restrained nor overconfident,
and it is counted in its own column. That column is the one to read first, since
restraint is the only metric here that a broken retriever makes look better — a
retriever returning less is a retriever declining more.

**The negative set is where it is bad, and the floor is why.** This result is
stable across all three sweeps. Restraint — answering an unanswerable question
without confidence — measured 25% / 75% / 25% / **12%** on sweep 3. Production's
arm is the worst of them: seven of eight questions the corpus cannot answer came
back grounded, including *"which items are safe for a peanut allergy"*, about
four published marks that do not include peanut.

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

**Constraint breaches: zero, on every arm, on every sweep.** The filter is exact
and it held.

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
