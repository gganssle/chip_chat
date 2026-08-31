# Decision: a citation carries two URLs, and only one of them is a link

**Issue:** [#111](https://github.com/gganssle/chip_chat/issues/111) · **Decided:** 31 August 2026, by Graham · **Measured:** 31 August 2026
**Changes:** the citation payload (`chip_chat.search.retrieve.Passage.citation`), `chip_chat.agent.envelope.Citation`, the widget's `renderSources`
**Does not change:** D9, which says citations are a field the app draws rather than a sentence the model wrote. That is still the rule and this is downstream of it.

---

A user-testing session on 31 August 2026 reported the source lines under
answers were opening pages that were not pages. The tester's words:

> It's fine to use the API to look at the data, but if the user's going to be
> taken to a null page, it doesn't look good.

They were right, and the mechanism ran the whole length of the pipeline without
anybody having to make a mistake. `chip_chat.harvest.sources.chipotle.menu`
reads the ordering data from JSON endpoints on `services.chipotle.com`, and
`records.py` writes `source_url` on every row as the endpoint it was actually
read from — *"Every row carries `source_url` and `harvested_at`"*, which is
deliberate and correct as provenance. The value survives bronze → silver → gold,
lands in the search index, comes back on the citation, and `page.py` did this
with it:

```js
const link = el('a', null, citation.source_url);
link.href = citation.source_url;
```

So the field that answers *where was this fact read* was being used to answer
*where should a person go to read it*. Those coincide for the pages harvested as
pages — `/allergens`, `/rewards`, `/rewards-terms`, the locator — and diverge for
everything behind the ordering API, which is the single most common citation the
demo produces.

## The decision

**Two fields. `source_url` is unchanged and stays provenance. A new
`public_url` is what the widget links, and it is empty where no published page
exists — in which case the citation is drawn, dated, and simply does not click.**

The rejected alternative was rewriting `source_url` to a human page at harvest
time. That destroys the thing it is for: a re-harvest diff is only meaningful if
the row records the endpoint that was fetched, and an operator asking "where did
this number come from" would get a marketing page instead of an answer.

The other rejected alternative was suppressing citations that have no public
page. That is worse than the bug — the claim loses its evidence, and PRD K2's
uncited-claim metric would start counting our own rendering choice as a model
failure.

`chip_chat.search.public_url` holds the rules, and `public_url` is derived at
query time rather than at index time. That is worth stating plainly: it means
this fix needed no lakehouse re-run and no re-harvest, and it means the mapping
can be corrected by a deploy rather than by a nightly.

## The rules are host *and* path, which was not obvious

The natural implementation is "block `services.chipotle.com`, allow
`www.chipotle.com`". It is wrong, and it would have shipped this bug half-fixed.
The FAQ content is harvested from
`https://www.chipotle.com/graphql/execute.json/chipotle/FAQ-Query;region=en-us`,
which is on the ordinary public host and answers `200`. A visitor tapping it
gets the same JSON blob the services host would have given them.

## What was measured, on 31 August 2026

Every mapping was checked with a browser user-agent, redirects followed, rather
than reasoned about:

| URL | Status | Content-Type |
| --- | --- | --- |
| `www.chipotle.com/order/build-your-own` | 200 | `text/html` |
| `www.chipotle.com/allergens` | 200 | `text/html` |
| `www.chipotle.com/rewards` | 200 | `text/html` |
| `www.chipotle.com/rewards-terms` | 200 | `text/html` |
| `locations.chipotle.com/` | 200 | `text/html` |
| `catering.chipotle.com/` | 200 | `text/html` |
| `www.chipotle.com/graphql/execute.json/…` | 200 | **`application/json`** |
| `services.chipotle.com/` | 404 | — |
| **`www.chipotle.com/menu`** | **404** | — |

The last row is the reason the table exists. `/menu` is the URL anyone would
write down from memory for a menu item, and it does not exist. Shipping it would
have been the reported bug again with a nicer hostname on it. The page that does
exist is `/order/build-your-own`, which is also what
`databricks/notebooks/lineage_probe.py` already recorded as the human page for a
menu row — so this agrees with a choice the project had made once rather than
inventing a second answer.

The `application/json` row is the reason the status code is not the test. A 200
that serves JSON is a null page as far as a visitor is concerned.

## What was not measured

- **Whether every remaining `public_url` in the live index resolves.** The nine
  URLs above were checked by hand. The mapping is by host and path rather than
  by enumeration, so a source family the harvest adds later gets `None` and
  renders unlinked — safe, but unlinked is not the same as verified, and nothing
  in `make ci` fetches a URL.
- **How many citations the deployment actually renders unlinked.** The
  proportion depends on the mix of questions visitors ask, and the corpus mix is
  not the same as the question mix. It could be measured off
  `render.response`'s citation metadata and has not been.
- **Whether `/order/build-your-own` is the *best* page for a specific item**, as
  opposed to a page that exists and is about the menu. Chipotle publishes
  per-item pages under some paths; none was found that resolved reliably for
  every SKU in the catalogue, so one page for all ordering-API facts is the
  honest choice rather than a per-item guess that 404s on the unusual ones.
