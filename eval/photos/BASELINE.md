# Labeled photo set — baseline

**There is no baseline. No photograph has been scored.**

This file is written by hand today and is overwritten by
`python -m chip_chat.eval.photos --out eval/photos/BASELINE.md` the first time
the set has frames in it and a deployment to run them against. Until then it
records what is and is not known, because the alternative — a file with numbers
in it produced from something other than photographs — is the exact failure
issue [#56](https://github.com/gganssle/chip_chat/issues/56) exists to prevent.

| | |
| --- | --- |
| Frames labeled | 0 (need 30) |
| Frames scored | 0 |
| Component-level F1, photo → order | **unverified** — not unmet |
| PRD §05 target | ≥ 0.80, not yet measurable |

## What is unverified, and what that blocks

**PRD §05's `photo → order, component-level F1 ≥ 0.80`.** Unverified is not the
same as unmet: nothing has been measured, so nothing has failed. Do not read the
absence of a number as a passing grade or a failing one.

**Issue #54's confidence floors are still an argument, not a measurement.**
`chip_chat.vision.matcher._DEFAULT_FLOORS` says so in its own docstring —
*"these are the numbers issue #56 exists to move"*. They were chosen from what
each mistake costs (protein highest, because a wrong protein is a different meal
at a different price; rice and beans lower, because they are frequently
half-hidden), and that reasoning is sound and untested. They ship untuned, and
the tuning input is the `described` / `believed` gap that this run would print.

**Issue #53's fourth acceptance criterion is still half met.**
`confidence_profile()` and `is_meaningfully_distributed()` exist and are proven
to catch a describer pinned at 1.0 on every slot. Whether *this* deployment's
confidences carry information on real photographs is a question only the set can
answer, and the report has a section waiting for it.

**Issue #55's three photo paths are specified and unmeasured.** Food this
restaurant does not serve, a component nobody could read, several meals in one
frame — each has a behaviour and a test with a scripted describer behind it. The
report's "which path each frame took" section is where those stop being assumed.

**The Phase 6 demo criterion — a photo of your own lunch turning into a correct
order — has not been demonstrated.**

## Why it is empty

The photographs are the one part of this ticket that cannot be written. Issue
#56's licensing note rules out using someone else's — this is a public
repository, and a labeled dataset of another person's photographs is an
avoidable problem — and a set of synthetically generated images would be worse
than no set at all: no vision model can read a bowl off a rendered rectangle, so
every number computed over one would measure the generator. A number that is
wrong in an unknown direction is worse than an admitted hole.

`chip_chat.eval.photos.testing.synthetic_set` does build exactly such a fixture,
and its module docstring is explicit that it is a fixture for the arithmetic and
a fraud as a dataset. It is what lets the scorer be driven at thirty frames in
the test suite with a describer whose answers are known exactly. It is not this
set and cannot become it.

## What is ready

Everything except the frames.

- `labels.json` — the manifest, validated at load against itself and against the
  catalogue's published vocabulary.
- `chip_chat.eval.photos.coverage` — issue #56's scope as checks, so the set can
  be told it is not yet the set.
- `chip_chat.eval.photos.scoring` — component P/R/F1 per slot and overall, at
  both stages; multi-meal and non-Chipotle detection in both directions; outcome
  accuracy over #55's paths.
- `chip_chat.eval.photos.run` — every frame through the real lane, through the
  real stage 1 and stage 2, one `chat.turn` per photograph, one frame's failure
  recorded rather than fatal.
- `python -m chip_chat.eval.photos` — `--check` for free, or a scored run.

## To produce the first real baseline

1. Take the frames. `README.md` in this directory has the shot list and the
   licensing rule.
2. Label them. Same file, under "How to label one" — and read the paragraph on
   `unreadable`, which is the field most likely to be skipped and the one that
   decides whether a wrapped burrito scores the model on reading or on guessing.
3. `python -m chip_chat.eval.photos --check` until it exits zero.
4. `python -m chip_chat.eval.photos --catalog <dir> --out eval/photos/BASELINE.md`
   and commit the result, whatever it says.
5. Read the `described` / `believed` gap per slot and retune
   `CHIP_CHAT_MATCHER_*` against it. Re-run; the two documents diff.

Report the numbers that come out. A poor F1 is a finding about the vision stage,
and it is the finding this ticket was opened to get.
