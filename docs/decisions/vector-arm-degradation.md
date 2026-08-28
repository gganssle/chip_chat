# Decision: a hybrid query that lost its vector half is served, loudly, and never retried

**Issue:** `chip-wez` · **Decided:** 27 August 2026
**Changes:** `chip_chat.search.fusion` (new), the `retriever.search` span, the
`search_menu_knowledge` payload, `eval/retrieval`'s scoring and report
**Does not change:** the tier — `docs/decisions/search-tier.md` stands — or the
retrieval design. Hybrid plus a reranker is still what production sends

---

## The fault, in one paragraph

Azure AI Search's Free tier drops the vector half of a query and reports the
drop as a success: `{"value": []}`, **HTTP 200**, no `error` key, no warning,
and an ordinary `elapsed-time` header. `docs/retrieval.md` §9 is the
investigation and it eliminated every other explanation by running one — the
vectorizer, the embedding deployment, four API versions, `rerankWithOriginal
Vectors` on a `stored: false` field, scalar quantization, `k`, and every
`/servicestats` quota. What is left is the tier. The rate climbs with query
volume within a run, from roughly a quarter on a rested service to roughly nine
in ten after a few dozen vector queries, and it does not clear in minutes.

The consequence is not a worse answer. It is a **silently different query**.
Reciprocal rank fusion carries no field saying which ranker placed a document,
so a hybrid response whose vector half returned nothing is a well-formed hybrid
response that happens to be the keyword response. Nothing in the application
could tell the two apart, and for three committed ablation sweeps nothing did:
`hybrid` came out identical to `keyword only` in **every cell** of two of them,
and the third's headline read a vector arm scoring 0% on three menu-row
categories as confirmation of RFC-001 §08's argument about proper nouns. It was
a service fault being written up as a finding.

## The question this record answers

Detecting it is settled by arithmetic and is not a decision — see below. What
needed deciding is what the *product* does on a live turn when the vector half
drops. Three candidates:

1. **Retry once.** Ask again and hope.
2. **Serve the lexical result, flagged as degraded.**
3. **Decline the lane**, as it declines for an outage.

## The decision

**Serve it, flagged, and do not retry.** Concretely, and all of it in
`chip_chat.search.retrieve`:

- `Retrieval.vector_arm` carries the reading and `Retrieval.degraded` is its
  one-word form.
- Two notes go to the agent. The first says the keyword half alone ran and
  forbids the one inference a lexical-only result cannot support: *do not say
  the restaurant does not publish something.* The second fires when nothing
  cleared the confidence bar either, and says to tell the visitor the lookup is
  only partly working rather than to report an absence.
- The notes that would contradict those are **withheld**. `"these are the
  nearest passages in the corpus"` and `"nothing in the published corpus
  matched"` are both claims about the corpus, and neither is available from a
  retriever that looked with half of itself.
- `search_menu_knowledge` carries `degraded` and `vector_arm` beside
  `confidence` and `reranked`, so declined, low-confidence and
  answered-with-half-a-retriever are three states the agent can tell apart.
- The `retriever.search` span carries both, plus `fused_by_both` per document,
  plus the tag `retrieval.lexical_only`.
- One query goes out and one comes back. No second request, ever.

## Why not retry

This is the candidate that sounds cheapest and is not, and the argument is
arithmetic rather than taste.

**The recovery rate is low exactly when it is needed.** The fault is
volume-driven and does not clear in minutes, so a retry issued a millisecond
after a drop is a draw from the same distribution the drop came from. On a hot
service that distribution is 85–90% empty. A measured run of 40 hybrid queries
through the shipped detector on 27 August returned **32 dropped of 40** with the
rate flat across both halves of the run — so a retry-once policy would have
recovered somewhere around four of those thirty-two.

**And it is paid for in the scarcer budget.** Retrying a *reranked* query spends
a second of the Free tier's 1,000 semantic requests a month — the hard ceiling
`docs/decisions/search-tier.md` accepted, which is roughly 33 a day. At an 80%
drop rate, retry-once turns 1,000 requests a month into about 555, to recover
around one turn in eight. Paying 45% of the month's retrieval quality budget for
a 12% recovery on the failure it is meant to fix is the wrong trade, and it is
the wrong trade *by a wide enough margin that no plausible re-measurement of the
rate reverses it*.

There is a version of retry that costs no allowance — retry without reranking —
and it is worse rather than cheaper: it silently changes which arm answered the
visitor's question mid-turn, so the retrieval that reaches the agent is neither
the arm it says it is nor the arm the confidence rule was written for.

## Why not decline

`docs/retrieval.md` §7 already argues this for the reranker and the argument
does not weaken here: **a degraded answer beats no answer when the degraded
answer is real published data.** A lexical-only result is not a guess. It is
BM25 over the five searchable fields, which is the `keyword only` arm — and that
arm measured `recall@3` of **84%** on all three of the sweeps in
`eval/retrieval`, stable to the point of being the most reproducible number in
the file. It is the second-best of the four arms and it is what the degrade path
has quietly been serving all along.

