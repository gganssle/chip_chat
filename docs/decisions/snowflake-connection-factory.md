# Decision: put the Snowflake driver in the app's dependencies, and read the key from Key Vault

**Bead:** `cc-lpy4` · **Decided:** 27 August 2026 · **Measured:** 27 August 2026
**Reverses:** the note in the root `pyproject.toml` keeping `snowflake-connector-python` out of the lockfile
**Retires:** `docs/decisions/shipped-persona-roster.md` on any deployment that has a credential
**Closes the mechanism half of:** `docs/public-demo.md` §9

---

## The gap this closes

`chip_chat.api.pool` is the most carefully argued module in this repository. It
binds a visitor's identity to a Snowflake session on checkout, reads it back
before it hands the connection out, destroys any connection that arrives
carrying somebody, and refuses to serve a session the store cannot resolve.
`api/tests/test_pool_concurrency.py` drives dozens of visitors through a handful
of slots thousands of times over and asserts nobody sees anybody else's rows.

**It had no connections in it.** `pool.py`'s `SessionConnection` is a protocol,
and its own docstring said why nothing implemented it:

> A protocol rather than the Snowflake driver, for the reason
> `chip_chat.snowflake.snow` gives for shelling out to the CLI: the driver is
> not in this lockfile, the connection is already described once in
> `~/.snowflake/config.toml`, and a second place that knows how to authenticate
> is a second thing to rotate a key in.

Every consequence of that followed mechanically. `build_visitors(None)` returned
a desk over the roster shipped in the image. `build_service` was called with
`lanes=NO_LANES`. `get_points_balance` and `get_usual_order` answered from
`chip_chat.agent.hardcoded.ACCOUNT`, `ask_account_question` and
`get_recommendations` were not offered at all, `GET /healthz/lanes` reported
five lanes `not_wired`, and the visible symptom was an opening message and an
account tool disagreeing inside one conversation. Nothing was broken. Nothing
was connected.

## The decision

**Add `snowflake-connector-python` to `api/`, write the adapter, and read the
private key from Key Vault — through the platform where possible and through the
SDK where not.**

`api/src/chip_chat/api/connect.py` is the whole of it: an adapter over the
driver's cursor, a settings object with no key material in it, a lazily-resolved
private key, a factory that answers `None` on a deployment that has no
credential, and a key-pair `TokenSource` for Cortex Analyst.

### Why the lockfile argument was right, and why it stopped being right

The root `pyproject.toml` listed `azure-functions` and
`snowflake-connector-python` together as *runtime-only* dependencies: both are in
`api/functions/requirements.txt`, which is what the Functions host installs, and
adding either to the workspace would put a dependency into every developer's
virtualenv to satisfy a file nothing here imports.

That reasoning has one premise — *nothing here imports it* — and this change
falsifies the premise for one of the two. The deployed image now imports the
driver on the conversational path, so a workspace that does not declare it is a
workspace whose lockfile does not describe what runs. `azure-functions` is
untouched: it is a runtime SDK for a worker, no workspace package imports it, and
`api/tests/test_ops_host.py` still asserts it stays out.

The entry was **split rather than edited**, in both `pyproject.toml` files and in
the test, so that a reader who finds the driver in the lockfile finds the
argument for why beside it rather than inferring that the old reason still
applies to both.

### Where the key comes from, and why the order is what it is

Three sources, resolved on first connection and never at start-up:

| Source | Where it is used | Why |
| --- | --- | --- |
| `SNOWFLAKE_PRIVATE_KEY` | The Container App | A Container Apps secret whose value is a **versionless Key Vault reference**, resolved by the platform with the app's managed identity |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | A developer's laptop | The unencrypted PKCS#8 file `snow --private-key-file` already wants |
| `AZURE_KEY_VAULT_URI` + `SNOWFLAKE_PRIVATE_KEY_SECRET` | Anything else | `DefaultAzureCredential`, which is `az login` locally and the managed identity in Azure |

The first is production and it is first for a measured reason rather than a
tidiness one. `docs/deployment.md` §3.11 is a write-up of a deployment that spent
thirty-five seconds looking healthy and then stopped answering `/healthz` until
Container Apps restarted it, over and over, because assembling the photo intake
constructed two Azure SDK clients on the start-up path. **A Key Vault read to
fetch a private key is exactly that kind of client.** Letting the platform
resolve the reference means the read happens before the process exists and the
app pays nothing for it while a liveness probe waits.

