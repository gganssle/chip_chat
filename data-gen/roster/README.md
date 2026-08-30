# The roster the live account is supposed to be holding

Three of the six tables `chip_chat.data_gen` writes, plus the manifest that
names the generation all six came from. `make snowflake-load-roster` loads this
directory into `CHIP_CHAT.ACCOUNTS`; `data-gen/tests/test_roster.py` holds it to
what the shipped `population.toml` generates; and
`data-gen/tests/export_shipped_roster.py` projects `persona_fixtures.jsonl` into
the copy the API image carries. Nothing else in this repository reads it.

| File | Table | Rows |
| --- | --- | --- |
| `personas.jsonl` | `ACCOUNTS.personas` | 7 |
| `persona_fixtures.jsonl` | `ACCOUNTS.persona_fixtures` | 28 |
| `demo_visitors.jsonl` | `ACCOUNTS.demo_visitors`, and `ACCOUNTS.demo_visitor_baseline` from the same file in the same run | 500 |
| `manifest.json` | — | the seed, both input digests, and a SHA-256 per table for all six |
| `inputs/` | — | the catalogue and policy tables this generation came from |

## Why these three and not all six

Because these three are the only ones a human loads. [#39]'s nightly publish
writes `orders`, `order_items` and `loyalty_ledger` into `ACCOUNTS` out of
Databricks silver and cannot see the roster at all — the publish role is not
granted it — so `chip_chat.snowflake.load` is the sole route in for
`personas`, `persona_fixtures` and `demo_visitors`
(docs/snowflake-schema.md §7). Two loaders, two halves of one population, and
until this directory existed nothing that made them name the same generation.

On 2026-08-27 they did not name the same generation. The account held a history
generated for five hundred customers and a roster generated for sixty. Twenty-
eight `persona_fixtures` rows therefore described customers whose orders and
points belonged to somebody else — `demo-0048`'s fixture claimed eighty orders
and 397 points while the tables held thirty-one orders and 1,363 points — and
because the opening message is composed from the fixture's narrative while
`get_points_balance` sums the ledger, a visitor could see both numbers inside a
single conversation. `docs/snowflake-schema.md` §9 is the write-up.

The other three tables are not committed here because they are seventeen
megabytes and because they are reproducible from this repository byte for byte:
the population is generated from `inputs/` and the shipped `population.toml` at
seed 20260826, and the digests in `manifest.json` are what make "reproducible" a
claim you can check rather than one you have to believe.
`data-gen/tests/test_roster.py` checks it on every `make ci`.

## `inputs/`

The catalogue and the three policy tables the generation reads, 1.4 megabytes,
laid out exactly as a landing zone lays them out:

```
inputs/catalog/chipotle/*.jsonl          the built catalogue, 192 items
inputs/parsed/chipotle/policy/*.jsonl    rewards, policy_sections, faq_entries
```

They are here because of [#106]. Until then the roster was generated from the
*test* fixtures — the two-entree catalogue under `catalog/tests/fixtures/` and
the harvest tests' recorded site — and the account was loaded from the same
small generation, so nothing was wrong with comparing them. #106 replaced the
account's catalogue with the real harvest, so a test regenerating from the
fixture would have been asserting that two different generations were the same
one, and the input that makes the roster reproducible had to travel with it.

Refreshing them is the same deliberate act as retuning `population.toml`, and
has the same consequences: re-harvest, rebuild the catalogue, regenerate,
re-export all four files here, re-land the account and re-run the nightly
publish. `docs/menu-data.md` is that sequence written out.

## Re-exporting it

Only after a deliberate retune, or a deliberate refresh of `inputs/`. Either one
changes the population, which changes every table, which makes the *whole*
account stale — including the gold marts, which are computed from `orders` in
Databricks. The export is the easy half; re-landing the account and re-running
the nightly publish is the rest of it, and the sequence is in
`docs/snowflake-schema.md` §9 and `docs/menu-data.md`.

```bash
cp landing/accounts/synthetic/{personas,persona_fixtures,demo_visitors}.jsonl \
   landing/accounts/synthetic/manifest.json data-gen/roster/
cp landing/catalog/chipotle/* data-gen/roster/inputs/catalog/chipotle/
cp landing/parsed/chipotle/policy/{rewards,policy_sections,faq_entries}.jsonl \
   data-gen/roster/inputs/parsed/chipotle/policy/
uv run python data-gen/tests/export_shipped_roster.py
uv run pytest data-gen/tests/test_roster.py api/tests/test_shipped_roster.py
```

## The third copy

There is one more copy of `persona_fixtures`, and it is downstream of this one:
`api/src/chip_chat/api/fixtures/persona_fixtures.json`, which the API image
carries and `chip_chat.api.visitors.shipped_roster` reads when `build_service` is
called without a Snowflake connection factory — a local run with no credentials.
`docs/decisions/shipped-persona-roster.md` is why it exists.

It is the reason `export_shipped_roster.py` is in the sequence above rather than
a step somebody remembers. On 2026-08-28 that file was still holding the
sixty-customer generation this directory was committed to replace, and two of its
twenty-eight `demo_id`s existed in both generations naming *different* customers
— a stale roster that was not visibly stale, because every row in it was
well-formed and carried a plausible sentence. The exporter reads
`persona_fixtures.jsonl` and projects it onto the eleven columns the entry flow
reads, in the order the roster query returns them; it computes nothing, so the
two files are one generation or the export is wrong.
`api/tests/test_shipped_roster.py` is what says which.

[#106]: https://github.com/gganssle/chip_chat/issues/106

[#39]: https://github.com/gganssle/chip_chat/issues/39
