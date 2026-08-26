# `vision/` — the photo pipeline, front end first

RFC-001 §07 gives the vision pipeline six stages and is explicit that **their
ordering is the design**: moderation happens before inference so nothing
unmoderated reaches a model, and SKU resolution happens after inference so no
model output is trusted as a product identifier.

What is implemented here is stages 0 to 5 — the whole path from a hostile upload
to a draft of real catalogue rows. The first four happen before anything names a
product and decide whether a hostile upload ever reaches a model; the fifth is
the step that names one, and it is the step with no model in it.

| Stage | What it does | Where |
| --- | --- | --- |
| 0 Read | Byte ceiling and deadline, while the body arrives | `reader.py` |
| 1 Validate | Size, magic bytes, allowlist, pixel ceiling | `validate.py` |
| 2 Normalize | Strip metadata, re-encode, downscale | `normalize.py` |
| 3 Moderate | Content Safety, then the write | `moderation.py`, `store.py` |
| 4 Describe | Structured slots, no free text | `describe.py`, `vocabulary.py` |
| 5 Resolve | Deterministic catalogue match | `matcher.py` |
| 6 Propose | Priced, confirmable draft | [#62](https://github.com/gganssle/chip_chat/issues/62) |

## Using it

A library rather than a route, for the same reason the spend cap is one: the
FastAPI app is [#66](https://github.com/gganssle/chip_chat/issues/66) and the
shape of its request path is not this package's business. What *is* this
package's business is that the ordering above cannot be got wrong by whoever
writes that route — there is one entry point, and it does every step or raises.

```python
from chip_chat.otel import chat_turn
from chip_chat.vision import (
    AzureBlobStore,
    AzureImageAnalyzer,
    ImageModerator,
    PhotoIntake,
    UploadRejectedError,
    content_length,
)

intake = PhotoIntake(
    store=AzureBlobStore.from_env(),
    moderator=ImageModerator(analyzer=AzureImageAnalyzer.from_env()),
)

with chat_turn(session_id=sid, turn_index=n, message=text):
    try:
        photo = await intake.accept_stream(
            upload_file,
            declared_media_type=upload_file.content_type,
            declared_length=content_length(headers.get("content-length")),
        )
    except UploadRejectedError as refusal:
        return upload_error(refusal.message)  # nothing was written
    return uploaded(str(photo.blob_ref), photo.retention_notice)
```

`accept_stream()` rather than `await file.read()` and then `accept()`, and the
difference is stage 0: reading the body first buys whatever the sender chose to
send before any ceiling gets a vote. `accept()` is still there for a caller that
already holds bytes, but the stream form is the one a route should reach for —
it enforces *this intake's* ceilings rather than whichever ones the route
remembered to pass.

`accept()` emits `guard.content_safety`, which RFC-001 §09 places under
`chat.turn` — so it is called inside one, and `chip_chat.otel` enforces that
rather than merely documenting it. The guard belongs to the turn it protects,
next to the spend cap and in front of `agent.step`.

Stage 4 takes the ref that came back, and nothing else:

```python
from chip_chat.vision import AzureVisionModel, MealDescriber, Vocabulary

describer = MealDescriber(
    AzureVisionModel.from_env(),
    images=AzureBlobStore.from_env(),
    vocabulary=Vocabulary.from_env(),  # CHIP_CHAT_VISION_VOCABULARY
)

with agent_step(index=0), tool_call(ToolName.MATCH_MEAL_FROM_PHOTO, ...):
    try:
        description = describer.describe(photo.blob_ref)
    except DescribeError as declined:
        return say(declined.message)  # the lane failed; the turn did not
show(description.notes)  # display-only, and the only reader
resolve(description.meal)  # stage 5. There are no notes on it.
```

`vision.describe` is a child of `tool.<tool_name>` in RFC-001 §09's tree, so
`describe()` is called inside one — the same enforcement, one level down. For
the callers that are not the agent (a batch over the labeled photo set, a
script), `describe_as_tool()` opens those two spans itself.

Stage 5 takes the meal, and a catalogue:

```python
from chip_chat.catalog import load_catalog
from chip_chat.vision import MealMatcher, Outcome, SlotRules

matcher = MealMatcher(load_catalog(blobs), rules=SlotRules.from_env())

resolution = matcher.resolve(
    description.meal,  # no notes on it to parse
    restaurant_id=session.store_id,
    content_version=description.content_version,
)
if resolution.resolved:
    return card(resolution.items(), resolution.total())  # #62 renders it
return ask(resolution)  # #55 writes it
```

`matcher.resolve` is the other child of `tool.<tool_name>`, so the same rule
applies and `resolve_as_tool()` is the same escape hatch.

**The vocabulary has to be generated before any of this runs.** It is not
committed, on purpose:

```bash
python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all
python -m chip_chat.catalog --landing landing --offline \
    --vocabulary "$SITE_PACKAGES/chip_chat/vision_vocabulary.py"
export CHIP_CHAT_VISION_VOCABULARY=chip_chat.vision_vocabulary
```

## Six properties, each easy to undo by accident

### Nothing is read unbounded

`validate()` takes `bytes`, and its first gate compares `len(data)` against the
ceiling. That gate is right and it is also too late on its own: something has to
have read the body for `len` to have an answer. `reader.py` is the gate in front
of it, and it closes two holes no size ceiling can see.

**A small declared size with a large body.** `Content-Length` is a number the
client typed, exactly like `Content-Type`. It is believed in one direction only:
a declared length *over* the ceiling refuses before a byte is read, because a
sender who admits to being oversized can be taken at their word. A declared
length *under* the ceiling buys nothing, sizes nothing, and truncates nothing.

**A body that never ends.** Eight mebibytes at one byte a second is under an
eight mebibyte ceiling for ninety-seven days, and a few hundred of those
connections is the whole worker pool. So the read carries
`CHIP_CHAT_UPLOAD_MAX_SECONDS` as well as a size, checked between reads.

What it does *not* do is bound a socket that connects and then goes silent
inside a single `read` — no in-process loop can, and that one belongs to the
server's own read timeout.

### The declared content type is never trusted

`Content-Type: image/jpeg` is there because a client typed it, and anyone can
type it. What a file *is* is decided in `sniff()` from its first bytes, and
nothing else consults the declared type for the verdict.

The gate that matters most is the one that looks unnecessary: **SVG is refused.**
It genuinely is an image format, browsers render it, and it can carry script and
remote references — and being XML, it has no magic number. A check that asks
"does this parse as an image" rather than "does this start with a signature we
allow" is exactly the check that lets it through.

The declared type is still *recorded* — `declared_matches_bytes` — because a
mismatch is a useful signal for [#80](https://github.com/gganssle/chip_chat/issues/80).
It is a signal and not a verdict: iOS labels camera-roll photos
`application/octet-stream` often enough that refusing on mismatch would refuse
real visitors while stopping nothing the byte check does not already stop.

### Nothing unmoderated is ever stored

The ordering requirement is the one thing on this page that a correct-looking
change can quietly undo, so it is arranged rather than documented:

```
validate ──▶ normalize ──▶ screen ──▶ put ──▶ blob_ref
                              │        │
      screen() takes a NormalizedImage │  the write is the statement *after*
      and nothing else, so stage 3     │  screen(), so a refused photograph
      cannot precede stage 2 ──────────┘  is never in the container at all
```

`PhotoIntake` cannot be constructed without a moderator — there is no default
and no `None` — so there is no configuration of it that skips stage 3. And
because the write comes last, a flagged image is not merely kept away from the
vision model: there is no blob for stage 4 to be handed a reference to.

`tests/test_moderation.py` asserts this against the container rather than
against a return value. `StoreWatchingAnalyzer` records how many blobs existed
at the moment Content Safety was called, and the only correct answer is zero —
swapping the two statements in `accept()` leaves every happy path working and
fails nine tests.

### "Strip EXIF" is the wrong specification of the job

Deleting the EXIF block leaves XMP, which carries GPS too. It leaves the JPEG
comment segment, the MakerNote, and whatever the next phone vendor invents.

So `normalize()` does not remove metadata — it never carries any across. It
reads pixels out of the decoded image and constructs a new one from them, and a
new image has nothing to leak because it has never had anything. The orientation
tag is the single thing read before it is dropped, because dropping it without
applying it is how phone photographs arrive at the model on their side.

Re-encoding is also what neutralises a class of malformed-file payload. A
polyglot with an archive glued to its tail does not survive, because only the
pixels are copied forward and pixels cannot encode a parser bug.

### The model describes; it never names a SKU

That sentence is D3, and stage 4 is where it is either true or merely
aspirational. Three things make it true, and the prompt is not one of them.

**The vocabulary is generated, so there is no food name in this package.** The
enums come from a module the catalogue build wrote — `python -m
chip_chat.catalog --vocabulary <path>` — which `vocabulary.py` loads by dotted
name from `CHIP_CHAT_VISION_VOCABULARY`. There is deliberately no default and no
fallback: a built-in one would be the hand-maintained list the generation exists
to replace, and it would be reached on exactly the deployment where somebody
forgot the build step.

`tests/test_vocabulary.py` settles the claim the only way it can be settled —
by changing the catalogue and watching the accepted vocabulary change with it.
The same response is accepted under one build and refused under the next, with
no edit to this package in between.

**The schema is enforced by the API rather than by parsing.** The response format
is strict structured output, so the decoder cannot emit a token outside the enum.
The answer is validated anyway, because "the vendor promised" is not a
foundation D3 should rest on — and a violation is **rejected, not repaired**.
Every repair available here (clamp the confidence, drop the unexpected key, pick
the nearest term) is a guess about a photograph made by something that never saw
it, and the nearest-term guess in particular is a fabricated SKU with a
plausible spelling.

There are two schemas and the difference is the vendor's fault. The catalogue's
is RFC-001 §07 exactly: nine properties, two required, because a photograph
showing no beans should come back with no `beans`. `strict_schema()` makes two
edits to it and neither changes what it means:

- **Every property becomes required.** Strict mode has no notion of an optional
  key, so the optional ones become required-and-nullable and the validator maps
  the nulls back to absent on the way in.
- **Numeric bounds are dropped.** `minimum` and `maximum` are on strict mode's
  unsupported list, and a schema carrying one is *refused outright* — so leaving
  them in would break every call rather than loosen one check. They stay in the
  catalogue's schema, which is what the validator reads, so the bound moves to
  our side of the wire rather than disappearing. That is why a confidence of 1.4
  is a case with a test rather than a case that cannot arise.

The catalogue's schema stays the definition; the strict form is an adapter to
one vendor's enforcement mechanism, and keeping them apart is what stops the
vendor's constraints leaking into the design.

**`notes` cannot reach the matcher, because it is not on the object the matcher
is given.** `DescribedMeal` has no `notes` field. `notes` lives on `Description`
beside it:

```python
description = describer.describe(photo.blob_ref)
show(description.notes)  # display-only, and the only reader there is
resolve(description.meal)  # stage 5. There are no notes on it to parse.
```

That is the difference between a rule and an arrangement. "Do not parse `notes`"
is obeyed until somebody is in a hurry. A matcher handed an object with no notes
on it cannot parse them at all — and the test asserting so does not check the
field list, it walks the entire object graph reachable from `meal` and asserts
the sentence is not in it at any depth, under any name.

### Counting meals, and the two things the prompt is actually for

`meals_visible` counts **orderable meal-sized compositions**, and
`docs/decisions/multi-meal-photos.md` puts that definition on this issue rather
than leaving it to the model: a bowl next to a bag of chips is one meal. V0
gates the whole pipeline on this integer reaching two, so a loose reading fires
the decline on the most ordinary photograph anyone sends.

The same decision forbids asking the model to rank prominence or pick a primary
meal — an unverifiable visual judgement from the component whose output D3
refuses to trust. The schema returns one slot set plus a count, and the count is
spent on knowing when to stop rather than on knowing what to build.

Those two, and calibration, are the whole of what the prompt buys. Everything
else is enforced. A model that ignores every line of `SYSTEM_PROMPT` still
cannot name a food the catalogue does not publish.

**No catalogue term appears in the prompt**, and a test asserts it. RFC-001 §07
illustrates the vessel slot as `bowl|burrito|tacos|salad|quesadilla`; copying
that in would be a hand-maintained vocabulary in the one file nobody would think
to regenerate.

### When the vision model is down

RFC-001 §10 allows a lane to fail and forbids the conversation failing with it,
so every way stage 4 can go wrong raises, and every raise carries the line to
show the visitor. `DescribeUnavailableError` is the deployment being unreachable
or answering with nothing; `DescriptionRejectedError` is it answering something
the schema does not permit. The visitor sees one sentence for both. A trace sees
two types — an outage is operational and a violation is either a model
regression or a vocabulary that has drifted from the deployment, and those want
different people looking at them.

The sentence is deliberately *not* stage 3's neutral line. That one is neutral
because naming what moderation detected hands an uploader something to iterate
against. Nothing about the vision deployment being down is worth concealing, and
"I can't use that photo" would be a small lie about a photo that is fine.

### Confidences have to mean something

D3 moves the failure "into a slot confidence we can threshold on", which is only
true if the confidences carry information. `confidence_profile()` measures a run
— what share of slots came back at exactly 1.0, how many distinct values
appeared, how they spread — and `is_meaningfully_distributed()` puts bounds on
it. Half the slots at 1.0 is plausible on clear photographs; nearly all of them
is a model reporting that it answered rather than how sure it was.

The labeled photo set is [#56](https://github.com/gganssle/chip_chat/issues/56)
and does not exist yet. What ships here is the check plus a test proving it
catches the shape it is looking for; #56 feeds it real photographs and scores
the number.

### Nothing is named that the catalogue does not publish

That is stage 5, and it is the other half of D3. Stage 4 makes it impossible for
the model to *say* a product name; `matcher.py` is where one is said, and it
holds a `MenuCatalog` and nothing else — no client, no deployment, no network.
The only path from a described meal to a product identifier is a lookup in that
catalogue, so there is no fuzzy match against model text anywhere in it and no
nearest-term fallback. A term that resolves to nothing produces a question, and
a question names no item.

Two lookups, and neither is a string comparison against something a model wrote:

- **A vessel and a protein are each half of an entree.** `CMG-101` is the
  Chicken Bowl; neither `bowl` nor `chicken` identifies it, and the vocabulary
  leaves `item_ids` empty for both slots so that nothing can resolve "a bowl" to
  a SKU without learning what was in it. The pair resolves through the published
  `(item_type, primary_filling)`, and both published names come off the same
  vocabulary rows the model's enum was generated from.
- **A modifier's identity is per-parent.** Guacamole is one identifier on a
  burrito and another on a taco, at different prices —
  [`docs/action-surface.md`](../docs/action-surface.md) §1.3 calls resolving it
  to one identifier and reusing it "the first mistake a naive matcher makes". So
  the vocabulary's `item_ids` is a *candidate set*, and the answer is the
  `modifiers` row joining a candidate to the entree the meal resolved to.

A menu that sells a Chicken Bowl and a Steak Burrito does not sell a steak bowl.
Both halves of that description are terms the catalogue publishes and the pair is
not a row, so the answer is a question — not the nearest entree, which would be a
real SKU on an order nobody asked for.

The test for this is not thirty photographs. It is *every answer the schema
permits*: each vessel crossed with each protein, rice, bean, and every subset of
the salsas and toppings. A photograph can only ever produce one of them, so a
matcher that fabricates nothing across the whole space fabricates nothing from a
photograph either. #56 asks the different question — precision and recall against
real food.

### Below the floor, required and optional part company

D3 moves the failure into a confidence we can threshold on, and PRD V5 says what
to do at the bottom of that range: ask, do not guess. The floors are per slot
because the slots are not equally forgiving, and they are configuration because
[#56](https://github.com/gganssle/chip_chat/issues/56) exists to tune them
against a labeled set — a constant is not tunable.

| Slot | Floor | Required | Why this number |
| --- | ---: | :---: | --- |
| `protein` | 0.75 | yes | A wrong protein is a different meal at a different price, and the slot a visitor is most likely to send back. |
| `vessel` | 0.70 | yes | Also a different order, but a photograph shows the vessel plainly and a model is rarely unsure of it — a floor above the protein's would fire on almost nothing and refuse the odd good photograph for it. |
| `rice` | 0.55 | yes | Required by the published grammar and frequently half-hidden under everything else. A protein's floor here would question most real photographs. |
| `beans` | 0.55 | yes | The same. |
| `salsas` | 0.50 | no | Optional, so the floor decides what is *dropped* rather than what is asked. |
| `toppings` | 0.50 | no | The same. |

`rice` and `beans` are required because
[`docs/action-surface.md`](../docs/action-surface.md) §1.3 reads their groups off
the published menu as `(1, 1)` on every burrito, bowl and salad: a bowl with no
rice selection is not an under-specified bowl but an invalid one. Requiredness is
a knob too — `CHIP_CHAT_MATCHER_RICE_REQUIRED` — because the grammar is per
`item_type` and a taco has no rice group at all.

The asymmetry below the floor is the part worth arguing:

- A **required** slot that is missing, low, or resolves to no catalogue row
  becomes a clarifying question, and **nothing is proposed alongside it**. A card
  missing its protein and priced as though it were not is worse than a question:
  the price is wrong and the omission is invisible at a glance.
- An **optional** slot in the same state is **dropped, and recorded**. A topping
  the model half-saw must not arrive as an order the visitor did not want, and
  the card is editable in place — so the cheap correction is adding one back
  rather than noticing one that was never mentioned. It is recorded because a
  floor whose effect nobody can see is a floor nobody can tune.

### A total is a sum of published prices, or it is nothing

Money is per restaurant, because Chipotle's is — see
[`decisions/menu-pricing.md`](../docs/decisions/menu-pricing.md). Every resolved
item carries the `item_prices` row for the restaurant being quoted, and the total
is their sum. If any one of them has no price row, the total is `None` rather
than a smaller number: a partial sum looks like a price, is lower than the real
one, and says nothing about the line it is missing.

### One catalogue, one vocabulary

`Description.content_version` is the build whose vocabulary constrained the
model. Hand it to `resolve()` and a matcher holding a different catalogue raises
`CatalogueDriftError` instead of resolving terms from one menu against the rows
of another — which is how a real SKU ends up in front of a visitor for food the
photograph does not show. It is a build fault, so it raises; the four ordinary
answers (`resolved`, `clarify`, `several_meals`, `not_orderable`) are an
`Outcome` rather than an exception, because a deterministic matcher answering
"not this" is a result.

### The count gates the pipeline; it does not shape the draft

At `meals_visible >= 2`, stage 5 does not run — RFC-001 §07, and
[`decisions/multi-meal-photos.md`](../docs/decisions/multi-meal-photos.md). The
schema returns one slot set, so on a frame with several meals those slots
describe the photograph rather than any one meal, and resolving them would
produce a draft composed entirely of real catalogue items that nobody in the
picture is eating. The count travels onto the `Resolution`, because PRD V7
requires saying how many were seen rather than picking one.

`is_chipotle_style` false is the other refusal — PRD V4. What to offer instead is
[#55](https://github.com/gganssle/chip_chat/issues/55); declining to build a
draft out of a poke bowl's slots is this package's.

### The image never crosses a tool boundary

The upload returns a `blob_ref` and that is all it returns. `BlobRef` carries a
container and a name and has no method that yields image bytes. Reading a photo
back is a separate capability with a separate name — `BlobReader` — and stage 4
is handed one explicitly. A ref that could fetch its own bytes would put an
image one attribute access away from every place a ref is legitimately passed,
which is every span and every tool argument in this lane.

Stage 4 does send the pixels to the vision deployment, as a data URI: the
uploads account has shared keys disabled and its blobs are readable only by the
app's identity, so there is no URL the model could fetch. That is a model call,
not a tool argument. `vision.describe` records the ref and the structured
output, and a test asserts the encoded image appears in no span attribute. This is not a
size optimisation: it is what keeps a photograph out of the model's context, out
of a tool call's recorded arguments, and out of every span and log line those
produce.

Names are `uploads/<date>/<uuid4>.jpg` — unguessable, and carrying nothing about
the visitor. No session id, no address, no filename. A container listing is not
a record of who was where.

## Stage 3: what it refuses, and what it says

### Content Safety being down means no vision lane, not an unmoderated one

Every path out of `screen()` that is not "the service answered, and its answer
was below every threshold" refuses: a transport error, a timeout, a response
missing a category, a severity the service declined to fill in. RFC-001 §10
allows a lane to fail and forbids the conversation failing with it, so the
visitor is asked to describe their meal in words — which is exactly what happens
when the vision model itself is down.

Both refusals a visitor can reach here say the **same neutral line**, and that
is deliberate. Neutral means it does not name a category, does not moralise, and
hands an uploader nothing to iterate against. Two different lines would be a
signal: an outage that announced itself would tell the previous uploader that
theirs, specifically, was the one that got flagged. Which of the two happened is
on `guard.content_safety`, along with the categories and severities — the
operator can read it and the visitor cannot.

### Which refusals are neutral, and which are helpful

The dividing line is **content disclosure**, and it is not the same line as "is
this an abuse case". Most refusals here *are* abuse cases and are still worded
helpfully, because none of them says anything about what is in the file:

| Reason | What the visitor is told |
| --- | --- |
| `empty`, `too_large`, `too_slow` | The specific thing that was wrong, and what to do. A size limit is not a disclosure, and "something went wrong" in front of one just makes a visitor try the same photo four times. |
| `not_an_image`, `unsupported_format` | That it is not a photo, or not a readable one. Both are decided from a signature, before anything looks at content. |
| `too_many_pixels`, `corrupt` | That it is too big to open, or did not arrive intact. Both are measurements. |
| `disguised_payload`, `unsafe_image`, `moderation_unavailable` | The **same neutral sentence**, byte for byte. Each of these is a verdict reached by looking *inside*. |

`disguised_payload` — a file whose signature says one format and whose body
decodes as another — is on the neutral side for the reason the other two are.
"That file claims to be a PNG and decodes as something else" confirms both the
detection and the method, and invites a better disguise. And no message
interpolates anything from the upload: no filename, no declared type, no byte
excerpt, no sniffed format. The one value any of them quotes is the size ceiling,
which is a server-side constant.

### The thresholds, and why the violent one is looser

Image analysis reports one severity per category on a four-level scale — 0, 2,
4, 6 — and the threshold is the lowest severity that refuses. These are chosen
for *this* endpoint, where the expected upload is a photograph of a burrito
bowl:

| Category | Block at | Why this number |
| --- | --- | --- |
| `Sexual` | 2 | No photograph of a meal is legitimately sexual, so there is no false positive to trade against. A public, unauthenticated endpoint attached to a restaurant brand is the wrong place to be relaxed about this one. |
| `SelfHarm` | 2 | Same reasoning, and the cost of being wrong the other way is higher than a refused photograph. |
| `Hate` | 2 | Symbols and text in a frame. Nothing about an orderable meal requires tolerating any of it. |
| `Violence` | 4 | **The deliberate exception.** Knives, cleavers, raw meat and cutting boards are ordinary food photography and are exactly what this classifier's low band reports. Blocking at 2 would refuse real meals; 4 is depiction rather than implement. |
| *unknown* | 2 | A category the service adds later is one nobody chose a threshold for, and an unchosen threshold must not default to permissive. |

The asymmetry is the point: a false positive costs one visitor one photograph
and a sentence asking them to type what they wanted, while a false negative puts
a stranger's image in front of a model, in a trace, and in a demo.

Each is overridable — `CHIP_CHAT_MODERATION_VIOLENCE_BLOCK_AT` and its
siblings — and none may be set to a value that switches a category off. There is
no "never refuse": 0 would refuse everything and 8 is rejected, because
disabling a category is a decision to make in the open rather than in an
environment variable.

## Expiry: it is 48 hours, not 24

The design says photos are deleted after 24 hours. They are not, and cannot be.
Blob lifecycle management has **day granularity**, and the engine takes up to 24
hours to begin executing after a policy change and then runs periodically. A
blob written a minute after a run is collected by the next one:

```
upload ──▶ 24h (the shortest a lifecycle rule can express)
           + up to 24h (the engine's run interval)
           = deleted 24-48 hours after upload
```

`RETENTION_NOTICE` therefore says 48. This is a promise made to strangers about
their photographs and it should be one we actually keep, which means quoting the
ceiling rather than the best case. `tests/test_retention.py` holds the copy
against what the Terraform actually configures, so the two cannot drift.

### The trap is soft delete

**Blob soft delete must stay OFF on the uploads account.** With it on, the
lifecycle rule does not delete anything — it *soft*-deletes, and the images are
then retained for the full soft-delete window. From outside, that is
indistinguishable from working correctly: the blob vanishes from a listing on
schedule and sits on the account for a week.

It is disabled by *omitting* two blocks in `infra/terraform/storage.tf`, because
the provider's minimum for `days` is 1 and there is no way to write "zero days"
and mean it. The omission is load-bearing, and an ordinary-looking edit undoes
it. Three things guard it:

| Guard | Catches |
| --- | --- |
| `tests/test_retention.py` | The edit, in review |
| A Terraform `postcondition` | The edit, at `apply` |
| `make infra-check-uploads` | Somebody turning it on in the portal |

The last one is also how the "observe an object disappear" acceptance criterion
gets met, since that observation takes a day of wall-clock: run it, note a blob
name, run it again tomorrow.

## The ceilings

| Knob | Default | Why |
| --- | --- | --- |
| `CHIP_CHAT_UPLOAD_MAX_BYTES` | 8 MiB | Bounds the read. A 12-megapixel phone JPEG is 3–5 MB. |
| `CHIP_CHAT_UPLOAD_MAX_PIXELS` | 50 M | Bounds the **decode**, which the byte ceiling does not — a 67-byte PNG can honestly declare 8000×8000. |
| `CHIP_CHAT_UPLOAD_MAX_EDGE` | 1024 | The model's working resolution. Vision models bill by tile; sending 4032×3024 pays to transmit detail the provider discards. |
| `CHIP_CHAT_UPLOAD_JPEG_QUALITY` | 85 | Invisible re-encoding, small blob. |
| `CHIP_CHAT_UPLOAD_MAX_SECONDS` | 30 | Bounds the **read**, which the byte ceiling does not — a trickle is under every size limit forever. |

The matcher's floors are the other knobs, one pair per slot —
`CHIP_CHAT_MATCHER_<SLOT>_THRESHOLD` and `CHIP_CHAT_MATCHER_<SLOT>_REQUIRED`.
Every one is optional and falls back to the table above, so an unset environment
is the argued starting point rather than an unthresholded matcher; a value that
does not parse fails at startup, because a misspelled threshold that silently
kept the default would be a tuning run that measured the wrong number.

Uploads are also rate limited and charged to the turn's budget, and those knobs
live in [`api/`](../api/README.md) with the rest of the spend cap:
`CHIP_CHAT_SESSION_UPLOADS_PER_WINDOW`, `CHIP_CHAT_SOURCE_UPLOADS_PER_WINDOW`,
`CHIP_CHAT_UPLOAD_WINDOW_SECONDS` and `CHIP_CHAT_UPLOAD_TOKEN_CHARGE`.

Those defaults also land a normalized photo inside Content Safety's own limits —
50×50 to 2048×2048 and no more than 4 MB — which is another reason stage 3 sits
behind stage 2. They are configurable though, and a photograph the service will
not look at must not become a photograph nothing looked at, so `moderation.py`
makes a fitted copy when it has to: downscale to the ceiling, and pad the short
side up to the floor rather than upscaling, since padding adds white margin
where enlarging would invent pixels.

HEIC is in the allowlist because it is what an iPhone camera roll holds. Safari
usually transcodes to JPEG on upload and sometimes does not, and "sometimes the
photo you picked just fails" is not something a visitor can debug.

## Tests

```bash
uv run pytest vision/tests
```

`testing.py` ships the fixtures rather than `tests/` because the acceptance
criteria are stated in terms of them and #66 will want the same store double and
the same `StubImageAnalyzer` — "stage 3 fails closed" is only a claim a test can
settle if it can make Content Safety unreachable without reaching Azure to do
it. `photo_with_location()` attaches GPS in all three places a camera writes
it — EXIF, XMP and a JPEG comment — so that "EXIF is stripped" is a claim with
something behind it. `conftest.py` builds the payloads that are not photographs:
archives, an ELF binary, a shell script, HTML, a PDF, an SVG with script in it,
and a decompression bomb whose IHDR is patched and CRC-corrected so that it is
dangerous for the right reason rather than merely corrupt.

`tests/test_matcher.py` reaches across to `catalog/tests/catalog_fixtures.py`
for its last two cases, which resolve against a catalogue built by harvesting
the harvest tests' fixture site rather than against one this package wrote. A
matcher that worked only against the shape of its own fixtures would pass every
other test in the file and fail on the first real catalogue. The fixtures it uses
for everything else — `menu_catalog()` beside `generated_vocabulary()` in
`testing.py` — are built from one set of terms on purpose: a vocabulary and a
catalogue from two different builds is the drift `resolve()` raises on, and a
fixture pair that could drift would make every assertion in the file conditional.

`tests/test_upload_abuse.py` is the adversarial suite from #80, and it asks a
different question from `test_validate.py`: not "was the verdict right" but
"what did refusing cost". It asserts on how many bytes the reader took, on
whether the pixels were ever allocated, and on whether two refusals a visitor
can reach are the same sentence. `ScriptedStream` and `TricklingStream` in
`testing.py` are what make the first two assertable — a `BytesIO` hands over
twenty megabytes as cheerfully as one, and never takes any time to do it.

`conftest.py` also opens a `chat.turn` around every test in the package, because
`guard.content_safety` lives under one and `chip_chat.otel` refuses to emit it
anywhere else. It records the spans while it is there, so the tests that assert
on traces — several of #52's acceptance criteria are stated in terms of them —
can just ask for the `spans` fixture.
