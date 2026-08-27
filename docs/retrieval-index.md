# The retrieval index, and the one write that makes a corpus live

**Issue:** [#48](https://github.com/gganssle/chip_chat/issues/48) ·
**Code:** `search/src/chip_chat/search/` ·
**RFC:** [rfc-001.md](rfc-001.md) §08

RFC-001 §08 does not leave this open: *the index is rebuilt, never patched. The
weekly re-harvest produces a new index alias-swapped into place, so a partial
harvest can never leave the corpus half-updated.* That rule is already applied
one layer down — a harvest run stages everything under its own prefix and
publishes by writing one pointer ([corpus-freshness.md](corpus-freshness.md)) —
and this is the same rule at the index, deliberately the same rather than one
that resembles it.

```bash
make search-schema      # the index definition. Free, no credential, no network
make search-status      # what the service holds, and what the alias serves
make search-build       # a new index from the live corpus release, then swap
make search-rollback    # put the previous index back. One alias write
make search-verify      # hold the live service to #48.3 and #48.4
```

---

## 1. The shape

```
corpus/current.json          the harvest's pointer: names one run
  └─ 20260827T053000Z        the run id
       └─ chunks/*.jsonl     the chunk export the build reads

corpus-20260827t053000z      the index built from it
corpus                       THE ALIAS. One write of this is the swap
```

The index is named after the corpus release it holds, so the alias swap and the
release swap are the same swap. Given a live alias you can read which harvest is
being served; given a harvest you can name the index that would serve it. Neither
needs a lookup table that could be wrong.

**The application only ever knows `corpus`.** `AZURE_SEARCH_INDEX_ALIAS` is what
`compute.tf` puts in its environment, and no index name appears anywhere in the
app, in Terraform, or in this repository outside the build.

A second build of the *same* release — a schema change, a new embedding model,
neither of which re-harvests anything — takes `corpus-20260827t053000z-2`. The
ordinal only appears when it means something.

---

## 2. What a document carries

Every column of the chunk schema #35 fixed, and one field that is not a chunk
field: the vector. `chip_chat.search.chunks` restates that schema and
`search/tests/test_chunk_contract.py` asserts the two are the same list — the
same convention `gold.py` uses for the constants it shares with `silver.py`.

Three places the index deliberately disagrees with the table:

| | Delta | Index | Why |
|---|---|---|---|
| `calories` | `DECIMAL(8,2)` | `Edm.Double` | Azure AI Search has no decimal type. Published calorie figures are integers and every integer to 2⁵³ is exact as a double, so no current number changes value; what is lost is the guarantee, and `calories` is filtered on rather than summed. |
| `allergens` | filterable | filterable, **never searchable** | A searchable allergen field scores the dairy items highest for *"something without dairy"*, because free text has no idea the query negated it. The product question is `allergens/any(a: a eq 'dair')`, and a filter is exact. |
| `text_vector` | — | `stored: false`, int8 | The caller wants the chunk, not the 1536 floats that found it. |

**Every field is retrievable**, which is #48's own sentence — *"these fields are
retrievable and not merely filterable"* — because a citation the application
cannot read back is not a citation.

`chip_chat.search.documents` refuses three things before a document is uploaded,
and the refusals are the second acceptance criterion made true rather than
hoped for:

- a `source_url` that is not `http`/`https`. *Resolvable* is the issue's word.
- a `harvested_at` with no UTC offset. The service would guess one, and that
  timestamp is rendered to a visitor beside a published allergen claim.
- a field the chunk schema does not declare. That is how a rename gets halfway:
  the new name arrives, the old one is still in the index, and every filter on
  it silently matches nothing.

It does **not** refuse a missing `heading`. Every kind of chunk has the column
and plenty of published sections have no heading in them; refusing those for
tidiness would refuse a third of the policy corpus. That distinction —
*universal* is a claim about the table, *required* is a claim about the document
— was found by the first live build, not by a test.

**And the live index was read back and checked, which is a different claim.**
The refusals above are properties of the build. #48's second criterion is a
property of the *corpus that is serving*, so on 2026-08-27 every document was
pulled off the alias and held to it:

```
alias corpus: 31 documents, 31 read back
  documents with no source_url      0
  source_url that is not http(s)    0
  documents with no harvested_at    0
  harvested_at with no UTC offset   0
  distinct source_url               4
```

The four are the four pages the harvest reads, and each was then fetched over
the wire, because *resolvable* is a claim about the internet and nothing in a
build can check it without making a build depend on the internet being up:

| | `source_url` | chunks |
|---|---|---:|
| 200 | `https://www.chipotle.com/graphql/execute.json/chipotle/FAQ-Query;region=en-us` | 11 |
| 200 | `https://www.chipotle.com/rewards-terms` | 7 |
| 200 | `https://www.chipotle.com/rewards` | 3 |
| **401** | `https://services.chipotle.com/menuinnovation/v1/restaurants/0679/onlinemenu?…` | 10 |

The 401 is the honest reading of *resolvable* rather than a failure of it. The
menu API is the restaurant's own ordering endpoint and it wants the subscription
key the harvest holds; DNS, TLS and HTTP all worked, and the URL names the exact
document the figure came from. A citation is a provenance record, not a link a
visitor clicks — [decisions/citation-presentation.md](decisions/citation-presentation.md)
is where that distinction is settled — and the ten menu-row chunks behind it are
the ones carrying published calorie and allergen marks, which is precisely where
a provenance record has to be exact.

---

## 3. Integrated vectorization, and the half of it this estate cannot have

Integrated vectorization is two things usually discussed as one.

**Query time** — the application sends the index *text*, and the service embeds
it. That is configured here and it works: this query carries no vector.

```bash
curl -s -X POST "$AZURE_SEARCH_ENDPOINT/indexes/corpus/docs/search?api-version=2025-08-01-preview" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
    "search": "how do I earn rewards points",
    "vectorQueries": [{"kind": "text", "text": "how do I earn rewards points",
                       "fields": "text_vector", "k": 10}],
    "select": "chunk_id,heading,source_url,harvested_at"}'
```

```json
{"heading": "Okay, so how does Chipotle Rewards work?",
 "source_url": "https://www.chipotle.com/graphql/execute.json/chipotle/FAQ-Query;region=en-us",
 "harvested_at": "2026-01-01T12:00:00Z"}
```

This is the half that has to be right, because it is the half that fails
silently. A query embedded by a different model — or the same model at a
different `dimensions` — is not a worse query, it is a query in a *different
vector space*, and the index answers it with confident nonsense rather than an
error. `EmbeddingDeployment` is one object that produces both the build's
embeddings request and the index's `vectorizer` declaration, so the two cannot
disagree.

**Index time** — the textbook shape is an indexer with a skillset: the service
pulls the documents itself and an `AzureOpenAIEmbedding` skill vectorizes them.
**That shape is unavailable on this estate, and not because of a choice
anybody made.** Two facts, both read off the live estate on 2026-08-27:

```bash
az search service show -g rg-chip-chat -n srch-chip-chat-4cy39i \
  --query "{sku:sku.name,identity:identity}"      # Free, identity: null
az storage account show -g rg-chip-chat -n stchipchat4cy39i \
  --query allowSharedKeyAccess                     # false
```

The Free tier gives a search service no **outbound** managed identity, which is
the only keyless way an indexer authenticates to a data source. And the storage
account is created with shared keys disabled (`storage.tf`, issue #8), so there
is no account key, no connection string and no account SAS to give it instead.
An indexer therefore cannot reach this corpus *at all* — not with a weaker
credential, with none.

The alternatives are Basic ($73.73/month, which buys the service an identity)
and re-enabling shared keys on the storage account, which would put back a
credential #8 deliberately removed. Neither is worth it while the corpus is one
50 MB index, so the build pushes documents and calls the embedding deployment
for their vectors — and the query half is configured exactly as it would be
otherwise. If the corpus ever outgrows a push, the tier is a one-line change to
`var.search_sku` and this is the paragraph to re-read.

**The vectorizer needs a key, and that is a stated exception.** With no
identity, the service authenticates to Foundry with an API key, which the build
reads out of Key Vault and writes into the index definition. RFC-001 §05 leans
on identity-bound access everywhere else; this is the one place on the retrieval
lane where it cannot. Building without it is possible and is a deliberate flag
(`--no-vectorizer`) rather than a silent fallback, because a keyless vectorizer
is *accepted at index creation* and then fails at every query.

---

## 4. The API version is a preview one

Everywhere else in this repository an Azure API version is pinned to the newest
GA. Here it is `2025-08-01-preview`, and the reason is not carelessness:

```
              /indexes   /aliases
2023-11-01       200        400
2024-07-01       200        400
2025-09-01       200        400
2024-09-01-preview  200     200
2024-11-01-preview  200     200
2025-08-01-preview  200     200
```

*Probed against the live service, 2026-08-27.* Every GA version answers
`/aliases` with **400, "The version indicated by the api-version query string
parameter does not exist"**. So the choice is not "GA or preview", it is "an
alias or no alias" — and without an alias the application has to be told an
index name that changes every week.

Two consequences worth holding onto. This is the one place in the estate where a
Microsoft-side change can break a deployment without anybody editing a file, so
`chip_chat.search.schema.API_VERSION` is the first thing to check when a build
starts failing for no local reason. And the exposure is bounded by the same
property the rest of the design turns on: an index is rebuilt rather than
migrated, so moving to a different API version is a build and a swap.

---

## 5. Three indexes, and where they go

The Free tier allows **three indexes, three indexers, three data sources, three
skillsets and 50 MB** — read off `/servicestats`, not off a page. The alias
pattern spends two of the three by construction: the live index and the one
being built. The third is headroom.

That budget is what decides two things that would otherwise look arbitrary.

**A build prunes before it creates**, deleting every index this package built
that the alias is not pointing at. It does *not* spare a rollback target and
does not need to — the index the build is about to replace is live right now, so
it is exempt right now, and it becomes the rollback target the moment the swap
demotes it. Steady state is two of three.

**A failed build deletes its own partial index**, which is the one place the
corpus's rule is inverted. `corpus-freshness.md` leaves a failed harvest run on
disk on purpose — *"a failure you can read is worth more than a failure you
rolled back"* — and a blob store has no cap. Three indexes is a cap, and a
partial index that outlives its build is the newest index the alias is not
pointing at, which is precisely what a rollback would choose. The diagnosis goes
in the error instead; `--keep-failed` buys the generosity back for the run where
somebody actually wants to query the wreckage.

**Measured, 30 documents:**

| | bytes | per chunk |
|---|---|---|
| total storage | 271,396 | ~9.0 KB |
| vector index | 47,880 | ~1.6 KB |

1,536 dimensions at int8 is 1,536 bytes plus overhead, so the scalar
quantization is doing exactly what it claims — full-precision vectors would be
6 KB each. At ~9 KB per chunk with two indexes resident during a build, 50 MB is
roughly **2,900 chunks**. `stored: false` on the vector field is the other half
of that number: it drops the JSON copy the service keeps in order to be able to
return a vector to a caller that never asks for one.

---

## 6. The two criteria that are claims about time

```
#48.3  an alias swap is atomic from the application's point of view --
       verified by querying continuously across a swap
#48.4  a deliberately failed build leaves the live alias pointing at the
       previous good index
```

`make search-verify` is the run. It builds one index, swaps to it, then builds a
second **of a different size** while a thread queries the alias fifty times a
second, and finally runs a build that fails on purpose. The size difference is
what makes an observation unambiguous: a response reporting 31 documents came
from the old index and one reporting 30 came from the new one, and a response
reporting anything else would be a corpus assembled from both.

```
#48.3  alias swap: atomic
  documents    31 -> 30
  queries      90 across the swap
  failures     0
  half-updated 0
  propagation  0.16s from the alias write to the first response from the new index
  note         index corpus-20260827t060000z-2 replaced corpus-20260827t060000z
#48.4  failed build: the live alias held
  corpus -> corpus-20260827t060000z-2
  before       corpus-20260827t060000z-2
  serving      30 documents
  failure      POST /indexes/corpus-20260827t060000z/docs/index returned 400:
               InvalidName ... Document key cannot be missing or empty
  remains      (deleted with its build)
```

**What "atomic" is claiming.** Not that the swap is instantaneous — Microsoft
documents alias changes as taking up to ten seconds to propagate, and this run
measures the window rather than pretending it is zero. It landed at 0.16s. The
claim is the one the application cares about: across the window every query
succeeded and every response came entirely from one index or entirely from the
other. There is no moment at which the corpus is half-updated.

The same two properties are asserted in `make ci` against an in-memory service
(`search/tests/test_build.py`), and the two are not redundant. The fake proves
the build is correct against a model of the service; this proves the model is
right.

**Run twice, on purpose.** The run above is the first. Here is the second, an
independent `make search-verify` on the same service later the same day:

```
#48.3  alias swap: atomic
  documents    31 -> 30
  queries      66 across the swap
  failures     0
  half-updated 0
  propagation  0.16s from the alias write to the first response from the new index
  note         index corpus-20260827t053000z-2 replaced corpus-20260827t053000z
#48.4  failed build: the live alias held
  corpus -> corpus-20260827t053000z-2
  before       corpus-20260827t053000z-2
  serving      30 documents
  failure      POST /indexes/corpus-20260827t053000z/docs/index returned 400:
               InvalidName ... actions : 0: Document key cannot be missing or empty
  remains      (deleted with its build)
```

Different release, different index names, a different number of observations
because the watch runs on wall-clock rather than on a count — and the same two
verdicts, the same zero failures, the same zero half-updated responses and the
same 0.16 s propagation. Two runs is not a distribution, but it is the
difference between a measurement and an anecdote, and 0.16 s appearing twice
against a documented ceiling of ten seconds is worth having on the record.

---

## 7. Four things the live service does that the documentation does not say

Each of these cost a run to find, and each is a comment in the code now.

**Aliases are preview-only.** §4. The first build died on
`GET /aliases/corpus` with 400 against a GA version that answers `/indexes`
happily.

**Updating an existing alias returns 204, not 200.** Creating one returns 201.
`(200, 201)` is the obvious guess and it fails on the *second* build rather than
the first, which is the worst moment to find out.

**Indexing is acknowledged, not completed.** An upload request returns when the
service has accepted the documents; they become countable shortly afterwards. A
31-chunk load in ten-document requests counted **10** immediately after the last
one and 31 a second later — which read exactly like a load that had lost two
thirds of the corpus. The count is the gate in front of the alias write, so
`build._settle` waits for it: a build that treated the lag as a short load would
refuse to swap a corpus that was complete.

**A malformed key fails the whole request, not the document.** The API reports
per-document failures with HTTP 207 and a status per key, so the natural
expectation is that a bad document is skipped and the rest land. An empty key is
not one of those: it fails the request with `400 InvalidName`, and the documents
beside it in that request do not land. Documents in *earlier* requests are
already in, which is how a build fails halfway — and it is why
`make search-verify` lowers the upload batch to ten. At the default of 1,000
this corpus is a single request, the failed index ends up empty, and "a partial
harvest" is not what was demonstrated.

---

## 8. What is not done

**The chunk export does not exist yet.** The build reads
`corpus/runs/<run_id>/chunks/*.jsonl`; gold builds
`chip_chat.gold_harvested.corpus_chunks` as a Delta table in Unity Catalog,
which a build on a laptop cannot read without a cluster. Writing that export is
a step of the gold pipeline and is tracked as **cc-2yw**. Until it lands,
`make search-build CHUNKS=<dir> RUN_ID=<id>` reads a directory directly — which
is how every run in this document was made, against
`search/tests/fixtures/chunks.jsonl`: 31 chunks rendered from the parsed tables
in the live landing zone, with real item names, real FAQ questions and real
`source_url` values.

**The contract test is live.** `search/tests/test_chunk_contract.py` asserts
that this package's chunk schema is `chip_chat.databricks.gold_chunks.FIELDS`,
field for field. It used to skip: `gold.py` on `main` was #36's four marts and
#35's chunk renderers were on an unmerged branch under the same module name, so
the index was being built against a chunk schema nothing verified it matched.
#35 has since landed as `gold_chunks.py` — see
[corpus-chunking.md](corpus-chunking.md) — the test imports it directly, and any
drift between the two schemas now fails `make ci`. That was **cc-6rb**.

**Hybrid retrieval and the reranker are #49's, and they have landed.** The index
carries the semantic configuration and the scoring profile — an index is rebuilt
rather than altered, so leaving them out would have meant #49 could not turn
reranking on without rebuilding the corpus — and nothing *here* chooses a query
shape. What does is `chip_chat.search.{query,retrieve,allowance,lane}`, written
up in [retrieval.md](retrieval.md): hybrid always, reranked while the Free
tier's **1,000 requests a month** last, and hybrid-without-reranking after that,
because past the ceiling the API returns a billing error rather than a charge.

**Recall is measured, and the demo number is 100%.** The system design's demo for
this phase is *"top-3 recall on your allergen questions, measured, with
numbers"*, and [`eval/retrieval`](../eval/retrieval/README.md) is #50's answer to
it: forty labelled questions swept through the retriever under four
configurations with no model in the loop. Against this index, under the
configuration production runs, top-3 recall on the allergen questions is **100%**
— on three separate sweeps.

Two things that measurement found belong here rather than there. The first is a
service defect that this document's §3 came close to but did not catch: a vector
query against this Free-tier service returns an empty result set with HTTP 200
and no warning, at a rate that climbs with use, which makes the *vector* half of
a hybrid query unreliable while leaving the lexical half untouched. It is
eliminated against the vectorizer, the deployment, four API versions and both
compression settings in [retrieval.md](retrieval.md) §9, and it is **chip-wez**.
The second is that a sweep must follow a `make search-build` and not a
`make search-verify`: verify deliberately leaves the *smaller* of its two indexes
live, so a sweep run straight after it scores the retriever against a corpus that
is one chunk short. The report says which chunk, which is how that was caught.
