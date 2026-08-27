# Decision: Foundry thread storage suffices, and the app owns everything that is a security artefact

**Issue:** [#11](https://github.com/gganssle/chip_chat/issues/11) (RFC Q2) · **Decided:** 26 August 2026, by [#102](https://github.com/gganssle/chip_chat/issues/102) · **Probed:** 26 August 2026
**Reads with:** [`foundry-agent-shape.md`](foundry-agent-shape.md), which settled the agent shape this follows from, and [`visitor-persistence.md`](visitor-persistence.md), which set the constraint it had to satisfy
**Changes:** RFC-001 §04 — `thread_id` is a new column on `demo_visitors`

---

This record exists because #11 was answered across two other documents and never
collected. `foundry-agent-shape.md` settled the *ownership* half as a consequence
of choosing a hosted agent; `docs/phase-0-verification.md` §"Thread retention"
answered as much of the *empirical* half as an afternoon can. Neither is filed
under #11, and the issue is closed, so somebody looking for the answer under its
own name found nothing. That is what this file fixes.

## The question

#11 asked a binary: if Foundry thread storage suffices, the app holds only
sessions and budgets; if it does not, the app needs its own conversation store
and the app tier changes materially.

A constraint was added after it was written. **[#9] decided visitor state
persists between visits**, so the answer had to hold for a *returning* visitor
resuming a conversation, not merely for a single session.

## The decision: neither extreme

**Threads hold message history. The app holds a pointer to them, plus everything
that is a security artefact.**

| State | Owner | Why |
| --- | --- | --- |
| Message history | **Foundry thread** (Microsoft-managed) | The managed runtime's job; addressed by `thread_id` |
| `thread_id` | **App** — `demo_visitors.thread_id` | The pointer must outlive the visit; #9 made the visitor row durable |
| Order drafts | **App** | `propose_order` mints a `draft_id` the ops API later validates as confirmed — exactly the security role #11 anticipated |
| Confirmation state | **App** | RFC §06: enforced in the ops API, never in the prompt and never in the thread |
| Receipts | **Snowflake** (`orders`, `loyalty_ledger`) | System of record. A later turn re-queries; it does not remember |
| Budget counters | **App session store** | RFC §11 requires an inline synchronous check before every model call |
| Persona assignment | **App** (`demo_visitors.persona_id`), mirrored in Snowflake | Identity originates server-side; RFC §05 |

The line the table is drawing: **anything an attacker would benefit from
rewriting lives where the app can check it.** A confirmation state in a thread is
a confirmation state a prompt can talk about. A draft id validated by the ops API
is not.

**A visitor switching persona starts a new thread** rather than continuing the
old one. A thread carrying another persona's context degrades lane selection and
pollutes the trajectory evals.

No Cosmos DB account is created, because the agent is a hosted agent on a
basic-setup project. `foundry-agent-shape.md` has the full working, including the
part that is not about Cosmos at all: standard setup wants an AI Search resource
of its own, colliding with the one free AI Search service per subscription that
[#10] already spends.

## The empirical half, and exactly how far it got

**Established, 26 August 2026:**

1. **Threads are id-addressable from a cold client.** A thread created in one
   process was read back — thread and message — from a separate process with a
   separately acquired token. Nothing about retrieval depends on session
   continuity, which is the property a returning visitor needs.
2. **The service expresses no expiry.** A thread object is five fields, and none
   of them is `expires_at`, a TTL or a retention field. Checked across
   api-versions `v1`, `2025-05-01` and `2025-11-15-preview`, which answer
   identically. There is no thread-retention setting on the Foundry account
   either.

**Not established: the retention period itself.** "Survives an arbitrary gap"
cannot be demonstrated in an afternoon, and asserting it from the absence of a
field would be inference dressed as measurement.

The absence *is* genuine evidence, and for a second reason worth keeping: **a
retention period the API declines to express is one the app cannot code
against**, because nothing would let it pre-emptively migrate a thread about to
lapse. So an instrument shipped instead of a claim.

```bash
uv run python -m chip_chat.agent.threads fetch thread_aZOWnxHgCwhx7WNj9lcmvUN3
```

That baseline probe thread was created 26 August 2026 with one message.
**Re-running `fetch` on it after a week, a month, a quarter is the experiment.
The first run that fails is the retention answer; the last that succeeds is the
lower bound.** If it ever fails, the fallback is the cheap one: message history
moves into the durable per-visitor store #9 built anyway, and nothing else in the
table above changes.

## Why the issue is closed with an open experiment inside it

Because the *decision* is made and the *measurement* is a calendar item, and
leaving a ticket open for months to hold a `fetch` command is how a tracker stops
being read. The command and the thread id are here, where the decision is. Run
it; if it fails, this record gets an amendment rather than a new decision.

[#9]: https://github.com/gganssle/chip_chat/issues/9
[#10]: https://github.com/gganssle/chip_chat/issues/10
