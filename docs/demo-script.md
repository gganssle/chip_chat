# The five-minute demo

Issue [#90](https://github.com/gganssle/chip_chat/issues/90). Written 27 August
2026 against revision `0000018`.

## Read this before you perform it

**Two beats cannot currently be performed as the ticket describes them, and one
of them is the best beat in the script.** They are marked ⚠ below with what
actually happens instead. Pretending otherwise would produce a script that fails
in front of an audience, which is the one outcome a demo script exists to
prevent.

**Turns take about 34 seconds.** Median 34.2 s, p95 62.7 s, measured over 69
`chat.turn` spans. That is the single biggest constraint on this script: six
beats at 34 seconds is 3½ minutes of *waiting*, and there is no version of this
demo where you type a question and talk over the pause for four seconds. The
pacing below is built around it — every beat has something to say while the
answer is arriving, and the words are chosen so that the pause reads as
deliberate rather than broken.

**Recording is a human step and has not been done.** #90's second acceptance
criterion — a recording that survives the environment being torn down — is
**unmet**, and no part of it can be automated from here.

---

## 1. Personas, chosen in advance

Entry assigns an archetype; you cannot name it. Type the name, read the opening
line, and if you get the wrong archetype **tap the switcher** — it is one tap and
it announces the restart, which is itself beat 4.

| Beat | Archetype you want | How you know you got it |
| --- | --- | --- |
| 1–3 | **The Weekly Regular** | opening names a home store and a high order count |
| 4 | **The Lapsed Regular** | opening says "until March 2026, and not seen since" and quotes unredeemed points |
| 5 | either | — |

Verified live: `Sam` was assigned the Lapsed Regular — *"a regular at KY Town 1
Mall until March 2026, and not seen since — 14,495 points still unredeemed from
43 orders."* That sentence is the demo's best single line and it arrives in
0.2 s, before any model is involved.

---

## 2. The script

### Beat 1 — Cold entry (30 s)

> "This is a conversational assistant for a fast-casual restaurant. It runs on
> the restaurant's real published menu and on entirely synthetic customer
> accounts — no real customer data, no real orders, nothing is fulfilled. That
> notice at the top of the screen says so, and it doesn't go away."

Type a first name. Press enter.

> "The hardest problem in a demo like this isn't the model. It's that a stranger
> arrives with an empty account and has nothing to ask. So on entry you're given
> a fully populated customer — a home store, eighteen months of orders, a points
> balance — and the opening message tells you who you've become, so you know
> what's worth asking."

Read the opening line aloud. Point at the chips.

> "And those are tappable, so you don't have to think of the first question."

**This beat is fast — 0.2 s — and it is the only fast one.** Use the contrast:
say so, and it makes the model latency later read as *the model*, not the app.

### Beat 2 — A menu question, and the allergen boundary (60 s)

Type: **"Is the barbacoa spicy?"**

While it thinks:

> "The menu answers come from the restaurant's own published pages, harvested,
> chunked so that one menu item is one chunk with its nutrition and allergens as
> metadata rather than as prose a model has to re-read. Fixed-window chunking
> splits a nutrition table down the middle, and that produces exactly the
> confident wrong answer an allergen question can't tolerate."

Then type: **"Does the sofritas contain dairy?"**

> "This is the decision I'd most want to be asked about. It reports what's
> published, cites it, and stops. It does not reason one step past the source —
> 'the bowl has cheese and the salad doesn't, so the salad is dairy-free' is
> exactly the inference it refuses to make. There's a red-team suite of seven
> attack shapes on this one behaviour, including asking through a photograph and
> asking with emotional pressure attached."

⚠ **What actually happens today:** the knowledge lane is unwired on the
deployment, so this answers from a three-item hardcoded menu and says so in its
own result. The retrieval stack is built and measured — **allergen top-3 recall
100%**, stable across three sweeps — but the deployed app is not reading it.
**Say this out loud rather than hoping nobody notices.** It is a more
interesting sentence than the one you planned: *the retrieval is measured, the
wiring is one connection factory, and here is the honest state.*

### Beat 3 — ⚠ The isolation story (90 s) — CANNOT BE PERFORMED AS WRITTEN

The ticket asks for two browsers, two names, two different correct answers to
the same question. **This does not currently work**, because the account lane is
unwired: both browsers get their persona from a shipped fixture, and
`get_points_balance` answers from a hardcoded account, so the second browser
does not produce a different correct answer — it produces the *same* answer,
which tells the opposite story.

**Do this instead, and it is arguably better.** Put the terminal on screen:

```sql
USE ROLE CHIP_CHAT_READ;
SELECT COUNT(*) FROM CHIP_CHAT.ACCOUNTS.ORDERS;              -- 0
SELECT MAX(row_count) FROM CHIP_CHAT.INFORMATION_SCHEMA.TABLES
 WHERE table_name = 'ORDERS';                                -- 18,898
```

> "Same connection, same instant. Eighteen thousand rows in the table, zero rows
> visible, because no visitor is bound to this session. That's a row access
> policy keyed to a session variable, and it's the whole security argument: the
> agent is a program that takes untrusted input and produces untrusted output,
> so it is never the thing that decides *whose* data to return. There is no
> identity parameter on any tool for it to get wrong. The absence of the
> parameter is the enforcement mechanism."

Then show the tool table and point at the missing column: three of the six read
tools take **no arguments at all**.

> "A test fails if anyone ever adds one. We checked it by adding one — eleven
> tests go red."

### Beat 4 — The persona switcher (30 s)

Tap the switcher.

> "One tap, new persona, and it says the conversation restarted rather than
> quietly continuing a thread whose history now belongs to somebody else. That's
> a new session id on a clean connection — the old binding is released before
> the new one is made, which matters for exactly the reason the last beat did."

Read the Lapsed Regular's opening aloud. It is the best copy in the product.

### Beat 5 — A photo becomes an order (60 s)

Upload a photo of a burrito bowl.

> "The vision model never names a menu item. It describes what it sees, and a
> deterministic matcher resolves that description to SKUs that exist in the
> catalogue. That split is what makes fabrication impossible — a model that
> can't name an item can't invent one — and the database refuses any SKU the
> catalogue doesn't contain, so it's enforced twice."

Then, on the card:

> "Every action renders a card before it happens, it's editable in place, and
> every card says *simulated*. The confirmation isn't a prompt instruction — the
> ops API rejects any draft that wasn't confirmed by a request carrying the
> visitor's session. We tested that with a deliberately sabotaged system prompt:
> an attacker who has completely won the argument with the model still doesn't
> get a write."

⚠ **What actually happens today:** the upload, moderation, storage and
transcript rendering all work — verified live at 3.25 s — but `Lanes.photo` is
unwired, so the model cannot describe the image. Show the upload and the card
mechanics; describe the matcher rather than demonstrating it.

### Beat 6 — The trace (60 s)

Open the span tree for the turn you just ran.

> "One turn, one span tree. `chat.turn` at the root, the budget check and the
> content-safety check underneath it, then one `agent.step` per model round trip
> with the completion and any tool calls nested inside. Those names were fixed
> before anything was built, which is why the evaluation could be attached to
> them afterwards rather than retrofitted."

> "And this is where the observability argument lands: the same instrumentation
> targets two backends with no code change, because it's OpenInference. We
> proved that rather than asserting it — the instrumentation diff to switch is
> empty, and the agent-side diff is two lines of a version manifest."

---

## 3. The two-minute cut

Beats 1, 3 and 6. Entry, isolation, trace.

Drop the menu and photo lanes entirely — they are the two most affected by the
unwired state, and two minutes has no room for a caveat. What survives is the
part that is completely true: **a loaded persona in 0.2 s, a database that
physically cannot return another visitor's rows, and one span tree per turn.**

Script:

> "Type a name, and you're a customer with eighteen months of history — because
> a demo where you arrive with an empty account is a demo nobody plays with."
>
> *(entry, read the opening line)*
>
> "Everything it says about you comes from a synthetic account in Snowflake. The
> assistant never decides whose data to return — it can't, there's no identity
> parameter on any tool. A row access policy does it. Here's the same connection
> seeing eighteen thousand rows as zero, because nobody's bound to it."
>
> *(the two SQL statements)*
>
> "And every turn is one span tree, which is how you debug an agent and how you
> evaluate one. Same instrumentation, two backends, no code change."
>
> *(the trace)*

---

## 4. Acceptance criteria, honestly

| Criterion | Status |
| --- | --- |
| Script written with the actual words | **met** — this document |
| Rehearsed against the live app | **partial** — entry, switcher and upload verified live; the two ⚠ beats cannot be rehearsed as written |
| Recording produced and committed or linked | **NOT MET** — a human step, not automatable |
| A two-minute cut exists | **met** — §3 |
| Personas chosen in advance and verified | **met** — §1 |
| Works without narration for someone watching cold | **not met** — and would not today, for the reasons in §2 |

The last row is the one that matters, and it is the same finding as
`docs/launch-readiness.md`: the mechanisms are built and the deployment is not
running on them. **Re-record this script after `cc-lpy4` lands.** Beats 2, 3 and
5 all become demonstrable in one change, and beat 3 goes back to being the
two-browser story it was designed to be — which is much better told with two
browsers than with a terminal.
