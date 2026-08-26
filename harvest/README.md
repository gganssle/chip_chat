# harvest

Public menu, nutrition, and policy ingestion.

This package is the framework the source-specific harvesters sit on. The
manners live here — reading `robots.txt` and obeying it, a politely slow
request rate, an honest User-Agent, and caching every response so a URL is
fetched exactly once — so that no individual harvester has to remember them,
and none can quietly skip them.

## Using it

```python
from pathlib import Path

from chip_chat.harvest import Harvester, HttpxTransport, LocalBlobStore

with Harvester(LocalBlobStore(Path("landing")), HttpxTransport()) as harvester:
    menu = harvester.fetch_json("https://example.com/api/menu")
    page = harvester.fetch("https://example.com/nutrition")

print(page.source_url, page.harvested_at)
```

The second run of that program makes no network requests at all.

Prefer `fetch_json` against the endpoints a site's own front end calls over
`fetch` against its HTML. Those endpoints are more stable than the markup
around them, far lighter to fetch, and arrive structured instead of scraped.

## What the framework does on every fetch

1. **Reads `robots.txt` for the origin and obeys it.** A disallowed path
   raises `RobotsDisallowedError`. Nothing catches it and fetches anyway. If
   `robots.txt` cannot be read at all — a 5xx, a dead connection — the
   framework refuses everything for that origin rather than guessing in the
   permissive direction. A 404 means the site published no rules, which does
   mean everything is permitted. Rules are re-read after a day.
2. **Looks in the cache.** A hit returns without touching the network.
3. **Waits at the politeness gate.** A real delay since the last request
   (two seconds, or longer if the site declares a `Crawl-delay`), and a
   process-wide ceiling on requests in flight. Both live on the shared
   `GLOBAL_GATE`, so adding a fourth harvester cannot triple the request rate
   a site sees.
4. **Fetches, with backoff.** Timeouts, 429s and 5xxs are retried with
   exponential backoff, honouring `Retry-After`. A 4xx is never retried.
5. **Writes the raw bytes to the landing zone**, untouched and unparsed, with
   `source_url` and `harvested_at` recorded alongside. Those two fields are
   captured here, at the edge, because by the time a chunk reaches the
   retrieval index there is nowhere left to recover them from — and RFC-001
   section 08 requires them to survive into the response payload as citations.

## The cache

Bodies are stored under the SHA-256 of their own bytes, with one small JSON
pointer per URL naming the digest it currently resolves to. Re-harvesting an
unchanged page therefore writes nothing, and re-harvesting a changed one
writes a new blob *beside* the old one and records the digest it replaced.
A weekly re-harvest can diff rather than blindly overwrite.

`BlobStore` is the storage seam. `LocalBlobStore` writes a directory tree and
`InMemoryBlobStore` writes nothing at all; the ADLS Gen2 raw landing zone
plugs in behind the same four methods without a harvester noticing.

## Testing against it

`chip_chat.harvest.testing` ships `FakeTransport` and `FakeClock`. Use them —
a harvester test must never fetch from a real site, and a rate-limiter test
must never actually sleep.

```python
from chip_chat.harvest import Harvester, InMemoryBlobStore
from chip_chat.harvest.testing import FakeClock, FakeTransport, fake_response

transport = FakeTransport({"https://example.test/api/menu": fake_response(...)})
harvester = Harvester(InMemoryBlobStore(), transport, clock=FakeClock())
```

`transport.requests` records every call, which is how `test_harvester.py`
proves that a warm cache makes zero of them.
