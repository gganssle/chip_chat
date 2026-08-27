# search

The retrieval index. Issue [#48](https://github.com/gganssle/chip_chat/issues/48),
RFC-001 §08: **the index is rebuilt, never patched.**

Each build creates a new index named after the corpus release it holds, fills
it, checks it, and points the `corpus` alias at it in one write. The application
knows the alias and has never been told an index name.

The write-up is [docs/retrieval-index.md](../docs/retrieval-index.md) — read
that for the decisions. This is the map.

| Module | Holds |
|---|---|
| `chunks.py` | The chunk metadata schema #35 fixed, restated so the index can be built from it, and the two places an index is allowed to disagree with a Delta table. |
| `schema.py` | The index definition, as a pure function of the above. Printable with `make search-schema`, free and with no credential. |
| `embedding.py` | One deployment, read by the build *and* by the index's query-time vectorizer, so the two cannot embed into different spaces. |
| `documents.py` | One chunk row becomes one document, or the build stops. The interesting content is what it refuses. |
| `corpus.py` | Resolving the chunks through the harvest's release pointer, which is what makes the alias swap and the corpus swap the same swap. |
| `client.py` | The nine calls a rebuild makes, behind a protocol a test can implement. |
| `build.py` | The order of operations the four failure properties follow from. |
| `verify.py` | #48.3 and #48.4 against the live service, because both are claims about time. |

```bash
make search-schema      # free, no credential, no network
make search-status
make search-build
make search-rollback
make search-verify
```

Nothing here is in `make ci` except `search/tests`, which builds the same
31-chunk corpus end to end against an in-memory service. The live targets need
`az login`, the two data-plane roles `search.tf` grants, and a Foundry key from
Key Vault for the index's vectorizer — and a gate that needs a credential is not
a gate.
