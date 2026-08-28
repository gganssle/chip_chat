# The knowledge lane's query, and the three things it refuses to guess

**Issue:** [#49](https://github.com/gganssle/chip_chat/issues/49) ·
**Code:** `search/src/chip_chat/search/{query,retrieve,allowance,lane}.py` ·
**RFC:** [rfc-001.md](rfc-001.md) §08, §09, §10 · **PRD:** K1–K4 ·
**Decision:** [decisions/citation-presentation.md](decisions/citation-presentation.md)

[retrieval-index.md](retrieval-index.md) is how a corpus gets into an index.
This is what happens when somebody asks it a question.

```bash
make search-retrieve Q="how do rewards points work"
make search-retrieve Q="what has no dairy" RERANK=0   # the degrade path, on purpose
```

`az login` and the data-plane role `search.tf` already grants, and nothing else:
no embedding deployment, no Foundry key, no Key Vault read. The *index* holds the
vectorizer, so a query is text. It is the only live target in this repository
that is not also a build.

---

## 1. The shape

```
tool.search_menu_knowledge          the agent's tool layer opens this
└─ retriever.search                 KnowledgeLane opens this, and nothing else
       │
       ├─ query.read()              the sentence → constraints, or a refusal to guess
       ├─ query.body()              one request: BM25 + vector + optional reranker
       ├─ allowance.spend()         one of the month's 1,000 semantic requests
       └─ retrieve.Retriever        → passages, scores, citations, confidence
```

| Module | Holds |
|---|---|
| `query.py` | One visitor sentence becomes one hybrid query — and the three constraints it will not approximate. |
| `retrieve.py` | The single interface the tool layer calls. Passages, every score that ranked them, and how much the corpus actually had to say. |
| `allowance.py` | The Free tier's 1,000 semantic requests a month, counted, because past them there is no bill — only a refusal. |
| `lane.py` | The `retriever.search` span, and the lane that declines by itself when the service is down. |

`Retriever` is the interface; `KnowledgeLane` is `Retriever` plus the span plus
the guarantee that an outage returns rather than raises. The tool layer calls
the lane. Nothing below it ever learns an index name — the application knows
the alias `corpus` and no more.

---

## 2. Hybrid is not a hedge

RFC-001 §08 is unambiguous: *keyword recall matters here more than usual,
because item names are proper nouns that embeddings handle poorly.* Every query
carries both halves, always, and neither is a fallback for the other:

```json
{ "search": "barbacoa",
  "queryType": "semantic",
  "semanticConfiguration": "corpus-semantic",
  "captions": "extractive",
  "vectorQueries": [{"kind": "text", "text": "barbacoa",
                     "fields": "text_vector", "k": 50}],
  "top": 5 }
```

`"kind": "text"` is the half of integrated vectorization this estate has: the
**service** embeds the query, with the deployment named on the index, so the
application cannot embed into a different vector space than the corpus was built
in ([retrieval-index.md](retrieval-index.md) §3). There is no `select`, and that
is deliberate — every field of this index is retrievable except the vector, so
naming them would be a second copy of the chunk schema, free to drift from the
first.

**Semantic answers are not requested.** Captions are, because they are free with
the same request and they are what D9's *on-demand detail* expands to. An
extractive `answer` would be a second answer, written by the service, competing
with the one the agent is about to write from the same passages — which is a
groundedness question nobody needs to have.

**One half alone is expressible, and only an eval may ask for it.** *Hybrid is
not a hedge* is an argument, and #50 asks for it to be defended by data — so
`chip_chat.search.query.Halves` lets a query drop the `search` string or the
`vectorQueries` entry, and `eval/retrieval` sweeps all four arms. It is not a
knob: the default is both halves everywhere, that enum's docstring says who may
pass anything else, and nothing on a path that serves a visitor does. A vector
arm carries **no** `search` key rather than an empty one — Azure AI Search reads
an absent `search` as *no lexical half* and an empty string as a lexical query
matching everything, which would leave a second order in the fusion and quietly
stop being an ablation.

---

## 3. The three things it refuses to guess

This is the part of the issue that reads *"query construction that handles the
comparative and constrained cases"*, and almost all of its content is the
refusals.

### A filter only fires on a word the restaurant published

`dair` is the allergen code because Chipotle's chart publishes it as one, and
*Dairy* is what the chart calls it. So `read()` recognises **dairy, gluten, soy
and sulphites** — four codes, asserted equal in CI to the ones in the harvest's
own parsed vocabulary — and does not recognise *milk*, *lactose* or *wheat*.

That is not a gap to fill later. A visitor who types "no milk" still gets the
vector half of the query, which is what paraphrase is for. What they do not get
is a **filter**, because a filter is exact, and an exact answer to a question
the restaurant never published is the one kind of wrong this lane cannot be.

A mention is not a constraint, either. *"Does the cheese have dairy in it"* and
*"what has no dairy"* differ by a negation cue, and a filter built from the
mention alone answers the first one backwards — with the items that do *not*
contain dairy — and answers it confidently.

### An exclusion says "not marked", never "free of"

```
allergen_disclosure eq 'PUBLISHED' and not allergens/any(a: a eq 'dair')
```

The first clause is the load-bearing one, and it comes straight out of
[decisions/allergen-absence.md](decisions/allergen-absence.md). Without it the
answer set includes every chunk Chipotle publishes *no* allergen data about —
napkins, policy sections, anything new next month — under a heading the visitor
reads as *safe*. With it, the answer is exactly "items whose published allergen
marks do not include dairy", which is a sentence somebody at Chipotle has
actually written down. The caveat travels with the result as a note, because
Chipotle's own words are that an unmarked item is one it declines to make a
promise about.

### What the index cannot express, it says so

| The visitor asked | What happens |
|---|---|
| "under 500 calories" | `calories lt 500`. Menu items only — `calories` is null on every other kind, and an OData comparison against null is false, which is the question that was asked. |
| "500 calories or less" | `calories le 500`. The two phrasings differ on exactly one published figure; getting that wrong on purpose would be strange. |
| "which bowl has **fewer** calories" | **No filter.** The referent is an item this layer cannot see. Every passage carries its published `calories`, so the comparison is the agent's — and it is told so. |
| "what is **vegetarian**" | **No filter.** `vege` is a published tag and the chunk schema carries allergen marks with no dietary marks. The agent is told the passages are *not* filtered, in those words. |

Guessing "vegetarian" from ingredient text would be inventing a dietary claim
about food. There is a second reason to leave it alone even where the marks
exist: Chipotle's two published documents agree exactly about allergens and
**disagree about diets** — nine foods marked Whole30 under `whol` in one, two
under `wh30` in the other, and nothing published saying those are the same diet.

---

## 4. Citations are carried, not reconstructed

Every passage arrives with its `source_url` and its `harvested_at`, and
`documents.py` already refuses to *index* a chunk without them. So a hit that
comes back without one means the live index and the build disagree about the
schema — and the honest response is not to pass it on uncited. It is dropped,
counted in `Retrieval.uncitable`, reported to the agent as a defect rather than
as a data gap, and recorded on the span. *Citations present on every returned
passage* is then a property of the type rather than a hope about the data.

D9 ([decisions/citation-presentation.md](decisions/citation-presentation.md))
says the model names ids and the app draws the citation. This layer supplies
both sides of that:

```python
result.citations()  # {chunk_id: {id, label, source_url, harvested_at}}
result.as_tool_result()  # what the model sees -- ids, text, scores, no URLs
```

The URL is deliberately not in the second one. It is the one field a model could
paste into prose, and D9's whole mechanism is that it cannot: `render()` resolves
the ids the model named against what the retriever actually returned, and drops
the rest. The `label` — *"Menu · Barbacoa"* — is derived here from the chunk's
`kind` and `heading`, for the same reason: a label a model wrote would be a
source a model invented.

---

## 5. Low confidence is a result, not an absence

> When the published data does not contain an answer, the retrieval layer must
> make that legible to the agent rather than returning the nearest thing and
> letting a model paraphrase over the gap. — #49

`Confidence` is the three-valued answer at the tool boundary:

| | Means |
|---|---|
| `grounded` | The best passage cleared the bar. An answer may be drawn from these. |
| `low` | Passages came back and none cleared it. They are the nearest things in the corpus, which is not the same as an answer. |
| `none` | Nothing came back, or the lane declined. |

**Reranked**, the bar is `@search.rerankerScore` — a relevance score on 0–4 —
against a floor.

**Degraded**, it cannot be the fused `@search.score`, and this is the subtlety
worth spelling out. Hybrid results are ordered by reciprocal rank fusion, which
scores *rank*: the top hit of a query the corpus cannot answer scores very nearly
what the top hit of a perfect match scores, because both were first. A threshold
on it would really be measuring how many results came back. `test_retrieve.py`
asserts that directly — the two top scores differ by less than 0.01 — so it is a
property the suite holds rather than a claim in a paragraph.

So the degrade path uses a **lexical floor** instead: does any returned passage
contain any content word of the query at all. Cruder than BM25 by design, and
unlike the fusion score it is actually about the passage. A result set that
shares no word with the question is a handful of vector neighbours and nothing
more, which is exactly the near-miss RFC-001 §08 warns proper nouns produce.
Across the whole set rather than the top hit alone, because on this path the
order the passages arrived in is a rank fusion the keyword half only partly
decided.

### The one number that was not measured

`PROVISIONAL_RERANKER_FLOOR = 1.5`, and it is labelled rather than buried. What
is known is the scale (0–4) and one genuine hit that scored **1.73** on the live
service. What is *not* known is where the boundary falls on this corpus, because
that is a question about a golden set and the golden set is
[#50](https://github.com/gganssle/chip_chat/issues/50)'s.

Two things keep a placeholder from doing harm. It sits below the only good hit on
record — a test asserts exactly that, because a floor above it would reject a
real answer *silently*, reporting "the published data does not cover it" about a
passage that covers it. And **every raw score travels**: `score`,
`reranker_score` and `overlap` are on the payload and on the span, so #50 can
choose the real number from recorded runs without re-querying anything and
without changing a caller.

---

## 6. The ceiling, and why the degrade path is first-class

The search service is on the **Free** tier, and on Free the semantic ranker is
not merely cheap — it is capped at **1,000 semantic requests per calendar
month**, about 33 a day. Past the cap a request does not cost a dollar. It
**fails**. `standard`, the plan that would turn the ceiling into $1.00 per 1,000,
is refused outright on a Free SKU in as many words: *"Semantic Search Standard
Tier is not supported on Free SKU."*

So hybrid-without-reranking is a live path with a name, a test and a CLI flag —
not a fallback nobody exercises. Three things reach it:

1. **The counter says no.** `SemanticAllowance` claims one request before each
   semantic query, counted when *issued* rather than when answered: being one
   request pessimistic against a limit of 1,000 is not a failure mode, and being
   one optimistic is.
2. **The service says no.** A refusal that reads as a spent allowance — rather
   than as a malformed query — retries the same question without reranking and
   marks the month exhausted. The service is the authority on its own allowance;
   the counter is an estimate of it.
3. **The caller says no.** `rerank=False`, which is how the path gets exercised
   without waiting for a ceiling nobody wants to reach.

What must *not* happen is the fourth thing: declining. RFC-001 §10 sanctions
declining when AI Search is **unavailable**, and a spent reranker allowance is
not that. Weaker ranking is far better than no answer, and a lane that declined
because a counter rolled over would be declining for a reason no visitor can see
and nobody can fix before the first of the month.

**The count outlives the process** when it is asked to. The obvious way to spend
a month's allowance without noticing is an evaluation sweep — a hundred queries
is a tenth of the month in one command — so `FileAllowanceStore` keeps the count
in `$(LANDING)/semantic-allowance.json`, and `make search-retrieve` uses it.
[#10](https://github.com/gganssle/chip_chat/issues/10) asks for exactly this
counter.

---

## 7. One dead lane, and the rest of the conversation

RFC-001 §10's first row: *AI Search unavailable → the knowledge lane declines
and says why; other lanes unaffected. Blast radius: knowledge only.*

That is a property of `KnowledgeLane.search` **returning rather than raising**.
It catches the package's error type, marks the span failed — so an outage looks
like an outage rather than like an empty corpus, which need very different fixes
— and returns a `Retrieval` whose `declined` says what happened. The tool result
is a typed refusal the model can read out to the visitor:

```json
{"declined": "KNOWLEDGE_LANE_UNAVAILABLE",
 "detail": "... ConnectError: [Errno 61] Connection refused",
 "notes": ["The published-menu search service is not answering, so I cannot ..."]}
```

One thing had to change underneath for that to be true. `HttpSearchService` now
turns a transport failure — refused connection, DNS, timeout — into the package's
own `ServiceError`. Without it, *the one failure that row is actually about*
arrives as an `httpx` exception, escapes the lane and takes the turn with it. The
HTTP client is injected rather than imported, so the transport's exception types
are deliberately not nameable there; catching broadly and re-raising narrowly is.

---

## 8. Reuse the connection. It is the whole latency budget

Measured from `ca-chip-chat-web` (eastus2) against `srch-chip-chat-4cy39i`
(eastus) on 2026-08-26:

| | p50 |
|---|---|
| hybrid query, warm pooled connection | **11.2 ms** (p95 11.8, n=35) |
| the same query, fresh TLS connection | **84.3 ms** (n=12) |
| semantic reranking, captions, on top | ~30 ms |
| the cross-region hop | 6.8 ms |

A cold connection costs seventy milliseconds — **seven times** the region penalty
everyone reaches for first, and more than twice what the reranker costs. So the
reranker is worth its 30 ms, moving the service back to eastus2 would buy 6.8 ms,
and the one decision that dominates both is holding the connection open.

`pooled_client()` sets `keepalive_expiry` to five minutes against httpx's default
of five seconds, which is tuned for a service under constant load and wrong for a
demo with a visitor every few minutes. Build **one** client per process, hand it
to one `HttpSearchService`, and hold it in one `Retriever` for the life of the
app. A client per turn is a TLS handshake per turn, and nothing further down
recovers it.

---

## 9. The vector half comes back empty, and the service calls that a success

This section was a defect report and is now a defect report with a remedy
attached. The fault was found on 2026-08-27 by re-running the ablation against
the live alias, and it is the most consequential thing anybody has learned about
this lane since it was built, because it means *hybrid* is not reliably what the
service is doing when the application asks for hybrid. The remedy, decided the
same day and argued in
[decisions/vector-arm-degradation.md](decisions/vector-arm-degradation.md), does
not fix it — the tier is the fault and the tier is staying. It makes it **loud**:
detected at the retrieval boundary, said on the span, said at the tool boundary,
and refused a score in the eval. §9.1 below is what now happens.

**The symptom.** A vector query against the live index returns

```json
{"@odata.context": "…/indexes('corpus')/$metadata#docs(*)", "value": []}
```

with **HTTP 200**, no `error`, no warning key, and an `elapsed-time` header in
the ordinary 115–350 ms range. Not an error, not a timeout, not a partial
result. An empty result set that is indistinguishable from a corpus with nothing
in it.

**The rate, measured.** One question, `k: 50`, the same index, across a session
of about five hundred queries:

| When | n | empty |
|---|---:|---:|
| minutes after a build | 20 | 25% |
| after a few hundred queries | 20 | 85% |
| after three minutes idle, 15 s apart | 6 | 67% |
| a freshly built index, first 20 of a 60-query run | 20 | 30% |
| the next 20 of the same run | 20 | 85% |
| the last 20 of the same run | 20 | 90% |

The last three rows are the shape of it: a rested service answers the first
twenty or so vector queries and then mostly stops, and it does not recover in
minutes. That is why the middle one of the three sweeps in §10 scored the vector
arm 40% on `ingredients` — the first category in the question file — and **0%**
on all four categories after it. The number is not a fact about embeddings. It is
the order the questions are in.

**What it is not.** Each of these was eliminated by a run rather than by an
argument, and each cost an index or a few hundred queries.

| Suspect | Test | Result |
|---|---|---|
| query-time vectorization | send the floats: `"kind": "vector"` instead of `"kind": "text"` | 4/12 empty against 2/12 in the same minute. **Not the vectorizer.** |
| the embedding deployment | 20 embeddings requests direct to Foundry with the same Key Vault key | 20 × 200 in 4.2 s, no 429. The deployment is fine. |
| the preview API contract (§4) | the same query under `2025-08-01-preview`, `2024-11-01-preview`, `2025-09-01`, `2024-07-01` | 10/12, 11/12, 10/12, 11/12 empty. **Not the version.** |
| `rerankWithOriginalVectors` on a field the schema also declares `stored: false` — a genuine contradiction, and the best hypothesis there was | build an index with `rescoringOptions.enableRescoring: false` and A/B it | 8/30 against 6/30. **Not rescoring.** |
| scalar quantization | build an index with no compression at all and A/B it | 26/30 against 25/30. **Not quantization.** |
| `k` | 5, 10, 50 | no relationship. |
| a quota | `/servicestats` | storage 560,192 of 52,428,800; `vectorIndexSize` 98,928 against a `null` quota. Nothing is near a limit. |

What is left is the service: **Free-tier vector search on `srch-chip-chat-4cy39i`
degrades under a burst of vector queries and reports the degradation as an empty
result set.** The lexical half is unaffected throughout — every one of those
runs had BM25 answering normally beside it.

**Why the application could not see it.** A hybrid query fuses two rankers by
reciprocal rank, and RRF has no field saying which ranker contributed. When the
vector half returns nothing, the response is a well-formed hybrid response whose
documents happen to all come from BM25, and every score on it is a legal fused
score. There is one tell and it is arithmetic rather than reported, and it is
the whole basis of §9.1: RRF at `k = 60` gives a document found by exactly one
ranker `1/(60 + rank)`, so a result set with no score above `1/60` was found by
one half. Both states appear in this document — §2's worked example and §8's
latency table were taken on a healthy service; the probes in this section were
not.

**What it does not touch.** The reranked arm — the one production sends — is
unaffected in every measurement here, because the semantic ranker reorders the
union it is given and the lexical half is always in that union. #50's demo
criterion, top-3 recall on the allergen questions under `hybrid + reranker`,
measured **100%** on the run this section is about. The blast radius of this
defect is the degrade path and the ablation's two vector arms.

### 9.1 What now happens when it occurs

`chip_chat.search.fusion` is the reading and it is four values, not a boolean.
That is the part worth reading first, because a detector that fired on the
negative set would have cost more than the fault does.

**`CONTRIBUTED`** — some returned document scores above `1/60`, so two rankers
placed it, so the vector half ran. The reading is taken over *every* returned
passage rather than the top one: the reranked arm reorders by relevance, and a
healthy reranked response was observed on the live alias with a single-ranker
top hit and two-ranker scores at ranks four and five. Reading the first score
alone would have called that healthy query degraded.

**`DROPPED`** — passages came back and none of them was placed by both. This is
lexical-only, and it is claimed **only** from that arithmetic.

**`NOT_SENT`** — the `keyword only` arm, which asked for no vector half and
therefore lost nothing.

**`UNDETERMINED`** — nothing came back at all. A filter that matches no
published item produces this, and so does an index with nothing in it, and
neither is separable from a dropped vector half inside one response. So it is
not called a fault. The case that gives up — a query whose vector half dropped
*and* whose lexical half matched nothing — has never been observed and is
already safe, because no passages is `Confidence.NONE` and nothing downstream
builds a confident answer out of no passages. The `vector only` arm is the one
exception: it *is* the vector half, so an unfiltered empty response there is the
fault, and that arm only ever runs beside three others against the same index.

Two details in that arithmetic cost more to find than they look, and both are
one-character mistakes that would have silently disabled the whole thing. Azure's
RRF rank is **zero-based**, so the ceiling is `1/60` and not `1/61` — off by one
and the threshold sits below the value a degraded query returns and reports every
one of them as healthy. And the service answers in **single precision**, so its
own `1/60` arrives as `0.01666666753590107`, *above* the exact double; the
comparison carries a relative tolerance, and `search/tests/fakes.py` was changed
to narrow scores through float32 instead of rounding them to six places, because
rounding sends `0.016667` and the test suite written to prove the defect is
caught would not have caught it.

**On the span.** `retriever.search` carries `vector_arm` and `degraded` in its
metadata, `fused_by_both` on every document — so a trace says *which* passages
the vector half placed, not merely that some were — and the tag
`retrieval.lexical_only`, which is what makes the rate in the table above
countable over a day instead of measurable by hand. The span is **not** marked
failed. The service answered 200 and returned real passages; putting this in the
same bucket as an outage would send somebody to the wrong page.

**At the tool boundary.** `search_menu_knowledge` now carries `degraded` and
`vector_arm` beside `confidence` and `reranked`, so *declined*, *low confidence*
and *answered with half a retriever* are three states the agent can tell apart —
and it could not previously infer the third from the other two, because a
lexical-only hybrid result looks exactly like a hybrid one. Two notes go with it.
The first forbids the single inference this result cannot support: **do not say
the restaurant does not publish something**, because half the retriever did not
look and an absence measured through it is not an absence. The second fires when
nothing cleared the confidence bar either, and tells the agent to say the lookup
is only partly working rather than to report a gap. The notes that would
contradict those — *"these are the nearest passages in the corpus"*, *"nothing in
the published corpus matched"* — are withheld, because both are claims about the
corpus and neither is available here.

**In the eval.** A question whose vector half dropped is **unscored**: in no
numerator and no denominator, exactly as an unresolved label already is, and
counted again by its own name so that a service defect and a harvest gap do not
read the same. An arm with even one degraded question is marked *not comparable*
and stamped above its own table. Restraint on the negative set goes unscored too,
and that column is the one to read first — it is the only metric in the report a
broken retriever makes look *better*, since a retriever returning less is a
retriever declining more. This follows `eval/adversarial`, which refuses to score
a concurrent round that never contended a connection and prints *could have
caught a bleed: no* rather than a clean pass; the optimistic reading of a run
nobody could measure is available in the raw numbers and is never the headline.

**On a live turn: served, flagged, never retried.** The full argument is in
[decisions/vector-arm-degradation.md](decisions/vector-arm-degradation.md); the
short form is that retrying costs the scarce budget to buy almost nothing. The
fault does not clear in minutes, so a retry is a second draw from the same
distribution — 80–90% empty on a hot service — and on the reranked path it is
paid for with a second of the month's 1,000 semantic requests. At an 80% drop
rate, retry-once turns 1,000 requests a month into about 555 to recover roughly
one turn in eight. Declining is worse still: it would take the lane out four
turns in five for a defect whose blast radius is *some recall*, and what is
actually served instead is the `keyword only` arm, which measured `recall@3` of
84% on all three sweeps in §10 and is the most reproducible number in the file.
Confidence is deliberately **not** capped either — the passages that came back
are published data the reranker scored, this fault costs recall rather than
precision, and a blanket cap would fold a service defect into the same signal the
product already uses for a thin corpus, which is the exact conflation that let
this run unnoticed for three sweeps.

**Measured against the live service, 2026-08-27, through the shipped detector.**
`make search-vector-arm` sends forty hybrid queries through the ordinary
retriever and reports what the reading was on each. It costs no semantic requests
and it is the thing to run before `make retrieval-baseline` spends forty of them
on a sweep whose vector arms would be unscored.

| | |
|---|---:|
| queries | 40 |
| vector half dropped | **32 (80%)** |
| first half of the run | 16 of 20 |
| second half of the run | 16 of 20 |
| distinct top scores observed | `0.016667`, `0.032292`, `0.033333` |

Three top scores across forty queries, and the first of them is `1/60` exactly.
The service was already hot from the probes that produced the tables above, which
is why the rate did not climb across the run — it had already climbed. Two live
retrievals from the same session, one on each path:

```
"query": "what are the ingredients in barbacoa"     reranked: false
"degraded": true,  "vector_arm": "dropped"          → both notes fired

"query": "is the sofritas vegan"                    reranked: true
"degraded": true,  "vector_arm": "dropped"          → the note fired beside
                                                      the existing diet caveat

"query": "which items are marked as containing dairy"  reranked: true
"degraded": false, "vector_arm": "contributed"      → silent, correctly
```

---

## 10. What is not done here

**The tool layer still returns hardcoded data.**
`agent/src/chip_chat/agent/tools.py` answers `search_menu_knowledge` from three
in-memory menu items. Wiring `KnowledgeLane` into it belongs to
[#61](https://github.com/gganssle/chip_chat/issues/61), which is what this issue
unblocks; the interface it will call is `KnowledgeLane.search(query)` and the
span it already opens is the right one.

**Nothing here measures recall — and [#50](https://github.com/gganssle/chip_chat/issues/50)
now does.** `eval/retrieval` is the labeled set and the ablation: forty
questions, the published places that answer each one, and `recall@3`, `hit@3`,
MRR and P@1 per category under keyword only, vector only, hybrid, and hybrid +
reranker. Read [`eval/retrieval/README.md`](../eval/retrieval/README.md) before
its numbers.

The ablation needed two configurations this layer could not express, so
`chip_chat.search.query.Halves` exists — its docstring is the argument for its
own existence and names the one caller allowed to pass it; nothing on a serving
path may.

**Section 2's claim is not confirmed, and the reason is §9.** Three live sweeps
of the same forty questions have now been run against three equivalent corpora,
and the arms sort into two groups. `recall@3`, all categories:

| Arm | sweep 1 | sweep 2 | sweep 3 |
|---|---:|---:|---:|
| keyword only | 84% | 84% | 84% |
| hybrid + reranker | 95% | 95% | 91% |
| hybrid | 53% | 84% | 84% |
| vector only | 41% | 7% | 83% |

The first two rows are a measurement. The second two are the vector half's
availability that afternoon: 41% and 7% and 83% is not a retriever changing its
mind, and sweep 2 fell where it did because §9's failure had set in partway
through the first category and held for the other four. So *hybrid is not a
hedge* remains an argument from RFC-001 §08 — a good one — rather than a fact
this repository has measured.
Sweep 1's headline read the vector arm's zeros on the three menu-row categories
as embeddings failing on proper nouns exactly as the RFC predicted; sweep 3 has
vector-only at 80% on ingredients, 100% on nutrition and 83% on allergens, which
says sweep 1 was reading a service fault as a finding. **A number that is not
reproducible is not evidence, whichever way it points.** The ablation is
repeatable — that is #50's fourth criterion and it holds — so this becomes
measurable again the day the service does.

Those three tables are the last ones that can be printed this way. §9.1 is what
changed, and **sweep 4 is what it produced.** Taken the same day against the same
release, through the shipped detector, it does not add a fourth column here — it
marks three of the four arms **not comparable** and names the questions that made
them so:

| Arm | questions | vector half dropped | comparable |
|---|---:|---:|---|
| keyword only | 40 | 0 | yes |
| vector only | 40 | 8 | **no** |
| hybrid | 40 | 13 | **no** |
| hybrid + reranker | 40 | 12 | **no** |

`keyword only` is zero because it asks for no vector half and therefore cannot
lose one. The other three are why the rows above move by a factor of eleven.
Whether §2's claim is confirmed still waits on a rested service — but a sweep can
no longer *appear* to answer it, and the demo bar in
[`eval/retrieval/BASELINE.md`](../eval/retrieval/BASELINE.md) now reads 100% over
**four** allergen questions with the other four named, rather than 100% over
eight with three of them quietly lexical-only.

**The degrade path is not distinguishable from keyword-only, which is itself the
finding.** `hybrid` came out equal to `keyword only` in every single cell of
sweeps 2 and 3, and on a working vector half that would be a surprise. It is not
one: a hybrid response whose vector half returned nothing *is* the keyword
response, and §9's arithmetic tell — a fused top score of 0.0167 rather than
0.0321 — was observed on a live hybrid query in the same minute that the same
question's vector-only query answered normally. What the degrade path costs is
therefore still unpriced. **cc-t1o1** holds the question, whose most interesting
candidate is falling back to *keyword* rather than to *hybrid*; on the evidence
here that fallback is exactly what was happening, and §9.1 is now the thing that
says so on each individual query rather than in hindsight across a table.

**And section 5's floor is too low.** This one is stable across all three
sweeps and is a real result. Restraint on the eight questions the corpus cannot
answer measured 25% / 75% / 25% / **12%** on sweep 3, worst under the arm
production sends: seven of eight came back grounded, including *"which items are
safe for a peanut allergy"* against four published marks that do not include
peanut. `PROVISIONAL_RERANKER_FLOOR = 1.5` is what that is, and this is the
measurement its docstring was waiting for. Filed as **cc-sans**; **cc-mpdu** is
the same question on the degrade path, where confidence comes from the lexical
floor instead. The two are thresholds on one question and should be chosen
together, over three or four sweeps at three or four floors — `--floor` is a run
parameter, recorded at the top of every report, for exactly that.

**What did survive all three sweeps is the one #50 asks for.** Top-3 recall on
the allergen questions under `hybrid + reranker`: **100%, 100%, 100%.** The
reranked arm is the stable one because the semantic ranker reorders whatever
union it is handed and the lexical half is always in that union — so the demo
criterion is measured on the arm the defect cannot reach.

**Chunk ids are assumed stable across a rebuild, and nothing checks it.** D9
raises it directly: a rebuilt index that renumbered chunks would invalidate
citation ids mid-conversation, which is a live risk here because the corpus is
re-harvested weekly and the index is rebuilt rather than patched. Nothing in this
package can settle it — `chunk_id` is minted by the gold layer's chunk renderers
(#35), which are not on `main` yet, and the assertion belongs where the ids are
made rather than where they are read. Filed as **cc-g9bm**.

The eval does *not* wait for it, and the way it does not is worth knowing about.
A chunk id is a content hash, so a labeled retrieval set keyed on ids would go
uniformly wrong on exactly the rebuild that is at issue here. `eval/retrieval`
therefore labels a **place** — a kind plus the published fields that identify the
passage a person would point at — and resolves those against the corpus at run
time. A label that stops resolving is the regression, by name, in a diff of the
committed baseline. That is a check on the *corpus*; cc-g9bm is still the check
on the ids a conversation is already holding, and neither substitutes for the
other.
