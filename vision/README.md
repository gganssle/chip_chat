# `vision/` — the photo pipeline, front end first

RFC-001 §07 gives the vision pipeline six stages and is explicit that **their
ordering is the design**: moderation happens before inference so nothing
unmoderated reaches a model, and SKU resolution happens after inference so no
model output is trusted as a product identifier.

What is implemented here is stages 1 and 2 — the front of it, and the only part
that involves no model at all. Nothing here decides what a photograph *is*. It
decides whether a hostile upload ever reaches something that will.

| Stage | What it does | Where |
| --- | --- | --- |
| 1 Validate | Size, magic bytes, allowlist, pixel ceiling | `validate.py` |
| 2 Normalize | Strip metadata, re-encode, downscale, store | `normalize.py`, `store.py` |
| 3 Moderate | Content Safety | [#52](https://github.com/gganssle/chip_chat/issues/52) |
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
from chip_chat.vision import AzureBlobStore, PhotoIntake, UploadRejectedError

intake = PhotoIntake(store=AzureBlobStore.from_env())

try:
    photo = intake.accept(payload, declared_media_type=request_content_type)
except UploadRejectedError as refusal:
    return upload_error(refusal.message)  # nothing was written
return uploaded(str(photo.blob_ref), photo.retention_notice)
```

## Three properties, each easy to undo by accident

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

HEIC is in the allowlist because it is what an iPhone camera roll holds. Safari
usually transcodes to JPEG on upload and sometimes does not, and "sometimes the
photo you picked just fails" is not something a visitor can debug.

## Tests

```bash
uv run pytest vision/tests
```

`testing.py` ships the fixtures rather than `tests/` because the acceptance
criteria are stated in terms of them and #66 will want the same store double.
`photo_with_location()` attaches GPS in all three places a camera writes it —
EXIF, XMP and a JPEG comment — so that "EXIF is stripped" is a claim with
something behind it. `conftest.py` builds the payloads that are not photographs:
archives, an ELF binary, a shell script, HTML, a PDF, an SVG with script in it,
and a decompression bomb whose IHDR is patched and CRC-corrected so that it is
dangerous for the right reason rather than merely corrupt.
