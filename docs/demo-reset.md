# The nightly demo-data reset

Strangers place orders, redeem points and rename themselves. Without a reset the
personas drift away from the states the gold marts were computed against, and
personalization degrades quietly until nothing demos well. This is the job that
puts them back, and the argument for the four choices it makes.

Issue [#47](https://github.com/gganssle/chip_chat/issues/47). It depends on
[#42](https://github.com/gganssle/chip_chat/issues/42) for the schema and on
[#46](https://github.com/gganssle/chip_chat/issues/46) for the write path, and
its *shape* was fixed by [#9](https://github.com/gganssle/chip_chat/issues/9),
which is the decision to read first. RFC-001 §13 Q4 is answered by consequence
rather than separately. PRD §11 Q1.

The code is in three places and each has a different job.
`snowflake/sql/14_demo_reset.sql` is the procedure and the task, and is what
Snowflake runs. `snowflake/src/chip_chat/snowflake/reset.py` is the job as data
plus the manual trigger. `snowflake/tests/test_demo_reset.py` holds the first to
the second in `make ci`.

---

## 1. Why it ages out instead of truncating

The obvious nightly reset is `TRUNCATE`, and #9 took it off the table. Visitor
state **persists between visits, via a cookie**: Sam comes back tomorrow to the
order they placed today, which the PRD calls the materially better story. A
nightly truncate of the visitor-scoped tables would empty the account of a
returning visitor mid-story — an assistant with no history to be personal about,
which is the cold-start failure the PRD names as the single largest threat to
the demo, arriving on a schedule.

So the reset ages sessions out. #9's own consequence (2) says so, and RFC-001
§13 Q4 predicted that the resolution to "persisting sessions and nightly
truncation are in direct tension" would be temporal rather than categorical.

The thing that makes ageing cheap to implement is that **a visitor can only
change two kinds of thing**, and both are identifiable without a diff:

| What a visitor changes | Where it lives | How the reset finds it |
| --- | --- | --- |
| Rows they added | `orders`, `order_items`, `loyalty_ledger` | identifier at or above the live band, `ord-9000001` / `loy-9000001` |
| Rows they added | `action_receipts` | every row — only a live write makes one |
| Columns they edited | `demo_visitors` | restored from the baseline, column by column |

Generated history — eighteen months of it, numbered from `ord-0000001` — is
never touched. That is what makes "restores generated state exactly" a restore
rather than a reload, and it is the reason this job is safe to run while the app
is live: the rows a conversation cites are not rows it can reach.

The live band is `snowflake/sql/07_accounts.sql`'s sequence header. It was
argued there as a convenience for a reviewer reading the table — an identifier
above nine million is one the demo wrote — and it turns out to be the mechanism
this whole job rests on. `test_demo_reset.py` fails if a `DELETE` here loses its
band predicate, which is the single most expensive mistake this file could
contain and the one that would look right in review: an emptied persona leaves a
perfectly plausible row count behind it.

---

## 2. `demo_visitor_baseline`, and why a table

Five columns of `demo_visitors` can move: the three `EDITABLE_COLUMNS`
(`display_name`, `home_store_override`, `stated_preferences`) plus `thread_id`
and `last_seen`, which are the app's own session bookkeeping on the same row.
Restoring them needs somewhere that kept them, and there was nowhere:

* `persona_fixtures` deliberately carries **no name** — a narrative with a name
  baked into it goes stale the moment somebody edits one — so `display_name`
  exists on exactly one table in the database.
* `home_store_override` and `stated_preferences` are generated **non-null for
  some customers**, so "restore" is emphatically not "set to `NULL`". A reset
  that nulled them would quietly edit customers it was meant to leave alone.
* The generator picked `stated_preferences` from a fixed list at random, so
  nothing downstream can re-derive which.

So `CHIP_CHAT.ACCOUNTS.demo_visitor_baseline` is the eighth table in
`07_accounts.sql`, and the interesting thing about it is where it is filled
from. It is loaded from the generator's own `demo_visitors.jsonl` — **the same
file, in the same run, as `demo_visitors` itself**. `schema.Table.source` is the
one field that makes two tables read one export, and `load.sources()` is what
honours it.

That is what makes #47's first acceptance criterion checkable. "Restores
generated state exactly, verified against the generator's output" is only a
claim a reviewer can test if the baseline *is* the generator's output; a
baseline filled from a second generation would restore visitors to a state that
never existed, and nothing downstream could tell.

Two consequences worth knowing:

* **A visitor the baseline does not carry is never aged out.** The cursor inner
  joins the baseline, and the count it excluded is on the receipt as
  `held_no_baseline`. Without that join, a reset run against an unloaded
  baseline would delete every live row and restore nothing — which looks exactly
  like a working reset, and leaves a persona whose name is whatever the last
  stranger typed.
* **It is visitor-scoped and carries a row access policy**, like everything else
  in `ACCOUNTS`. `09_audit.sql` defaults to deny, so it had to be either
  protected or exempted with an argument, and a baseline is as much a fact about
  one visitor as the row it restores.

---

## 3. When a visitor counts as inactive

#9 says `last_seen` becomes load-bearing rather than decorative. Today it is
not, and the reset is written knowing that.

**Only `update_preferences` writes `last_seen`.** `place_order`,
`redeem_points` and `cancel_order` do not, and a read-only conversational turn
writes nothing anywhere. A job that aged on `last_seen` alone would therefore
age out a visitor who had been ordering all afternoon, because their `last_seen`
would still be the timestamp of their last *generated* order eighteen months
ago. That is not a subtle bug; it is the job doing the opposite of its purpose,
every night, to its most engaged visitor.

So activity is the **latest of four clocks**:

```
GREATEST( demo_visitors.last_seen,
          MAX(action_receipts.created_at),
          MAX(orders.placed_at)         -- live band only
          MAX(loyalty_ledger.created_at) )  -- live band only
```

`action_receipts` is the one that carries it. #46 makes each of the four write
procedures claim a retry key in that table before it writes anything else, so
**any visitor who has done something has a trustworthy clock**, whatever the
procedure forgot to update.

### The visitor with no clock at all

Somebody who has only *talked* has a `thread_id` and nothing dated: `last_seen`
is still the baseline's, there is no receipt and there is no live row. This job
cannot tell whether they left last week or are mid-sentence.

**It does not guess.** Such a visitor is held, nothing is deleted for them, and
the count comes back on the receipt as `held_no_clock`. A non-zero number there
is a message: the app tier is not touching `last_seen` when it binds a session,
and whatever writes `thread_id` is the thing that should write `last_seen`
beside it — they are the same row's session bookkeeping. That is cc-9xod.

Holding is affordable because **the visitor population is bounded**. Only a
`persona_fixtures` demo_id is ever handed to anybody, and switching personas
chooses another fixture rather than inventing a customer. Demo data grows in
rows per visitor, which the ops API's rate limits bound; it does not grow in
visitors.

### The TTL is derived, not invented

Two days, and the number comes off `api/src/chip_chat/api/app.py`:

```python
response.set_cookie(SESSION_COOKIE, ..., max_age=86_400)
```

The session cookie lives one day, so a visitor can return to their `demo_id` for
a day and then cannot reach it again by any means. The TTL is that day plus one:
everybody who could possibly come back still has their state, and nobody is kept
for a visit that cannot happen. The second day is doing real work rather than
being round-number caution — a visit that starts in the last minute of a
cookie's life is a conversation that can run past the cookie's own expiry, and a
reset firing at exactly twenty-four hours would land in the middle of it.

`reset.ttl_is_sound()` is that relation as an assertion, and the procedure
carries the floor itself so that a hand-typed `--ttl-days 1` is refused — the
reason somebody reaches for the manual trigger is that a demo just went badly,
which is exactly when a small number gets typed.

---

## 4. Running it while the app is live

#47's third acceptance criterion. Three things get it, and only the first is
about the reset being careful:

**An active visitor is out of scope by construction.** Activity is what the
cutoff is measured on, so there is no window in which a live visitor is a
candidate. This is the whole guarantee; the two below are about the edge.

**One transaction per visitor.** A visitor who returns at the exact moment their
own reset is running sees either their whole live state or none of it, never
four deletes' worth of it. That is why the procedure is a loop rather than five
set-based statements over the aged cohort — and the cost is that a run is not
atomic *across* visitors, which is the right trade: every visitor is
independently correct, the job is idempotent, and tomorrow's run finishes the
list. One transaction over the whole cohort would instead hold locks on `orders`
and `loyalty_ledger` for the length of the run, and the ops API writes those.

**It runs on the publish warehouse.** A reset is a batch, and the two-warehouse
split exists so that a batch cannot queue in front of a conversation. The
serving warehouse also cancels anything still running after sixty seconds, which
a loop over the roster could plausibly exceed.

### The refusal that matters

#43's row access policies filter `DELETE` and `UPDATE`, not only `SELECT`. An
admin session that has bound no visitor deletes **zero rows** — default deny
survives for the owner, which is the property `10_policies.sql` is proudest of.
So the reset sets `ALL_VISITORS`, exactly as `load.py` does; `10_policies.sql`
names this job as the escape's second caller, and it is not a widening, because
the policy honours the variable only for a session whose primary role is
`CHIP_CHAT_ADMIN` — a role that can detach the policy outright anyway, and which
no service user holds.

The important half is the check *after* the `SET`. Without it, the failure mode
is a task that runs every night, deletes nothing, restores nothing and reports a
clean run while the personas drift for a month. **A reset that silently does
nothing is worse than one that fails**, so this one fails:
`MAINTENANCE_ESCAPE_UNAVAILABLE`. The variable is released on every exit path
including the exception path, so an operator's next query in the same session is
never quietly cross-visitor.

---

## 5. Running it by hand

```bash
make snowflake-demo-reset-plan   # who would be aged out. Changes nothing
make snowflake-demo-reset        # do it
```

Both call `CHIP_CHAT.ACCOUNTS.reset_demo_sessions`, which is what the nightly
task calls, with the arguments the nightly task passes. That is the only
arrangement in which the manual trigger is worth having: a hand-written variant
is a thing nobody has run this month.

`python -m chip_chat.snowflake.reset --ttl-days N` for a different window; the
floor is two days and the procedure refuses below it.

The receipt is JSON and every field is there to be read during an incident:

| field | what a number in it means |
| --- | --- |
| `visitors_aged` | how many were reset |
| `orders_deleted`, `order_items_deleted`, `ledger_entries_deleted` | live-band rows removed. Generated history is not counted here because it is not touched |
| `receipts_deleted` | spent retry keys released |
| `threads_retired` | Foundry thread ids **detached and not deleted** — see §7 |
| `held_no_clock` | non-zero means the app owes `last_seen` a write. §3 |
| `held_no_baseline` | non-zero means a load ran one half of itself. §2 |
| `rejection` | present only when `ok` is false. Four of them, all in `reset.REJECTIONS` |

The nightly run's receipt is in Snowflake's `TASK_HISTORY`:

```sql
SELECT scheduled_time, state, error_message
  FROM TABLE(CHIP_CHAT.INFORMATION_SCHEMA.TASK_HISTORY(
       TASK_NAME => 'RESET_DEMO_SESSIONS_NIGHTLY'))
 ORDER BY scheduled_time DESC;
```

The task runs at **09:00 UTC**, two hours after #39's publish starts at 07:00.
Not an ordering requirement — this job restores `demo_visitors`, which the
publisher cannot see — but both write `ACCOUNTS.orders`, and a `DELETE` landing
inside an `INSERT OVERWRITE` is a lock wait at best.

A newly created Snowflake task is **suspended**. `14_demo_reset.sql` resumes it,
and `EXECUTE TASK` is granted to `CHIP_CHAT_ADMIN` in the same file, because an
apply that installs a schedule which never fires is the second failure in this
feature that looks exactly like success. `test_demo_reset.py` checks for both
lines.

---

## 6. The one open conflict, and the decision

`docs/nightly-publish.md` §7 ends by handing a question to this issue:

> **#47 and this job both write the account tables.** … The two are not in
> conflict today because nothing writes `ACCOUNTS.orders` at runtime yet —
> #46's action lane is not landed. When it is, "tonight's publish erases the
> order a visitor placed this afternoon" becomes a real sentence, and deciding
> whether that is right is #47's job, not this one's.

#46 has landed. The sentence is real, and the decision is:

> **A visitor's live rows survive until that visitor ages out.**

The publish is therefore what has to change, not this job. `INSERT OVERWRITE` on
`orders`, `order_items` and `loyalty_ledger` replaces the whole table, which
erases live-band rows for every visitor rather than for the aged-out ones —
including the visitor who is coming back tomorrow, whose returning is the entire
thing #9 bought. A demo where yesterday's order vanished overnight has not
bought it.

The other answer — "it is a demo sandbox and the answer is probably yes" — was
available and is rejected for one reason: it makes the persistence decision
true only within a calendar day, which is not what #9 decided and is not what
the story it was decided for describes.

This is #39's code and a real design change: the publish's single-statement swap
is what buys its atomicity, and preserving a band means it stops being a single
statement. It is **tracked separately, as cc-fxf4**. Until it lands, the reset
is still correct — it deletes what is there — but `orders_deleted` will read zero most
mornings for a reason that is not "nothing happened", which is the kind of
number that gets misread once and then trusted.

`reset.ENFORCED_ELSEWHERE` names this alongside the other three gaps, so it is a
thing with an owner rather than an absence.

---

## 7. What this job does not do

**It does not delete a Foundry thread.** Nothing in Snowflake can call Azure.
The reset clears `demo_visitors.thread_id` — which is the part that matters for
correctness, because the next visitor handed that fixture must not resume a
stranger's conversation — and returns the detached ids under `threads_retired`
for whoever holds the project credential — cc-mdmf. Whether the threads even
need deleting is open: `agent/src/chip_chat/agent/threads.py` is the instrument for
Microsoft-managed thread retention and #8 is where the answer will be.

**It does not collect order drafts.** #47 asks about drafts that were never
confirmed, and they never reach Snowflake at all: `api.drafts.DraftStore` holds
them in the ops API's own memory with a 900-second TTL and sweeps them itself.
The ones that would survive long enough for a nightly job to matter do not
survive a restart.

**It does not touch the catalogue or the gold marts.** #39 owns both, and the
marts are recomputed from generated history — which this job leaves alone, so a
reset cannot make a mart stale. `test_demo_reset.py` asserts the absence, over
every table `schema.py` declares in `CATALOGUE` and `MARTS`, rather than trusting
this paragraph.

**It does not maintain `last_seen`.** That is the app tier's, and §3 is the
argument. The reset's `held_no_clock` count is how you find out it has not
happened.

---

## 8. The first live run, 2026-08-27

Everything above used to end at "and it has not been run against the live
account, because no visitor has ever written to it". It has now, deliberately:
a real fixture customer was dirtied through the shipping procedures, aged out,
and reset, and the four acceptance criteria were asked of the rows rather than
of the SQL.

The customer was `demo-0006` — Camille Gallego, 42 generated orders, 99 lines,
61 ledger entries, a 2,098-point balance and a `last_seen` of 2026-07-18. Those
five numbers describe the generation that was loaded at the time and not
necessarily the one in the account now; a later load the same afternoon
replaced the population, which is exactly the thing §2 says a baseline has to be
loaded in the same run as the table it is the baseline for. What matters here is
that they were the same before and after. What
was done to her, in order: `update_preferences` renamed her, `place_order`
placed `ord-9001001` at restaurant 679 and accrued 23 points, a thread id was
pinned on her, and every clock she owns — `last_seen`, the receipts'
`created_at`, the live order's `placed_at` — was wound back five days. That last
step is the one that is easy to get wrong and is the reason the first attempt
aged nobody: `last_active` is the **greatest** of the visitor's clock and every
live row's timestamp, so a visitor whose `last_seen` says last week and whose
receipt says one minute ago is a visitor who is still here. The reset reported
her `dirty` and skipped her, correctly, and said so in the receipt.

Then `make snowflake-demo-reset`:

```json
{"action": "RESET_DEMO_SESSIONS", "cutoff": "2026-08-25 18:58:06.018",
 "dirty_visitors": 1, "visitors_aged": 1, "orders_deleted": 1,
 "order_items_deleted": 1, "ledger_entries_deleted": 1, "receipts_deleted": 3,
 "threads_retired": ["thread_issue47_live"], "held_no_clock": 0,
 "held_no_baseline": 0, "ttl_days": 2, "ok": true}
```

**Restored exactly.** Afterwards `demo_visitors` for `demo-0006` matched
`demo_visitor_baseline` on every restored column — the name back to "Camille
Gallego", the stated preferences unchanged, `last_seen` back to
2026-07-18T20:26:00 rather than to now — with `thread_id` null and
`action_receipts` empty. The counts went back to 42 orders, 99 lines, 61 ledger
entries and a 2,098-point balance: the same four numbers as before the exercise,
which is what "the band held" means. One live order, one live line, one live
accrual and three spent retry keys went; eighteen months of generated history did
not.

**Safe during live traffic.** A second session ran the read lane bound to
`demo-0048` continuously across the reset — 20 queries in the window, every one
returning the same count of 80 orders, no errors and no lock waits. The reset's
per-visitor transaction is short and it never touched a visitor it was not
ageing, which is the property the concurrency rests on rather than luck about
timing.

**`held_no_baseline` is zero because it was fixed that morning.** It was not
zero when the day started. `demo_visitor_baseline` arrived with #47, *after* the
population had been loaded, and `load.py` fills the baseline from
`demo_visitors.jsonl` in the same run as `demo_visitors` itself — so there had
never been a run in which to fill it. Every table existed, every policy was
attached and every offline test passed, and the nightly task would have aged
nobody out for as long as nobody looked, because a visitor with no baseline row
drops out of the cursor's join rather than raising. It was recoverable only
because no visitor had ever written: the live `demo_visitors` was still the
loaded generation exactly, so the baseline could be filled from it without
inventing a second generation. `make snowflake-verify` now fails by name on an
unfilled baseline — *"every visitor has the baseline the reset restores them
to"* — so the next occurrence is loud.

### What is still not measured

1. **The escape inside a task.** The runs above called the procedure directly.
   The nightly task calls the same procedure with the same arguments, but it
   does so from a task's own session: the procedure sets `ALL_VISITORS` itself,
   via `EXECUTE IMMEDIATE`, and runs `EXECUTE AS CALLER` so that it reads that
   session rather than the owner's. If a task session refuses the `SET`, the run
   fails loudly with `RESET_FAILED` rather than quietly with a clean report —
   which is why the refusal in §4 is written the way it is, and the 09:00 UTC
   run is where that gets confirmed.
2. **`held_no_clock` against real visitors.** It was zero here because the
   exercise wrote through the procedures, which leave receipts. The number that
   matters is the one after a real conversation that never wrote anything, and
   it is the app tier's bill.
3. **Contention with the ops API at load.** One reader is not two writers.
   `orders` is a table the reset and the action lane now share.
