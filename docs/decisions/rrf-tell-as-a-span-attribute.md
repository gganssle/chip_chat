# Decision: the RRF tell is a flat span attribute, and the threshold travels with it

**Issue:** `chip-wez` · **Decided:** 28 August 2026 · **Not measured:** the rate the flag fires in production
**Changes:** `otel/src/chip_chat/otel/attributes.py`, `otel/src/chip_chat/otel/spans.py`, `search/src/chip_chat/search/fusion.py`, `search/src/chip_chat/search/lane.py`, `docs/retrieval.md` §9
**Does not change:** the retrieval strategy, the search tier, the eval pacing, or `VectorArm`'s four readings

---

[decisions/vector-arm-degradation.md](vector-arm-degradation.md) settled *what*
to do about Free-tier vector search returning `{"value": []}` with HTTP 200:
detect it from the reciprocal-rank-fusion arithmetic, serve the result anyway,
and say so at every boundary. This is the smaller question that decision left
open — *where on the span does the reading actually live* — and it is worth its
own file because the obvious answer was already in the code and was wrong for a
reason nobody would notice until they needed the number.

## The problem with what was already there

The lane recorded the reading through `set_metadata`, which writes one attribute:
OpenInference's `metadata`, holding a JSON string. Everything about the retrieval
went in together — the index alias, the confidence, whether the reranker ran, the
semantic allowance, the constraints, and `vector_arm` and `degraded` beside them.
For somebody with a trace open in Phoenix that is exactly right. It is one place
to look and the fields are in context.

It is not a thing a dashboard can count. The whole reason `docs/retrieval.md` §9
exists as an investigation rather than as a chart is that establishing the rate
took five hundred hand-driven queries and a probe script reading raw scores.
Making it countable was the point of instrumenting it at all, and Application
Insights — the backend this system actually ships to — filters attributes, does
not walk trace trees, and does not parse JSON blobs either. `where
customDimensions.metadata contains "dropped"` is a substring match over a
serialised object, which is not a filter so much as a hope.

This is the same argument that produced `chip_chat.tokens.*` beside
`llm.token_count.*`, and it is worth noticing that it is the same argument,
because the conclusion is the same: a fact somebody will want to aggregate has to
be a first-class attribute, even when it is already derivable from something else
on the span.

## The decision

**Five keys in the `chip_chat.*` namespace, written by
`RetrieverRecorder.record_fusion`, in addition to the metadata rather than
instead of it.** `vector_arm` and `document_count` on every retrieval;
`single_ranker_fusion`, `top_fused_score` and `single_ranker_ceiling` only where
the fusion inequality was actually evaluated.

The namespace follows the precedence order in `otel/README.md` mechanically:
OpenInference owns `retrieval.documents.*` and has nothing to say about rank
fusion, the OpenTelemetry `db.*` conventions are scoped to `db.cortex_analyst`,
so this is ours and the prefix says so.

Keeping the metadata copy is deliberate rather than an oversight about
duplication. The two serve different readers — one trace against a week of them —
and dropping the metadata copy would make a single trace worse to read in order
to save an attribute nobody is paying for.

### Absent is not false, and that is the part that took the argument

`single_ranker_fusion` is set only on a hybrid query that returned at least one
document, and the temptation to write `False` everywhere else is real: a boolean
that is sometimes missing is more annoying to query than one that is always
there. It is still wrong. A lexical-only query's `@search.score` is BM25 — 34.6
on the live alias — and a vector-only query's is a cosine similarity around 0.7.
Both clear `1/60` by one to three orders of magnitude, so a threshold applied to
either records *healthy* about a query that has no fusion to be healthy about.
An empty result set has no score at all.

Writing `False` in those cases would not merely be uninformative; it would put
readings into the denominator of every rate somebody computes from this
attribute, and it would do it in the direction that makes the service look
better. That is the failure mode this detector exists to remove, reintroduced one
layer up. So the attribute is absent, and `document_count` — present even when it
is zero — is what separates *returned nothing* from *was never asked*.

### The threshold is passed in, not typed in

`chip_chat.otel` is a leaf: an import-linter contract holds it to importing
nothing from this workspace, and `make imports` checks it. So the recorder cannot
reach `chip_chat.search.fusion.SINGLE_RANKER_CEILING`, and the two obvious ways
out are both bad — hardcoding `0.0166…` in the otel package puts a search
service's constant in a package that knows nothing about search, and inverting
the dependency would break the leaf.

The threshold is therefore an argument: `fusion` derives it as `1.0 / RRF_K` from
the constant it measured, and hands the quotient to the span. That keeps one
definition of `k`, keeps the leaf a leaf, and has a third benefit that was not
the reason but should have been — the number appears **on the trace**, so a
retrieval judged today can be re-judged next quarter without anybody having to
work out which revision of the source tree was deployed when it was recorded.

`RRF_K` is `60` because Azure documents it and because a degraded query on the
live alias returned `1/60, 1/61, 1/62, 1/63, 1/64` byte for byte, which is also
what settles the zero-based-rank question the documentation leaves ambiguous. The
comparison carries a relative tolerance of `1e-6` rather than testing equality,
because the service answers in single precision and sends its own `1/60` as
`0.01666666753590107` — larger than the exact double, and a bare `>` reads that
as proof of a second ranker.

### The verdict is carried, never recomputed

`fusion.tell` takes the `VectorArm` the retrieval already reached rather than
deriving a second one from the scores. A span that contradicted the tool result
it describes would be worse than a span with less on it, and the two computations
could drift apart on any future edit to `contribution`. So `tell` adds numbers
and never an opinion.

## What was rejected

**A metric rather than an attribute.** An OpenTelemetry counter would answer
"how often" more directly than any attribute query. It would also answer only
that: no session id, no query text, no scores, nothing to open. This defect's
whole history is of a number that looked plausible for three sweeps, and the
remedy for that is evidence attached to individual traces, not a faster
aggregate. Nothing stops a counter being added later off the same reading.

**Marking the span failed.** Already rejected in
[vector-arm-degradation.md](vector-arm-degradation.md) and unchanged here. The
service answered 200 and the passages are real published data.

**Reusing the `retrieval.lexical_only` tag as the only signal.** Tags are a list
attribute and Phoenix filters them well. They carry no numbers, so the evidence —
the top score, the threshold — would have had nothing to ride on, and an
after-the-fact re-judgement would be impossible.

## What is not measured

**The rate at which this flag fires in production is not measured.** The 80% in
`docs/retrieval.md` §9 came from `make search-vector-arm`, a probe script run by
hand against the live alias on 2026-08-27, and it is a measurement of the
detector, not of the attribute. Reading the same number off these attributes
needs the deployed service, real traffic, and an Application Insights query; none
of those exist inside `make ci`, and nothing in this repository has run one. What
is verified is narrower and worth stating exactly: the flag fires on the score
sequences the live service returned while degraded, stays silent on the ones it
returned while healthy, is withheld on every query shape the arithmetic does not
apply to, and does not disturb the span tree it hangs on.
