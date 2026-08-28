# The roster the live account is supposed to be holding

Three of the six tables `chip_chat.data_gen` writes, plus the manifest that
names the generation all six came from. `make snowflake-load-roster` loads this
directory into `CHIP_CHAT.ACCOUNTS`; nothing else in this repository reads it
except `data-gen/tests/test_roster.py`, which holds it to what the shipped
`population.toml` generates.

| File | Table | Rows |
| --- | --- | --- |
| `personas.jsonl` | `ACCOUNTS.personas` | 7 |
| `persona_fixtures.jsonl` | `ACCOUNTS.persona_fixtures` | 28 |
| `demo_visitors.jsonl` | `ACCOUNTS.demo_visitors`, and `ACCOUNTS.demo_visitor_baseline` from the same file in the same run | 500 |
| `manifest.json` | — | the seed, both input digests, and a SHA-256 per table for all six |

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
the population is generated from the committed catalogue fixture, the committed
policy harvest and the shipped `population.toml` at seed 20260826, and the
digests in `manifest.json` are what make "reproducible" a claim you can check
rather than one you have to believe. `data-gen/tests/test_roster.py` checks it
on every `make ci`.

## Re-exporting it

Only after a deliberate retune. Changing `population.toml` changes the
population, which changes every table, which makes the *whole* account stale —
including the gold marts, which are computed from `orders` in Databricks. The
export is the easy half; re-landing the account and re-running the nightly
publish is the rest of it, and the sequence is in
`docs/snowflake-schema.md` §9.

[#39]: https://github.com/gganssle/chip_chat/issues/39
