# The public demo: what a stranger touches, and what it is allowed to say

This is the write-up for Phase 8's app tier — issues
[#66](https://github.com/gganssle/chip_chat/issues/66) (session store and
persona assignment), [#67](https://github.com/gganssle/chip_chat/issues/67)
(entry flow), [#68](https://github.com/gganssle/chip_chat/issues/68) (the chat
widget), [#69](https://github.com/gganssle/chip_chat/issues/69) (the persona
switcher) and [#70](https://github.com/gganssle/chip_chat/issues/70) (the
unaffiliated-demo framing). `docs/deployment.md` is the sibling for #71: how the
thing gets onto a URL and how to take it off one.

Everything here is about the tier a visitor can see. The parts they cannot —
the spend cap, the connection pool, the ops API — are written up in
`api/README.md`, `docs/ops-api.md` and `docs/snowflake-isolation.md`.

---

## 1. The one screen, and why the persona arrives on the second request

A visitor types an invented first name and is in the conversation. Two requests
and no navigation:

```
GET  /               the page: banner, name gate, an empty chat view, a composer
POST /api/entry      {"name": "Sam"}  ->  who you are, what to ask, and the chips
```

The page is the same document for everybody. The persona is not, so it does not
go in the HTML: `POST /api/entry` is what resolves the session cookie, asks
`VisitorDesk.admit` for a customer, and composes the opening sentence from the
row it got back. That split is deliberate in three ways.

It keeps the page cacheable and one string, which is what makes
`web/tests/test_page.py` able to assert on requirements rather than on a
rendered instance. It keeps the entry response the only place a persona is
described, so there is exactly one opening message rather than one in the HTML
and another after a switch. And it means the served page contains no account
data at all, which matters for the same reason the profile omits `demo_id`: a
document that never carried an identity cannot leak one to a cache, a proxy or a
screenshot.

The second screen is already loaded before the visitor types, so the only thing
between the name and the conversation is one JSON round trip. Measured against
the live deployment on a warm replica: **0.20 s** for `POST /api/entry` and
**0.17 s** for `POST /api/switch`, against a criterion of two seconds. See
`docs/deployment.md` §6.1 — and §6.3 for the turn latency, which is a different
and much less comfortable number.

## 2. The opening message is written, not filled in

Issue #67 sets the standard: *"not a template with holes, a sentence that reads
like someone wrote it"*, built from the persona's **narrative, home store,
points balance and characteristic order**.

All four of those are already in one field. `docs/decisions/persona-fixtures.md`
shipped `ACCOUNTS.persona_fixtures` precisely so that the opening message would
have a sentence to quote rather than four numbers to assemble:

> a regular at NC Town 1 Mall, 1,288 points on the card, and 99% of 79 orders
> the same Chicken Bowl with guacamole, white rice, black beans and cheese

That fragment was written by `data-gen`'s per-archetype template, measured from
the customer's real order history, and reviewed as twenty-eight rows a person
can read. Re-deriving the same sentence in `web/` from `points_balance` and
`home_store_name` would be a second, worse copy that can disagree with the
first — and disagreeing with the account tool is the failure the opening message
exists to prevent.

**What `chip_chat.web.persona` adds is the grammar around it.** The narratives
come in two shapes. Five archetypes read as noun phrases — *"a regular at
..."*, *"42 orders across 13 stores ..."* — and two read as verb phrases —
*"puts in the floor's lunch order at ..."*, *"turns up every couple of months
..."*. One lead-in cannot carry both. *"I've set you up as puts in the floor's
lunch order"* is the sentence a single template produces, and it is why `_FRAMES`
is keyed by `persona_id`:

| Archetype | Frame | Reads as |
| --- | --- | --- |
| `regular`, `newcomer` | `You're {narrative}` | You're a regular at ... |
| `lapsed` | `You were {narrative}` | You were a regular at ... until March |
| `explorer` | `You've got {narrative}` | You've got 42 orders across 13 stores |
| `office_manager`, `weekend_family`, `occasional` | `You're the one who {narrative}` | You're the one who puts in the floor's lunch order |

Then a closing invitation, also per archetype, because *"ask me anything"* is
the blank prompt PRD §06 says loses the visitor. The Lapsed Regular is told
*"those points are still sitting there"*. The Office Manager is told to *"ask
about allergens before you order for eleven people"*. A reason to type beats an
invitation to type.

`web/tests/test_page.py::test_the_opening_message_is_grammatical_for_every_archetype`
is what fails if somebody collapses this back into one string.

## 3. Chips, and why four of them

Issue #67 asks for tappable prompts spanning at least three capabilities.
`suggestions()` returns four, tagged with the lane each exercises: **account**,
**account**, **menu**, **order**, plus a **photo** chip appended to every set.

The wording changes per archetype and the coverage does not. *"What's my usual?"*
for the Regular, *"What are my points worth?"* for the Lapsed Regular, *"What
haven't I tried yet?"* for the Explorer. A visitor who switches personas should
see the demo ask a different question, not offer a different feature — that is
what makes the switch legible as a change of customer rather than a change of
app.

The `lane` field is not decoration. A criterion about coverage needs the
coverage to be a value something can count, and the tests count it.

## 4. The framing, which is a launch criterion

Issue #70's sentence, verbatim, is `chip_chat.web.copy.BANNER`:

> Unofficial demo, not affiliated with Chipotle Mexican Grill. All orders are
> simulated.

**It is `position: sticky` rather than a block at the top of the document.** The
criterion has two halves — on the entry screen *and* persisting in the chat
header — and a banner that scrolls away satisfies only the first. Sticky means
it is on screen at the bottom of a long transcript, on a phone, mid-order.
There is no control that hides it, no `aria-label="Close"`, and no local-storage
key that remembers a dismissal, because a notice a visitor can close is a notice
that is absent for every visitor who closed it.

**`noindex` twice.** `<meta name="robots" content="noindex,nofollow">` on both
pages, and an `X-Robots-Tag: noindex, nofollow` response header on *every*
route from a middleware, plus `/robots.txt` returning `Disallow: /`. The header
is the half that works when something fetches the page without executing it,
which is what a crawler does. Verified against the live URL in
`docs/deployment.md` §6.

**The branding review.** No logo, no wordmark, no image of any kind: the page
contains no `<img>` and no `<svg>`. The palette is a warm paper grey
(`#f6f5f3`) on near-black ink with a slate indigo accent (`#3d4a72`), which is
not any of the incumbent's colours — their brand red is `#A81612` and their
dark brown `#451400`, and `test_nothing_on_the_page_borrows_the_incumbents_branding`
asserts those hexes appear nowhere. The previous palette used a green accent;
it was changed, because "not their green either" is a weaker sentence than "not
a restaurant palette at all". The name *Cilantro* is already deliberately
distinct from the incumbent's assistant, and the visual design now is too.

**No bulk export.** The menu data is cached for the demo, not republished as a
dataset. This is asserted as an absence rather than as a 404 somebody remembered
to add: the application has exactly three `GET` routes — `/`, `/robots.txt` and
`/healthz` — and `test_no_endpoint_serves_a_bulk_export_of_the_corpus` fails if
a fourth appears. Answers cite their source pages, which is the attribution that
was wanted anyway for groundedness.

### 4.1 Takedown, in five minutes

The posture, from #70: *if anyone at Chipotle ever asks for this to come down,
take it down cheerfully. Having built it is the point, not keeping it online.*

The runbook step is in `docs/deployment.md` §7. It is one `az` command, and it
is the same kill switch the spend cap uses, so it has been exercised.

## 5. The card, from PRD Flow 3

```
BURRITO BOWL — Ballard
double barbacoa · white rice · black beans
mild salsa · cheese · + guacamole
——
$13.85 · 1,250 pts available
[ Edit ]  [ Place order ]  · simulated
```

`renderCard` in `web/src/chip_chat/web/page.py` draws exactly that: the title
line is the entree in caps with the store after an em dash, the modifier lines
are the line's selections joined with middots, a rule, then the money with the
points balance beside it, then the two buttons and the word.

**"Simulated" is in the action row.** Not a footnote and not a tooltip — the
same line as the button, where the eye already is because that is where the
buttons are. It is on the confirmation card, on the editor, and on every
receipt, and the `notice` field of the payload carries it too so that a card
rendered by something other than this page still says it.

**No write is reachable without the card.** The path from a typed message to a
placed order runs: model calls `propose_order` → the app returns a card →
the visitor presses **Place order** → the request carries `confirm_draft_id` →
`OrderDesk.confirm` marks the draft → the model's `place_order` finds a
confirmed draft. The model cannot press the button; `confirm_draft_id` is a
field on the *request*, beside the session cookie, and there is no tool that
reaches `confirm`. `api/tests/test_app.py::test_an_order_needs_the_button_and_not_the_prompt`
is the end-to-end proof, and it predates this work.

**Receipts persist.** A receipt is rendered in the transcript like any other
card, carries its order id, and says *"Kept in this conversation — ask me about
order CC-XXXXXX any time."* It stays in the log; the model's own message history
holds the same tool result, so a later question about it is answerable.

### 5.1 Editing in place

Pressing **Edit** turns the card into a small editor: a quantity stepper per
line, a remove control per modifier. Pressing **Re-price** posts to
`POST /api/draft/revise`, which mints a **new** draft on the same desk and
returns a new card.

Three properties fall out of it being a new draft rather than a mutation:

1. **An edited card is unconfirmed by construction.** There is no window in
   which a confirmation granted for one basket applies to another, because the
   confirmed thing has a different id.
2. **No model is called.** An edit is a catalogue lookup and an arithmetic. It
   costs nothing, it cannot be refused by the spend cap, and it returns in
   milliseconds — which is why it is a route rather than a sentence typed into
   the conversation. `test_an_edit_calls_no_model` asserts the call count.
3. **A rejected edit leaves the card that was already on screen.** The new draft
   is minted before anything is discarded, so an edit naming something not on
   the menu is a sentence in the transcript and not a lost order.

`chip_chat.api.drafts.DraftStore.revise` is the catalogue-priced version of the
same rule and is what this route will call once the ops API is the write path;
today the desk it uses is `SpendGate.desk`, because that is the desk the card
being edited came off.

### 5.2 Photographs

`POST /api/photo` takes the image as the request body — not a multipart form,
because the ceiling that matters is on bytes read off the socket and a multipart
envelope has to be parsed before the first gate can refuse anything.
`UploadLimiter` runs first, then `PhotoIntake.accept_stream` does validate →
normalize → moderate → store, and the route hands back a `BlobRef` string and
the 48-hour retention promise.

The photo appears in the transcript immediately, from a local object URL, with
the retention notice under it; what Cilantro made of it arrives in the bubble
directly beneath. The reference travels to the model as a line appended to the
visitor's own message, which is how the tool surface says it should arrive:
*"Call it only with a reference the app has given you for a photo the visitor
uploaded on this turn."*

**`PhotoRegistry` is why a reference is not a capability.** `BlobRef.parse`
stops a caller escaping the container and stops the model inventing a path.
Neither stops a caller naming a *well-formed* reference belonging to somebody
else's upload. So the app remembers what it minted, per session, and a chat
request naming a reference this session did not upload names no photograph at
all — the same rule as a draft id, for the same reason.

**Moderation runs inside the turn, and the span schema is what made that
explicit.** The first real upload against the deployed app came back *"I could
not take that photo in just then"*, and the container's log named the reason:
`SpanSchemaError: guard.content_safety must be a child of chat.turn, but was
opened under the trace root`. RFC-001 §09 puts image moderation under the turn
it happens on, `chip_chat.otel.spans.content_safety` enforces that by raising
rather than by asking, and the route had opened no turn. So the upload opens the
turn it is the first half of — which is the design and not a workaround, because
handing over a photograph *is* a visitor turn. The turn records `(photo upload)`
as its input rather than a sentence the visitor did not type.

Verified against the live URL on 27 August 2026: a 400×300 JPEG posted to
`/api/photo` came back in **3.25 s** with
`{"photo": "uploads/2026-08-27/c13f0629-….jpg", "retention": "Deleted within 48
hours."}`, and Application Insights shows one `guard.content_safety` span under
its `chat.turn`.

The photo *lane* is a separate question. `Lanes.photo` is `None` on every
deployment today, so `match_meal_from_photo` is not offered and the model will
say it cannot see the picture. That is `cc-mpd`'s work and not this tier's; what
this tier owed was the route, and the route is what
`chip_chat.vision.__init__`'s own docstring has been describing since stage 3
shipped with no caller.

### 5.3 Streaming, and exactly how far it goes

`POST /api/chat` answers in one of two shapes, chosen by `Accept`. A caller
asking for `application/x-ndjson` — which is what the widget asks for — gets
newline-delimited frames:

```
{"type":"open"}
{"type":"waiting"}
{"type":"waiting"}
{"type":"text","text":"Barbacoa is the spicier of the two, though "}
{"type":"text","text":"mild by most standards."}
{"type":"card","card":{...},"receipt":false}
{"type":"end","stopped":false}
```

Anything else gets one `ChatReply` object, which is what a test, a `curl` and an
eval harness want. Both are the same turn and the same fields.

**The streamed shape is load-bearing, not cosmetic.** Container Apps ingress
closes a response that has sent no bytes for sixty seconds, and p95 turn latency
on this deployment is 62.7 s — so the object shape of this route was losing
answers the app had already written and the visitor had already been billed for,
at exactly 60.19 s, ten times out of ten. The `waiting` heartbeat every ten
seconds is what stops that. Verified live: an 81-second turn arrived complete
where the same turn had been cut at 60. `docs/deployment.md` §3.12.

**What is streamed and what is not, said plainly.** The transport is real: the
generator is synchronous, so Starlette iterates it on a worker thread and the
event loop is never blocked; the turn itself runs on a thread of its own so the
generator can keep the response alive; the card is its own frame the moment the
turn produces one. The **tokens** are not. `chip_chat.agent.model.ChatModel` has exactly one
method and it returns a finished reply, so the prose is chunked in `_chunks`
after the turn rather than forwarded from the provider as it arrives.

Token streaming is an `agent/` change — a second method on that protocol and a
loop that can yield mid-step — and this route is written so that landing it
means replacing the body of `_chunks` and nothing else. Saying so here is
cheaper than a reader discovering it from a stopwatch.

## 6. Switching persona

One tap, from the chat header, beside the label of who you currently are.
`POST /api/switch` has a request model with **no fields at all**: the session
being left comes from the cookie, and the archetype to move away from is read
out of the store inside `VisitorDesk.switch`. A body with a field would be a
body an attacker could put a persona in.

Four things happen, in the order that makes the release real rather than
described:

1. A **new** session id is minted, so the browser leaves holding a different
   cookie than the one it arrived with. A switch is a new `demo_id` on a clean
   connection, not a mutation.
2. `VisitorDesk.switch` releases the old binding from the store **before** it
   makes the new one. The store is what `VisitorPool.for_session` resolves
   against, so a released session checks out nothing.
3. The old conversation and the old session's photographs are dropped. "No data
   from the previous persona survives" is not satisfied by a new identity in
   front of an old transcript.
4. The archetype being left is excluded from the choice, so a switch shows the
   Lapsed Regular after the Regular rather than a reshuffle. It is a preference
   and not a rule: on a roster with one archetype left, the tiers fall through
   rather than returning nobody.

In the browser the transcript is cleared **before** the request goes out, so the
restart is visible first and explained second — the right order for something
that just threw away what was on screen. Then `restart_message` says it:

> Starting over. Everything above belonged to somebody else, so I have put it
> down. You were a regular at CO Town 1 Mall until March 2026, and not seen
> since — 1,904 points still unredeemed from 51 orders. Those points are still
> sitting there...

Editing a persona rather than switching to one is #59, and is not offered here.

## 7. The session store, and what a restart costs

Issue #66's fourth criterion offers a choice: *survives a container restart, or
degrades in a way that is decided rather than discovered.* Both halves exist and
which one is in play is a single environment variable.

| `CHIP_CHAT_SESSION_JOURNAL` | Behaviour | Announced by |
| --- | --- | --- |
| unset | Bindings live in memory. A restart assigns returning visitors a new persona. | `WARNING` at assembly, naming the variable |
| a writable path | `FileJournal` appends every binding and replays it at start-up. | nothing; this is the intended state |
| a path that cannot be written | Falls back to memory rather than refusing to boot. | `ERROR` with the path in it |

**The deployed state is the first row, and that is a decision rather than an
omission.** The Container App has no mounted share. Adding one is a Terraform
change and an Azure Files account; it buys a returning visitor their own
persona back after a restart, and it costs a standing storage charge on a demo
whose whole cost argument is `min_replicas = 0`. A visitor who comes back after
a restart is assigned a fresh loaded persona and has a working demo; a visitor
who comes back to a *stranger's* account would be a correctness problem, and
that cannot happen — the cookie resolves to nothing and `admit` treats it as a
new session.

Two things do **not** survive a restart and never did: open drafts (a forgotten
draft is a `DRAFT_NOT_FOUND`, never a draft placed unconfirmed) and the
conversation history. Both are stated in their own modules.

The other two stores are process-local for the same stated reason: one replica,
one counter, one obvious place for a shared implementation to land. `max_replicas
= 1` is what makes that honest, and `docs/deployment.md` §3.7 is where it is
argued.

## 8. Which lanes are actually wired, and how to ask

`GET /healthz/lanes` answers it, from the deployment itself rather than from a
document that can go stale. It is #65's *"surfaced somewhere operable"*, and the
route is here rather than in `agent/` because `agent/` has no request path — and
because two of the five answers are this tier's to give: the probe needs a
**bound** session, since the Snowflake-backed lanes check a connection out of
the pool by session id, and it needs to be told whether the ops API is
available, because `agent/` does not import `api/`.

It is deliberately not the platform's liveness probe. A lane being down is a
fact an operator wants and not a reason to restart a container; RFC-001 §10 is
explicit that a lane may fail and the conversation may not fail with it, so
Container Apps stays pointed at `/healthz`, which answers for the process and
nothing else.

Asked of the live deployment on 27 August 2026:

```json
{"healthy": true, "down": [], "stale": [],
 "lanes": [{"lane": "knowledge",       "state": "not_wired", ...},
           {"lane": "account",         "state": "not_wired", ...},
           {"lane": "personalization", "state": "not_wired", ...},
           {"lane": "photo",           "state": "not_wired", ...},
           {"lane": "action",          "state": "not_wired",
            "detail": "no ops API configured; drafts are proposed and nothing is written"}]}
```

`healthy: true` with five `not_wired` lanes is the correct answer and not a
contradiction: nothing is broken, and nothing is connected. `build_service` is
called with `NO_LANES` on every deployment, deliberately and at length in its
own docstring — see §9 below for what that costs a visitor.

## 9. The one thing that is still true and worth saying

`Lanes()` is empty on the deployed app. `search_menu_knowledge`,
`get_points_balance` and `get_usual_order` answer from
`chip_chat.agent.hardcoded` — a three-item menu and one account fixture — and
each says so in its own result. `ask_account_question`, `get_recommendations`
and `match_meal_from_photo` are not offered at all, because a hardcoded NL→SQL
answer is exactly the plausible number PRD A4 forbids.

So a visitor assigned the Explorer, whose narrative says *"42 orders across 13
stores"*, will be told their points balance is the fixture's rather than the
one in the sentence they just read. That disagreement is real, it is the
account lane's absence rather than this tier's, and it closes when `cc-lpy4`
supplies a connection factory — at which point `SnowflakeRoster` replaces the
shipped roster and the account tool reads the same rows the narrative was
measured from. It is recorded here rather than discovered by the first person to
click the second chip.
