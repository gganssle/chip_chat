# `vision/` — the photo pipeline, front end first

RFC-001 §07 gives the vision pipeline six stages and is explicit that **their
ordering is the design**: moderation happens before inference so nothing
unmoderated reaches a model, and SKU resolution happens after inference so no
model output is trusted as a product identifier.

What is implemented here is stages 1 to 3 — everything that happens before a
model is involved at all. Nothing here decides what a photograph *is*. It
decides whether a hostile upload ever reaches something that will.

| Stage | What it does | Where |
| --- | --- | --- |
| 1 Validate | Size, magic bytes, allowlist, pixel ceiling | `validate.py` |
| 2 Normalize | Strip metadata, re-encode, downscale | `normalize.py` |
| 3 Moderate | Content Safety, then the write | `moderation.py`, `store.py` |
| 4 Describe | Structured slots, no free text | [#53](https://github.com/gganssle/chip_chat/issues/53) |
| 5 Resolve | Deterministic catalogue match | [#54](https://github.com/gganssle/chip_chat/issues/54) |
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
)

intake = PhotoIntake(
    store=AzureBlobStore.from_env(),
    moderator=ImageModerator(analyzer=AzureImageAnalyzer.from_env()),
)

with chat_turn(session_id=sid, turn_index=n, message=text):
    try:
        photo = intake.accept(payload, declared_media_type=request_content_type)
    except UploadRejectedError as refusal:
        return upload_error(refusal.message)  # nothing was written
    return uploaded(str(photo.blob_ref), photo.retention_notice)
```

`accept()` emits `guard.content_safety`, which RFC-001 §09 places under
`chat.turn` — so it is called inside one, and `chip_chat.otel` enforces that
rather than merely documenting it. The guard belongs to the turn it protects,
next to the spend cap and in front of `agent.step`.

## Four properties, each easy to undo by accident

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

### The image never crosses a tool boundary

The upload returns a `blob_ref` and that is all it returns. `BlobRef` carries a
container and a name and has no method that yields image bytes. This is not a
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

`conftest.py` also opens a `chat.turn` around every test in the package, because
`guard.content_safety` lives under one and `chip_chat.otel` refuses to emit it
anywhere else. It records the spans while it is there, so the tests that assert
on traces — several of #52's acceptance criteria are stated in terms of them —
can just ask for the `spans` fixture.
