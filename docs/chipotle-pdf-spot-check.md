# Spot check: the PDF path, against the live site and the live service

**Issue:** [#22](https://github.com/gganssle/chip_chat/issues/22) (bead `cc-5h0`) · **Checked:** 26 August 2026
**Checked against:** every page this project harvests, read from `www.chipotle.com`,
`catering.chipotle.com` and `locations.chipotle.com`; and the Document Intelligence
account `di-chip-chat-4cy39i` in `rg-chip-chat`, East US 2.

Issue #22 asks that every harvested PDF gets a structured extraction. This file records
what happened when that was checked against reality, and the answer has two halves that
should not be confused with each other.

---

## 1. Chipotle publishes no PDFs

Not one, on any page this project reads. The sweep looked for the string `pdf` anywhere
in the served markup — not merely for `<a href="…pdf">`, so a sheet behind a script or a
data attribute would still have shown up.

| Page | Status | Occurrences of `pdf` |
| --- | ---: | ---: |
| `https://www.chipotle.com/` | 200 | 0 |
| `https://www.chipotle.com/allergens` | 200 | 0 |
| `https://www.chipotle.com/nutrition-calculator` | 200 | 0 |
| `https://www.chipotle.com/ingredients` | 200 | 0 |
| `https://www.chipotle.com/rewards` | 200 | 0 |
| `https://www.chipotle.com/rewards-terms` | 200 | 0 |
| `https://catering.chipotle.com/` | 200 | 0 |

Two guesses at a conventional address were tried and both 404: `/nutrition-facts` and
`/content/dam/chipotle/global/pdf/nutrition.pdf`. `www.chipotle.com/sitemap.xml` also
404s — the locator publishes its own sitemap, and that one lists store pages only.

So the phrase in the system design — *"Azure Document Intelligence handles any PDF
nutrition sheets"* — describes a case that does not exist today. What Chipotle publishes
as nutrition data, it publishes through the JSON endpoints its own calculator reads, and
issue #20 already harvests those.

**Which is why nothing in this code holds a nutrition-sheet URL.** The harvest re-reads
every document the other three datasets landed, looks for links whose path ends in
`.pdf`, and fetches those. A sheet that appears next month is picked up by the next
harvest without a code change. A remembered URL would have been a 404 waiting to happen,
and would have found nothing today either.

A live run on 26 August 2026 confirms it end to end:

```
$ python -m chip_chat.harvest.sources.chipotle --landing landing --dataset pdf
...
"coverage": { "discovered_urls": 0, "rejected_urls": 0, "unread_urls": 0, "pdfs": 0, ... }
```

Four empty tables and a manifest that says it looked. That is a result, not a failure —
and `test_chipotle_publishes_no_pdfs_and_the_harvest_says_so` fails the day it stops
being true.

## 2. The reader itself was checked against the live service

An empty result proves nothing about the code that would have run. So the path was
exercised for real, against the real account, rather than against a mock of it.

**The account.** `di-chip-chat-4cy39i`, kind `FormRecognizer`, SKU `F0`, in
`rg-chip-chat`, East US 2 — stood up by the Terraform of issue #5. Endpoint
`https://di-chip-chat-4cy39i.cognitiveservices.azure.com/`. The name is worth writing
down because this service has been renamed before: the portal calls it **Azure Document
Intelligence**, the ARM `kind` is still `FormRecognizer`, and the bill says **Foundry
Tools**. All three are the same thing. See
[service-inventory.md §2.5](service-inventory.md).

**Authentication.** No key was read and none exists in this repository. The call is
authorised by an Entra ID token for `https://cognitiveservices.azure.com/.default`,
obtained through `DefaultAzureCredential` — which resolves to `az login` locally and to
the user-assigned managed identity in the deployed container, the one Terraform grants
*Cognitive Services User* on the account.

**The document.** `harvest/tests/fixtures/chipotle/nutrition-sheet.pdf`: a one-page
ruled table of seven items with real published figures. It is a fixture, not a
recording, because — see above — there was nothing to record.

**What came back.** `prebuilt-layout`, API version `2024-11-30`, one page, one table,
eight rows by six columns, forty-eight cells, and every cell correct:

| Item | Serving | Total Calories | Total Fat (g) | Saturated Fat (g) | Sodium (mg) |
| --- | --- | ---: | ---: | ---: | ---: |
| Guacamole | 4 oz | 230 | 22 | 3.5 | 370 |
| Chips | 4 oz | 540 | 25 | 3.5 | 390 |
| Chicken Bowl | 4 oz | 180 | 7 | 3 | 310 |
| Steak Burrito | 4 oz | 150 | 6 | 2.5 | 330 |
| White Rice | 4 oz | 210 | 4 | 0.5 | 350 |
| Black Beans | 4 oz | 130 | 1.5 | 0 | 210 |
| Cheese | 1 oz | 110 | 8 | 5 | 260 |

The service marked row zero as `columnHeader` itself, which is what makes the headings
addressable rather than assumed. That response is checked in verbatim as
`nutrition-sheet-layout.json` — minus the per-word and per-line boxes and the `styles`
array, which nothing here reads and which were four fifths of the file — so every test
of the extraction runs against the service's own field names and omissions rather than
this repository's guess at them.

**And the second call cost nothing.** Running `analyze_once` twice over the same bytes
made one request; the second was served from `analysis/prebuilt-layout/2024-11-30/…` in
the landing zone, beside the raw PDF. Verified in the same session.

## 3. The reconciliation, against real published figures

The sheet's figures are Chipotle's own, taken from the nutrition data issue #20
harvested — with **one deliberate exception**: it prints 260 mg of sodium for Cheese
where the calculator publishes 190. That single planted disagreement is what proves the
comparison notices one.

Twenty-eight comparisons come out of the sheet. Twenty-seven `AGREES`, one `DISAGREES`,
and the disagreement keeps both numbers:

| Field | Value |
| --- | --- |
| `item_id` | `CMG-5252` |
| `nutrient_key` | `sodi` |
| `pdf_value` | 260 |
| `published_value` | 190 |
| `finding` | `DISAGREES` |

Nothing picked a winner. See
[decisions/pdf-tables.md](decisions/pdf-tables.md) for why that is the whole point.

---

## What would change this file

- Chipotle publishing a nutrition or allergen PDF anywhere on the pages above. The
  harvest would find it without a code change; this file's first section would become
  wrong, and `test_chipotle_publishes_no_pdfs_and_the_harvest_says_so` would say so.
- A newer Document Intelligence API version extracting the fixture sheet differently.
  The analysis cache is keyed by model *and* version, so the new answer lands beside the
  old rather than over it, and the two can be diffed.
- The account being renamed or moved. The endpoint is read from
  `CHIP_CHAT_DOCUMENT_INTELLIGENCE_ENDPOINT` or `--document-intelligence-endpoint`, both
  fed from the `document_intelligence_endpoint` Terraform output, so nothing here needs
  editing when it is.