The third path exists anyway, because `.env.example` promises it for every other
secret in the system — *secrets live in Key Vault and are read at runtime over
the credential `az login` writes* — and because a factory with only two sources
would be a factory a laptop cannot use without a key on disk.

**`snowflake_connect` never reads any of them to decide.** It asks whether a
source is *named*, which is a question about environment variables, and returns a
factory or `None`. The key is fetched inside the first `connect()` call and
memoised behind a lock. A deployment with a misconfigured vault therefore fails
on its first Snowflake checkout — a lane that declines, in a trace, with the
vault and the secret named — rather than on a probe that times out.

### Two things the driver does that nothing in this repository had recorded

Both were found by asking the live account, and both would have been a confusing
afternoon for whoever found them next.

**The `private_key` argument wants DER bytes, not a PEM string.** Key Vault holds
PKCS#8 PEM, because that is what an operator can paste and what `openssl` prints.
Handing that string to the driver produces `251008: Failed to load private key
[...] Please provide a valid RSA or ECDSA private key in DER format as bytes
object`. `connect._der` is the one conversion, and it is the reason `cryptography`
is reached for directly. *(`api/functions/function_app.py` passes
`os.environ["SNOWFLAKE_PRIVATE_KEY"]` — a string — straight through. That file is
not touched here; it is noted so somebody checks it against the driver version
the Functions host installs.)*

**The default paramstyle is `pyformat`, and the pool binds with `?`.**
`pool.py` spells its bind as `SET DEMO_ID = ?`. Under `pyformat` that is not a
placeholder — it is a syntax error, on the one statement in the system that makes
a row access policy true, and the failure mode is a connection that cannot be
bound rather than one bound to the wrong person. `CONNECT_SETTINGS` passes
`paramstyle="qmark"` **per connection** rather than setting
`snowflake.connector.paramstyle`, which is a module global that would reach into
any other consumer of the driver in the same process.

`SET` really does take a bound parameter under `qmark`. Verified against the live
account on 27 August 2026:

```
read unset:       [(None,)]
SET DEMO_ID = ?   ('demo-0001',)  ->  Statement executed successfully.
read:             [('demo-0001',)]
orders bound:     [(45,)]
UNSET DEMO_ID     ->  Statement executed successfully.
read after unset: [(None,)]
roster unbound:   28 rows
```

This is worth recording because `api/functions/function_app.py` states the
opposite in a docstring — *"Snowflake's `SET` does not take a bound parameter, so
the identifier is interpolated into the statement that binds it"* — and
interpolates behind an allowlist. The allowlist makes that safe; the claim it
rests on is not true of this driver, and a reader comparing the two tiers should
know which one measured it.

### The role, and where it comes from

`APP_USER` is spelled once in `connect.py`. `READ_ROLE` and `SERVING_WAREHOUSE`
are looked up off `chip_chat.snowflake.account.USERS`, the declaration
`snowflake/sql/04_users.sql` is generated from. A rename there is a
`StopIteration` at import time; three transcribed strings that quietly drifted
would be a chat app running on the write role with nothing saying so.

`CHIP_CHAT_READ` is refused an `INSERT` by the account itself — checked on
27 August 2026 — so this tier is defence in depth rather than the enforcement.

### Cortex Analyst, and the CLI the container does not have

`chip_chat.snowflake.cortex.CliJwt` shells out to `snow connection generate-jwt`
and argues for it: the account, the user and the private key are described once
in `~/.snowflake/config.toml`. **A container has no such file and no `snow` on
its PATH**, and `cortex.py` anticipated exactly this: *"a deployment that cannot
ship the CLI supplies a different `TokenSource`; that is what the protocol is
for."*

`connect.KeyPairJwt` is that `TokenSource`, signing with the same key the
connection authenticates with. It lives in `api/` and not beside `CliJwt`
because `snowflake/` has no driver in its dependencies and holds no credentials,
and `api/` is the tier that has both. It satisfies the protocol structurally,
with no import in either direction.