Declining would also cost far more than the fault does. At an 80% drop rate the
knowledge lane would be out four turns in five, which is not a degraded product,
it is an absent one — for a defect whose actual blast radius is *some recall*.

## What is deliberately **not** done: capping confidence

The obvious protective move is to force a degraded retrieval to
`Confidence.LOW`. It is not made, and the reason is worth stating because it
looks like the safe option.

The passages that came back are not suspect. They are published chunks that BM25
matched and, on the serving arm, that the semantic ranker scored — and the
reranker's score is a relevance score over whatever union it is handed, so it is
exactly as meaningful on a lexical union as on a fused one. §9 measured the
reranked arm at **100% top-3 recall on the allergen questions** on the very run
where the fault was found. Capping confidence would trade that measured result
for a blanket disclaimer.

What is lost to this fault is **recall**, not precision. The risk is a passage
that never came back, and the harm it does is a confident *absence* — "the
restaurant doesn't publish that" — rather than a confident wrong answer. So the
mitigation is aimed at absence claims specifically, which is what the two notes
do, rather than at confidence generally. And a blanket cap would make the
failure *quieter*, not louder: it would fold a service defect into the same
signal the product already uses for a thin corpus, which is the exact conflation
that let this go unnoticed for three sweeps.

## The detection, and the one thing it refuses to claim

Not a decision — the arithmetic is forced. Reciprocal rank fusion gives a
document `1/(k + rank)` from each ranker that placed it, at `k = 60` with a
**zero-based** rank. So a document exactly one ranker placed scores at most
`1/60`, and one both placed scores strictly more. Measured against
`srch-chip-chat-4cy39i` on 27 August, the same question seconds apart:

```
healthy   0.033060  0.031754  0.031746  0.031319  0.031054
degraded  0.016667  0.016393  0.016129  0.015873  0.015625
```

The second row is `1/60, 1/61, 1/62, 1/63, 1/64` to every digit the service
prints — the ranks themselves, with no relevance anywhere in the numbers.

Two details cost more to find than they look. The rank is zero-based, not
one-based, so the threshold is `1/60` and not `1/61`; off by one and it sits
below the value a degraded query returns and reports every one of them as
healthy. And the service answers in **single precision**, so its own `1/60`
arrives as `0.01666666753590107` — *above* the exact double. The comparison
carries a relative tolerance for that, and the fake in `search/tests` was
changed to narrow through float32 rather than round to six places, because
rounding sends `0.016667`, which is above `1/60`, which would have made the test
suite written to prove the defect is caught unable to catch it.

The reading is taken over **every** returned passage rather than the top one.
The reranked arm reorders by relevance, and a healthy reranked response was
observed live with a single-ranker top hit and two-ranker scores at ranks four
and five.

**`DROPPED` is claimed from that proof or not at all.** An empty result set is
never on its own called a fault, because a filter that matches nothing, an index
with nothing in it, and a dropped vector half all produce one and none of them
is separable from the others inside a single response. That gives up one case —
a query whose vector half dropped *and* whose lexical half matched nothing —
which has never been observed and is already safe, since no passages is
`Confidence.NONE` and nothing builds a confident answer on no passages. The
single-half `vector only` arm of #50's ablation is the exception: there the
returned count *is* the whole signal, and that arm runs beside three others
against the same index, so "the index is empty" is ruled out by its neighbours.

The refusal matters as much as the detection. `eval/retrieval`'s negative set is
eight questions the published corpus genuinely cannot answer, and a detector
that called those degraded would make restraint unmeasurable. It does not: a
nearest-neighbour search returns neighbours for any question it is asked, so
every one of those questions comes back full and reads as two-ranker.

## What the eval does with it

Follows `eval/adversarial`, which refuses to score a concurrent round that never
contended a connection and prints *could have caught a bleed: no* rather than a
clean pass. A question whose vector half dropped is **unscored** — in no
numerator and no denominator, exactly like a label the corpus does not hold —
and it is counted apart from that under its own name, because a service defect
and a harvest gap have different fixes. An arm with even one degraded question
is marked not comparable and stamped above its own table. Restraint on the
negative set goes unscored too, and that column is the one to read first: it is
the only metric in the report a broken retriever makes look *better*, since a
retriever returning less is a retriever declining more.

## Revisit trigger

The Basic tier settles this along with the 1,000/month ceiling and the missing
managed identity, at $73.73/month. Revisit when the measured drop rate makes the
knowledge lane's answers materially worse in a run of `eval/grounding` — the
detector now makes that measurable, which it was not before — or on any of
`docs/decisions/search-tier.md`'s own triggers. `make search-vector-arm` is the
measurement, costs nothing, and should be run before `make retrieval-baseline`
spends 40 semantic requests on a sweep whose vector arms would be unscored.
