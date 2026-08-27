# search

The knowledge lane, both halves. Issues
[#48](https://github.com/gganssle/chip_chat/issues/48) and
[#49](https://github.com/gganssle/chip_chat/issues/49), RFC-001 §08.

**The corpus: the index is rebuilt, never patched.** Each build creates a new
index named after the corpus release it holds, fills it, checks it, and points
the `corpus` alias at it in one write. The application knows the alias and has
never been told an index name.

**The query: hybrid, reranked while the month's allowance lasts, cited either
way.** Keyword and vector together on every question, because item names are
proper nouns; the semantic ranker on top of that until the Free tier's 1,000
requests a month run out, and hybrid alone afterwards, because past the ceiling
the API returns a billing error rather than a charge.

The write-ups are [docs/retrieval-index.md](../docs/retrieval-index.md) and
[docs/retrieval.md](../docs/retrieval.md) — read those for the decisions. This is
the map.

| Module | Holds |
|---|---|
| `chunks.py` | The chunk metadata schema #35 fixed, restated so the index can be built from it, and the two places an index is allowed to disagree with a Delta table. |
| `schema.py` | The index definition, as a pure function of the above. Printable with `make search-schema`, free and with no credential. |
| `embedding.py` | One deployment, read by the build *and* by the index's query-time vectorizer, so the two cannot embed into different spaces. |
| `documents.py` | One chunk row becomes one document, or the build stops. The interesting content is what it refuses. |
| `corpus.py` | Resolving the chunks through the harvest's release pointer, which is what makes the alias swap and the corpus swap the same swap. |
| `client.py` | The nine calls a rebuild makes, behind a protocol a test can implement — and the connection pool that is the whole latency budget. |
| `build.py` | The order of operations the four failure properties follow from. |
| `verify.py` | #48.3 and #48.4 against the live service, because both are claims about time. |
| `query.py` | One visitor sentence becomes one hybrid query, and the three constraints it refuses to guess at rather than approximate. |
| `retrieve.py` | The single interface the tool layer calls: passages, every score that ranked them, and how much the corpus actually had to say. |
| `allowance.py` | The Free tier's 1,000 semantic requests a month, counted, so an eval sweep cannot spend them without noticing. |
| `lane.py` | The `retriever.search` span, and the lane that declines by itself rather than taking a turn with it. |

```bash
make search-schema      # free, no credential, no network
make search-status
make search-build
make search-rollback
make search-verify
make search-retrieve Q="how do rewards points work"   # spends one semantic request
```

Nothing here is in `make ci` except `search/tests`, which builds the same
31-chunk corpus end to end against an in-memory service and then asks it
questions. The live targets need `az login` and the data-plane roles `search.tf`
grants — plus, for a build, a Foundry key from Key Vault for the index's
vectorizer — and a gate that needs a credential is not a gate.

`make search-retrieve` is the exception worth knowing about: it needs no
embedding deployment and no key, because the *index* holds the vectorizer and a
query is therefore text. It does spend one of the month's 1,000 semantic
requests, which is why the count lives in a file under the landing root rather
than in a process. `RERANK=0` runs the degrade path and spends nothing.