The REST host is `SNOWFLAKE_HOST` where set and
`<account_locator>.snowflakecomputing.com` otherwise.
`cortex.host_from_env` refuses to guess and gives the reason — *"an account
identifier assembled here and wrong is a lane that fails on every turn with a DNS
error"* — but that argument is about assembling one from a configuration file.
This derives it from the locator the same process is already opening connections
with. Both forms answer `200` on this account, checked on 27 August 2026:

```
https://hq72718.us-east-2.aws.snowflakecomputing.com   200
https://llmpcwe-gs74649.snowflakecomputing.com          200
```

Terraform sets `SNOWFLAKE_HOST` anyway, so the derivation is a floor and not the
normal path. The alternative — refusing — would mean one unset variable silently
withdrew `ask_account_question` and put `get_points_balance` back on the fixture,
which is the exact regression this whole change exists to prevent.

## What the shipped roster keeps

`docs/decisions/shipped-persona-roster.md` says the file it ships is *"dead
weight to be deleted"* the moment a factory exists. It is not deleted here, and
the rule that record states is unchanged and still enforced in one place:

```python
if connect is None:
    return VisitorDesk(shipped_roster(), store=store), None
```

There is still no merge and no fallback-on-error. What changed is only who
decides: `build_service` now asks `snowflake_connect()` rather than being handed
`None` by every caller. A deployment with no `SNOWFLAKE_ACCOUNT`, or with one and
no key, gets exactly the behaviour that record describes — twenty-eight populated
personas out of a JSON file, a `WARNING` naming it, and `not_wired` at
`/healthz/lanes`. That is the state a fresh subscription with an empty Key Vault
comes up in, and it should stay working until somebody deletes the file
deliberately.

## What it costs

**Image size and cold start.** `snowflake-connector-python` and its transitive
tree (`pyarrow` is the large one) are now in the runtime virtualenv of an app
whose cold start a visitor waits behind, and `min_replicas = 0` means somebody
pays that start every time the link is handed to a stranger who has not clicked
one recently. **This was not measured before and after.** The image was built in
ACR and deployed; the deploy-check loop waited for the revision the way it always
does, and no timing was taken against the previous revision. It is the first
number to take if the entry latency regresses.

**Four round trips per checkout, on a real network.** `pool.py` argues for them
and the argument does not change, but they were free while there was no
connection and they are not free now: read back, `SET`, read back, `UNSET`, each
crossing from Azure East US 2 to AWS us-east-2. `docs/decisions/snowflake-region.md`
is the write-up of that hop.

**A warehouse that wakes.** `CHIP_CHAT_SERVING_WH` is X-Small and suspends after
sixty seconds; before this change a demo session woke it never. Every visitor who
asks about their account now resumes it.

**One tool that is offered and declines.** Wiring `Lanes.personalization` offers
`get_recommendations`, and `CHIP_CHAT.MARTS.recommendations` does not exist on
the live account — `reads.RECOMMENDATIONS_MART` says so and names bead `cc-afo5`
as the decision that would create it. The lane returns a decline carrying the
SQL compilation error, which is legible in a trace and is not a lie, but it is
the shape `agent/lanes.py` argues against and it is a cost of wiring the lane
rather than a benefit of it. The alternative was leaving `get_usual_order` on the
hardcoded fixture, which is the defect being fixed.

## What this does not fix, and it is worth saying plainly

**`ACCOUNTS.persona_fixtures` is out of step with `ACCOUNTS.orders` and
`ACCOUNTS.loyalty_ledger` on the live account.** The opening message quotes the
fixture's narrative; `get_points_balance` sums the ledger. `chip_chat.data_gen`
generates both from one population and asserts they agree
(`data-gen/tests/test_fixtures.py`), so on a coherent load they would be the same
number. Measured across all twenty-eight fixtures on 27 August 2026, after this
change:

```
agree: points 4/28   usual item 20/28   order count 4/28
```

That is not something this ticket introduced and not something it can fix — it is
a property of what was loaded into the account, and the fix is a reload rather
than a wiring change. It was **invisible** before this change, because the tool
was reading a hardcoded fixture and could not disagree with the roster about
anything except everything. `docs/public-demo.md` §9 carries the transcript and
the follow-up.
