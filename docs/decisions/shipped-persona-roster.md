# Decision: ship the persona fixtures in the image, and read them only when Snowflake is absent

**Issue:** [#66](https://github.com/gganssle/chip_chat/issues/66), [#67](https://github.com/gganssle/chip_chat/issues/67) · **Decided:** 27 August 2026
**Builds on:** `docs/decisions/persona-fixtures.md` (#26)
**Retired by:** `cc-lpy4` — the Snowflake connection factory

---

## The gap this closes

`chip_chat.api.visitors` was written correctly and, on every deployment that has
ever existed, assigned nobody.

The chain is short and every link is defensible on its own. `SnowflakeRoster`
reads `ACCOUNTS.persona_fixtures` through `VisitorPool.unbound()`. `VisitorPool`
needs a `Callable[[], SessionConnection]`. `build_service` takes that as an
argument and **nothing supplies one**, because there is no Snowflake driver in
this lockfile — `chip_chat.snowflake.snow` shells out to the `snow` CLI, which
is a developer tool and not something a container calls per request. So
`build_visitors(None)` returned `VisitorDesk(StaticRoster())`, an empty roster is
a roster that assigns nobody, and `VisitorDesk.admit` returned `None` for every
visitor on the live URL.

`None` is a *decided* state and the code says so at length. What it means for a
visitor is the exact failure PRD §06 names as the largest threat to the demo:

> a visitor types their name, arrives at an empty account, asks the only
> question that occurs to them, and is told they have zero points and no order
> history. There is nothing wrong with any component in that exchange and the
> visitor leaves anyway.

That was the deployed behaviour. Not a risk — the behaviour.

## The decision

**Export the twenty-eight rows of `ACCOUNTS.persona_fixtures` into the image at
`api/src/chip_chat/api/fixtures/persona_fixtures.json`, and read them only when
`build_service` is called with no connection factory.**

```
$ snow sql -q "select demo_id, persona_id, label, rank, home_store,
      home_store_name, points_balance, usual_item_id, order_count,
      lifetime_spend, narrative
    from chip_chat.accounts.persona_fixtures order by persona_id, rank"
```

Seven archetypes, four exemplars each, every one of them carrying the measured
narrative #26 curated.

## Why this is not "inventing accounts"

`visitors.py` refuses to hand a visitor an unpopulated fixture and says, in its
own docstring, that *"an invented account is the empty account issue #66 is
written to prevent"*. That rule is intact and this does not bend it.

These are not invented. They are the rows `data-gen` generated, the fixture
selection #26 curated against per-archetype bounds, and a human reviewed as
twenty-eight sentences. The export carries the **same `demo_id`s** Snowflake
holds, so a session bound from this file is bound to the same synthetic customer
a Snowflake-backed deployment would have bound it to. When the connection
factory lands, the identities do not move.

The thing that would be inventing accounts is a roster generated at start-up
from a random seed, or a single hardcoded account served to everybody. Neither
is what this is.

## The rule that keeps it honest

**The shipped roster is consulted only on the `connect is None` path.**

```python
store = VisitorSessionStore(journal_from_env())
if connect is None:
    return VisitorDesk(shipped_roster(), store=store), None
pool = VisitorPool(connect, sessions=store, size=pool_size)
return VisitorDesk(SnowflakeRoster(pool), store=store), pool
```

There is no merge, no fallback-on-error, and no precedence question. A
deployment either has a connection and reads the live table, or has none and
reads the file. The moment `cc-lpy4` supplies a factory, `shipped_roster()`
stops being called and the file becomes dead weight to be deleted — which is the
right shape for a stopgap, and the reason it is a whole separate function with
its own name rather than three lines inside `StaticRoster`.

`shipped_roster()` logs a `WARNING` naming the file every time it is used. An
operator reading the container's first ten lines can see which roster is in play
without asking.

## What it costs

**The row access policies are not exercised.** A file-backed roster is read by
Python rather than by a session that #43's `entry_roster` policy narrowed. That
is not a regression — the pool is `None` on this path, so *nothing* was being
enforced by Snowflake before either — but it means "identity is enforced by row
access policies" remains a claim about the Snowflake tier that this deployment
does not yet demonstrate. `api/tests/test_identity_binding.py` still holds the
app tier to the absence, and `test_two_sessions_see_only_their_own_rows` still
exercises the pool against a fake account.

**The file goes stale if the population is regenerated.** #47's nightly load can
change `persona_fixtures` underneath it. The cost of stale is a narrative that
quotes a points balance the account no longer has — which is exactly the
disagreement between the opening message and the account tool that
`persona.py` is written to avoid. Two mitigations: the export is small enough to
re-run in one command, and it is only read at all on deployments where the
account lane is *also* unwired, so there is no account tool to disagree with.

## The alternative that was rejected

Leave the roster empty and let #67's opening message say "this deployment has no
synthetic accounts loaded".

That sentence exists and is what an unpopulated deployment gets
(`unbound_opening_message`). It is honest and it is a bad demo. The URL is
handed to strangers; a stranger who is told there is no account has been given a
menu search box with a disclaimer on it, and every one of #67's acceptance
criteria fails by design rather than by accident. Shipping the real rows costs
one JSON file and a deletion later.
