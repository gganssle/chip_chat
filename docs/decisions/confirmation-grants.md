# Decision: carry the confirmation across the process boundary as a signature, not a shared store

**Issue:** [#62](https://github.com/gganssle/chip_chat/issues/62), [#63](https://github.com/gganssle/chip_chat/issues/63) · **Decided:** 28 August 2026
**Builds on:** `docs/ops-api.md`, RFC-001 §03 and §06
**Implements:** `chip_chat.api.grants`, `chip_chat.api.opsclient`, `chip_chat.api.orderdesk`

---

## The gap this closes

The ops API has been deployed, credentialled and refusing correctly since
`func-chip-chat-ops-4cy39i` first served four routes. `make ops-verify` put
issue #63's acceptance criteria to it over HTTPS and every probe held. And the
chat app did not call it. `docs/ops-api.md` said so in as many words:

> The write path is deployed, credentialled and refusing correctly, and **the
> chat app does not yet call it**. `chip_chat.agent.orders` still holds the
> week-one order desk, and moving the agent's write tools onto this service is
> its own change, waiting on a draft store both processes can read.

`GET /healthz/lanes` reported `action: not_wired`, three of PRD T1's four write
actions were not in `agent.tools.TOOLS` at all, and the live write-gate red team
could not score its two redemption probes because the lane they attack did not
exist.

The reason it had not been done was not laziness. It was a real obstacle, and the
agent that deployed the ops API was right to stop at it:

> A draft minted in the chat app's process lives in that process's memory (#62),
> so an ops service in a *different* process cannot see it.

Wiring the two together naively would have made every `place_order` return
`DRAFT_NOT_FOUND` — the gate refusing not because nobody confirmed, but because
nobody could see that they had. That is worse than an unwired lane: it is a
broken one that looks like a working gate.

## The constraint

RFC-001 §06 and `CLAUDE.md` invariant 2, which is the second launch gate:

> The ops API rejects any draft that has not been marked confirmed by a request
> carrying the visitor's session — a precondition checked in code, not a prompt
> instruction and not a UI convention.

And RFC-001 §03, which is why the two tiers are two processes at all: **the ops
API is the only holder of the Snowflake write role.** Any solution that gave the
chat app a write credential would have dissolved the problem by dissolving the
boundary, and the boundary is the point.

## What was considered

### A. A shared draft store the ops API can read

The obvious one, and the one `docs/ops-api.md` and `chip_chat.api.ledger` both
name as "the one obvious place for a shared implementation to land". Put the
drafts in a table, or in Redis, or in blob storage; both processes read it.

Rejected, and the reason is a credential rather than a preference. The chat app
authenticates as `CHIP_CHAT_APP` on `CHIP_CHAT_READ`, which the account itself
refuses an `INSERT` — that refusal is checked by `snowflake/tests` and is one of
the things that makes "the ops API is the only path that writes" true rather than
merely stated. A shared draft store the app can *write* therefore needs a fourth
Snowflake user, a fourth role, a schema, a row access policy, and a private key in
a fourth place. Every one of those is a thing to get wrong, and the thing they
buy is a lookup we do not need.

A non-Snowflake store — Redis, a blob container — avoids the role but not the
credential, and adds a service to the estate whose outage is a new failure mode
with no row in RFC-001 §10's table. It also does not remove the two-process
problem; it moves it, because now *three* things have to agree.

### B. Give the chat app the write role

Not seriously considered, and recorded only because it is the shape the problem
pushes you toward at two in the morning. It ends RFC-001 §03.

### C. A confirmation the ops API verifies rather than looks up — **chosen**

When a request carrying the visitor's session confirms a card, the app can claim
that record — the same `DraftStore.claim` that has always been the gate, which
checks the confirmed flag and retires the draft as it hands it over — and then
**sign what it claimed**. The ops API verifies the signature and writes what is
inside it.

The gate does not move. It is still *checked in code, before a database session
is acquired, on every write*. What changes is the evidence it consumes: a
dictionary lookup becomes an HMAC verification, and the thing being established
is the same sentence — a request carrying this visitor's session marked this card
confirmed.

## The design

`chip_chat.api.grants`. A grant is a compact signed object carrying:

| Field | Why it is in the signature |
| --- | --- |
| `action` | so a grant for one route cannot be spent on another |
| `demo_id` | so a captured grant is worthless to anybody else |
| `reference_id` | so a grant for one card cannot authorise a different one |
| `arguments` | **the procedure's own argument list**, so nothing on the wire reaches a procedure |
| `grant_id` | the single-use retry key |
| `expires_at` | two minutes, not the card's fifteen |

`arguments` is the field that carries the weight. It is the stored procedure's
positional arguments after the retry key, built in the app from the *claimed
record* — the draft's own lines, priced at the draft's own restaurant, in the
draft's own channel — and handed to `chip_chat.api.ops._arguments` unread. There
is therefore no argument anywhere in the deployed write path through which a
model could alter an order between the card the visitor read and the row that
gets written. That is the same property `docs/ops-api.md` step 4 already claimed
for the in-process design, preserved rather than weakened, and by the same
mechanism: the service never looks at the second thing.

**The signing key is derived, not stored.** Both tiers already hold one shared
secret — `CHIP_CHAT_OPS_KEY`, minted by `make ops-key` into Key Vault, presented
by the app on `x-cilantro-ops-key` and compared by `function_app._authentic`.
Adding a second Key Vault secret would be a second thing to mint, reference,
rotate and get wrong, and `docs/ops-api.md` already records what an *unresolved*
Key Vault reference costs in confusion. So `grants.signing_key` is one HMAC of
that secret under a fixed label. It costs nothing, adds no credential, and buys
one real thing: the ops key travels on every request as a bearer header, and the
key that signs confirmations never leaves either process.

**Replay needs no shared state, and this is the part worth reading twice.** The
grant's single-use id *is* the retry key, and every procedure spends its retry
key inside its own transaction with a `MERGE` (`sql/12_procedures.sql`). A
replayed grant therefore does not write twice; the second attempt finds the
first's receipt and replays it. That mechanism already existed, for the
connection that dies after the procedure committed, and it turns out to be
exactly the right shape for this second job. The alternative — a spent-nonce
table both processes read — would have reintroduced the shared state this design
exists to avoid, one layer down and with a worse failure mode.

**The wire format is deliberately not a JWT.** `base64url(payload)` and
`base64url(HMAC-SHA256)`, two segments, no header. A header segment is a field
naming the algorithm, and a field naming the algorithm is the `alg: none`
foothold. There is one algorithm, it is not negotiable, and the version lives
*inside* the signature so that a second one could only ever be introduced by
somebody holding the key.

## What an attacker can and cannot do

The threat model the PRD actually asks about is a **fully compromised model** —
prompt injection from the corpus, a sabotaged system prompt, an adversarial
visitor. `agent/tests/test_sabotage.py` plays a model that obeys a prompt written
to break both gates. Under this design:

| The attacker | What happens |
| --- | --- |
| Calls `place_order` with a draft id it invented | The app claims from *this visitor's* store, finds nothing, and refuses. No grant is minted, no request is composed, and the ops API is never asked. |
| Calls `place_order` for a draft it saw but the visitor never confirmed | Same. `DraftStore.claim` raises `DRAFT_NOT_CONFIRMED` before anything is signed. |
| Asserts a confirmation in a tool argument | There is no such argument on any of the eleven tools. `test_sabotage.py` holds every write tool to a list of ten spellings the sabotaged prompt tries. |
| Tries to alter the order between the card and the write | The arguments are inside the signature and the ops API does not read the body's. |
| Tries to write for another visitor | The `demo_id` is signed and checked against the session header the app resolved. A well-formed grant on the wrong session is refused before a database session is acquired. |
| Replays a grant it captured | Same retry key, so the procedure replays a receipt. One row. |
| Waits and replays later | Two minutes. |
| Mints a grant | It cannot. The key is not reachable from any tool, and the model has no channel that emits an HTTP request. |

**What it can do, stated plainly:** it can place an order the visitor genuinely
confirmed, at a moment the visitor did not intend — by stalling, or by placing a
card the visitor had mentally abandoned but not let expire. And it can decline to
place a confirmed order. Both are inside the gate, because the gate is about
consent and not about timing, and neither is a write nobody agreed to. The
fifteen-minute card TTL is the only bound on the first and it is invented
(RFC-001 §10 says so).

**What an attacker who has compromised the app *process* can do** is mint any
grant, because the process holds the key. That is not a regression and it is not
something this design could close: the same attacker already holds the ops key
and can call the write path directly, and held the confirmation flag before any
of this existed. What the boundary still buys — and the reason the write role
stays where it is — is that such an attacker is confined to what the four stored
procedures allow: catalogue-validated lines at a published price, a published
reward at its published cost, the visitor's own orders, all under row access
policies. Not arbitrary SQL against `CHIP_CHAT.ACCOUNTS`. That is a materially
smaller blast radius than a compromised app with a write role, which is the
comparison that matters.

## What it cost

**Two transcriptions of one declaration.** `_order_arguments` now exists in both
`chip_chat.api.ops` (for the in-process claim) and `chip_chat.api.orderdesk` (for
the app's claim). They must agree, and `api/tests/test_grants.py` holds them to
each other by driving both tiers and comparing the arguments the procedure was
actually called with. They are deliberately not shared: an import would hide a
disagreement, and the tiers are meant to be separable.

**Two rejection codes that are not new rejection codes.**
`chip_chat.api.grants.GrantCode` has two values and neither is in
`PRECONDITION_REJECTIONS`. A grant that does not verify is not a seventh kind of
failure — it is *this visitor has no confirmed record for this write*, so
`_as_record_rejection` maps it onto the draft or confirmation vocabulary and
carries the grant's own sentence through as the detail. The diagnosis survives;
the published list stays complete. `eval/adversarial/gate2.py`'s siege asserts
that every code in that list is provoked by a probe, which is a good rule that two
un-attacked codes would have quietly weakened.

## What is still not solved by this

**The draft store is still per-process, and now that matters more.** A second
replica of the chat app would hold its own drafts, and a visitor whose Confirm
landed on the other replica would be told `DRAFT_NOT_FOUND` having done
everything right. `min_replicas = 0` and `max_replicas` make that reachable
today. `chip_chat.api.orderdesk` logs the process id and the store's size on
every refused placement for exactly this reason — the two causes of a missing
draft look identical to a visitor and must not look identical in a log. Making
the app-tier store shared is the same ticket `BudgetLedger` has been waiting on,
and this design does not make it easier or harder.

**Clock skew is assumed small.** The grant's expiry is absolute epoch seconds
checked against the verifier's own clock. Two Azure services in one region agree
to well within the two-minute window; nothing checks that they do.

**The key is not rotated.** `make ops-key` is deliberately idempotent and will
not rotate a secret that exists, because rotating it without rolling the caller
takes the write path down. Rotation is now a *two*-effect change rather than one:
it invalidates in-flight grants as well as in-flight requests. Two minutes of
in-flight grants, which is the reason that TTL is two minutes rather than fifteen.
