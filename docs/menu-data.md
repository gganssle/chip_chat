# The real menu, and what it cost to land it

**Issue:** [#106](https://github.com/gganssle/chip_chat/issues/106) — *"Not enough
data in the backend"* · **Landed:** 28 August 2026

---

## 1. What was actually wrong

The report was that Cilantro's menu had three items on it. That was true, and
the cause was not the data mart.

`chip_chat.agent.hardcoded.MENU` is the week-one slice: a three-item menu that
`search_menu_knowledge` answers from when no knowledge lane is wired, and it
says so in its own result rather than pretending. `chip_chat.api.app.build_lanes`
never wired one. It assembled the account and personalization lanes over the
Snowflake pool and returned; *knowledge* and *photo* were left as ticket
references in its own docstring, so `/healthz/lanes` reported both `not_wired`
and the model was never offered `match_meal_from_photo` at all. Loading a
hundred times more data into Snowflake would not have changed a word of what a
visitor was told about the menu.

Underneath that, though, the backend really was thin, and in three separate
places:

| | Before | After |
| --- | --- | --- |
| `CHIP_CHAT.CATALOGUE.menu_items` | 10 | **192** |
| `CHIP_CHAT.CATALOGUE.modifiers` | 10 | **1,385** |
| `CHIP_CHAT.CATALOGUE.item_prices` | 20 | **192** |
| `CHIP_CHAT.ACCOUNTS.order_items` | 48,771 | **73,692** |
| `CHIP_CHAT.MARTS.item_affinity` | 12 | **8,198** |
| Retrieval index documents | 31 | **358** |
| Vision vocabulary terms | 8 | **48** |
| Distinct orderable things ever ordered | 4 | **153** |

The catalogue was the committed **test fixture** — `catalog/tests/fixtures/catalog`,
two entrees, one side, one drink — put there by `make snowflake-load-sample`,
which is what that target is for. The real harvest had simply never been run
into the account, and `make snowflake-load`, the target that would have done it,
could not have: it named `$(LANDING)/catalog`, and the catalogue build writes
its nine tables one directory further down under `catalog/chipotle`.
`chip_chat.snowflake.load.sources` only refuses when *none* of the directories
it is given holds a file for any table, so the accounts half satisfied it and
the four catalogue tables were skipped in silence. Not a failed load. An
incomplete one that reported success.

## 2. The sequence, written out

Everything below is re-runnable and nothing in it is a one-off. Each step is
also a `make` target, and the numbers beside them are what the run of
28 August 2026 produced.

```bash
make reharvest                                    # 76 documents, run 20260828T175755Z
uv run python -m chip_chat.catalog \
    --landing landing --offline --stores 30       # 192 items, 1,385 modifiers, 48 terms
uv run python -m chip_chat.data_gen --landing landing \
    --report landing/texture-report.md            # 18,898 orders, 73,692 lines, 19 checks held

az storage blob upload-batch --account-name stchipchat4cy39i \
    --auth-mode login -d raw -s landing --overwrite

make snowflake-load                               # CATALOGUE: 1,799 rows
make snowflake-load-roster                        # ACCOUNTS: personas, fixtures, visitors

databricks pipelines start-update <bronze>  --full-refresh
databricks pipelines start-update <silver>  --full-refresh
databricks pipelines start-update <gold>    --full-refresh
databricks pipelines start-update <chunk>   --full-refresh
databricks jobs run-now <publish>                 # eleven tables into Snowflake

make vocabulary && make image image-push deploy
```

`make snowflake-load` deliberately loads **only** the catalogue half. The
account tables reach Snowflake by the other route: `chip_chat.snowflake.load`
refuses `order_items` by name, because the generator leaves `demo_id` off an
order line and #43's row access policy filters one table against a session
variable and cannot follow a join to the order. Silver carries the visitor down
onto the line, and #39's publish is what lands it. Two loaders, two halves —
`data-gen/roster/README.md` is the write-up.

## 3. Four things that were broken and had to be fixed to get here

None of these were caused by the menu data. All four were found by being the
first person to run the whole pipeline end to end since it was written.

**The landing zone in blob storage held three merged harvests.** 143 index
entries against the 80 a single harvest produces, and 63 of the extras were from
a *fixture* run: `https://example.test/deliberately-mistyped`, a
`nutrition-sheet.pdf` that Chipotle does not publish, and store profiles for a
synthetic 2000–2056 id sequence. The bronze autoloader read all of them, so the
corpus was a third fabricated. They are deleted; the backup is in the run's
scratch directory and the sixty-three source URLs are listed in the bead.

**`silver_harvested.document_blocks` has never materialised.** Every full
refresh since 26 August fails one expectation, `is_not_furniture_the_stripper_missed`,
and the table holds zero rows. Seven blocks trip it, all of them Chipotle's
"Try our Featured Meals" promotional carousel — `ORDER NOW`, `MOST POPULAR`,
`IT'S FUN TO GET REWARDED` — which is identical on all 30 `locations.chipotle.com`
store pages out of 35 corpus documents, a share of 0.86 against a threshold of
0.5. The stripper cannot reach it: the markup is
`<div class="py-8 sm:py-16 bg-gray-200 px-4">`, Tailwind utilities, no id, no
role, no semantic class. Left open deliberately — see §6.

**The deployed Databricks library had drifted from the repository.**
`/Shared/chip-chat/lib/publish.py` was 850 lines against the repository's 887
and had no `row_count`, so the publish counted with `SELECT COUNT(*)` and read
0 rows through #43's policy, aborting *after* the swap. The fix had been
committed and never applied. Every other library and notebook matched.

**The embedder had no answer for a rate limit.** `HttpEmbedder.embed` raised on
any non-200, and 358 chunks at a batch of sixteen is more than an AIServices S0
deployment will take in a minute: `429 RateLimitReached`, `Retry-After: 54`,
build dead, new index created and abandoned, alias still on the old one. It now
retries a 429 honouring `Retry-After` — bounded at six attempts and 120 seconds
each — and nothing else, because a 400 is a request that will never get better
by asking again. Retrying is safe here specifically: embedding is a pure
function of the text, so a repeated batch produces the same vectors and costs
one more call.

## 4. What the roster now carries, and why

`data-gen/roster/` is the copy of the population the live account is supposed to
be holding, and `data-gen/tests/test_roster.py` regenerates it on every
`make ci` and compares byte for byte. It used to regenerate from the *test*
fixtures, which was correct while the account held a population generated from
the same small catalogue.

It does not any more, so `data-gen/roster/inputs/` now carries the inputs: the
built catalogue and the three policy tables `load_rewards_terms` reads, 1.4
megabytes against the seventeen of the population they generate, laid out
exactly as a landing zone lays them out. `make ci` still needs no network, no
credential and no harvest, the regeneration costs 3.4 seconds, and "reproducible
from this repository byte for byte" stays a claim you can check.

## 5. What it cost, measured

**The `propose_order` tool definition grew by a factor of forty-three.**
`chip_chat.api.orderdesk._describe` composes one line per orderable item — id,
name, reference price, and the required content groups with the modifier ids
that fill them — and it goes in a *tool definition*, which is part of every
request:

| | Characters | Approx. tokens | Enum values |
| --- | --- | --- | --- |
| Ten-item fixture | 480 | 120 | 10 |
| Real catalogue | **20,605** | **≈5,150** | **192** |

That is about 5,000 extra input tokens on every turn. `CHIP_CHAT_SESSION_TOKEN_CAP`
is 120,000 and `CHIP_CHAT_TURN_TOKEN_RESERVATION` is 8,000; neither was re-tuned
here, and a session will now reach its token cap in noticeably fewer turns than
its 40-turn cap allows. **This has not been measured against a real
conversation** — the figure above is the tool definition alone, counted as
characters over four, not a token count from a provider response, and not a
per-turn total.

`chip_chat.agent.desk.Desk.orderable_menu` may answer `None`, which leaves the
schema open and falls back to the enforcement that was always doing the real
work: the draft store's pricing, which refuses an unpriced item, and the
procedure's catalogue check behind that. It is deliberately not pulled. The
reason is `agent/desk.py`'s: the first wiring of a real catalogue gave the model
ids with no names and no required groups, and it could not compose a single
valid draft — it guessed, was refused with `REQUIRED_SLOT_EMPTY`, guessed again,
and hit the loop's step ceiling. An open schema is a policy decision about what
the model may name, and it belongs to whoever decides the trade has gone the
other way, with this number in front of them.

Trimming does not save much. Filtering to the 117 rows carrying an `Entree`,
`Side` or `Drink` category saves 13 per cent and drops Guacamole, which has no
category and is ordered on its own every day. The weight is the 65 entrees'
required-group modifier id lists, which are the half a model cannot compose a
draft without.

## 6. What was not measured, and what was left open

- **The per-turn token cost of the larger tool definition, end to end.** §5
  measures the definition; nobody has run a conversation and read
  `llm.token_count.prompt` off `chat.turn` before and after. The right way to
  get that number is `make experiment-compare`, and it was not run.
- **Retrieval quality against the 358-chunk index.** `make retrieval` and
  `make grounding` score against the offline slice, not against this index, and
  neither baseline was refreshed. One query was run by hand — *"what vegan
  options do you have?"* returns the vegan and vegetarian FAQ entries and
  Veggie Tacos, each with its `source_url` and `harvested_at` — which is
  evidence that the lane answers, not evidence about its quality.
- **`document_blocks`, and therefore `DOCUMENT_BLOCK` chunks.** The index holds
  192 menu items, 134 FAQ entries, 27 policy sections and 5 allergen caveats,
  and no document blocks. What is missing is prose that appears on pages the
  corpus already covers by other means; what would be needed to include it is
  a rule for a syndicated promo module with no semantic marker, or a decision
  that a corpus which is 86 per cent one site section is measuring its own
  composition rather than the stripper's quality. That is a design question and
  it has its own bead.
- **`NUTRITION_ROW` chunks are zero** because Chipotle published no PDF
  nutrition sheets on 28 August 2026. That is a result, not a gap.
- **`CATALOGUE.rewards` and `CATALOGUE.rewards_terms` are still hand-loaded.**
  Both are real published data and neither is a file the harvest writes:
  `rewards` needs a `reward_id` this repository derives, and `rewards_terms` is
  computed from `policy_sections` and `faq_entries` rather than parsed. The
  eight and four live rows were checked against the fresh harvest and match, so
  nothing is stale today. There is no committed path that reproduces them, and
  that has its own bead.
- **The recommender was not re-run.** `gold_synthetic.recommendations` still
  holds the previous generation. It is not among the eleven tables #39
  publishes, so it does not reach the demo, and `get_recommendations` answers
  off `MARTS.item_affinity`, which was rebuilt.

## 7. The four invariants, unchanged

Worth stating explicitly, because this touched every layer.

**Identity is never a tool argument.** Nothing here added a parameter anywhere
model-reachable. The four tier tests are unmodified and pass.

**No write without explicit confirmation.** The action lane was not touched.

**The spend cap is inline.** `SpendGate` still privately holds the model, and
`build_lanes` hands lanes to it rather than around it — the knowledge and photo
lanes were added to the `Lanes` value the gate is constructed with, not to a
path beside it. What §5 records is that a turn now costs more, which is a
question for the ceiling and not for the mechanism.

**Real published menu, entirely synthetic accounts.** Every one of the 192 items
came out of `services.chipotle.com`; every order that references them was
generated by `chip_chat.data_gen` from a seed. The one place this run found the
boundary blurred was in the *other* direction and it was pre-existing: fixture
documents from a test harvest sitting in the production landing zone, described
in §3, now removed.
