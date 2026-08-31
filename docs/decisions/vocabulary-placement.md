# Decision: the generated vocabulary's destination is discovered, and the build asserts the import

**Issue:** [#110](https://github.com/gganssle/chip_chat/issues/110) · **Decided:** 31 August 2026, by Graham · **Verified:** 31 August 2026
**Changes:** `Dockerfile` (the runtime stage's placement of `build/vision_vocabulary.py`), `catalog/tests/test_vocabulary_placement.py`
**Does not change:** RFC-001 §07 — the vocabulary is still generated from the live catalogue at build time and still is not committed

---

The photo lane was withdrawn on the deployment. Every photograph a visitor
uploaded came back as *"matching a meal from a photo isn't available on this
turn"*, on the first turn of a fresh conversation as readily as mid-conversation.
That sentence is not an error path; it is `agent/loop.py` telling the truth about
a tool it was not offered. `build_photo_lane()` had returned `None`, and the
start-up log said why:

    photo lane: the generated vocabulary, the vision deployment or the uploads
    container is not configured, so match_meal_from_photo is not offered
    ...
    No module named 'chip_chat.vision_vocabulary'

## What was actually wrong

The runtime stage of the root `Dockerfile` placed the generated module with a
single COPY whose destination it spelled out of the base-image tag:

```dockerfile
ARG PYTHON_VERSION
COPY --chown=cilantro:cilantro build/vision_vocabulary.py \
     /app/.venv/lib/python${PYTHON_VERSION}/site-packages/chip_chat/vision_vocabulary.py
```

`PYTHON_VERSION` defaults to `3.13`, for which that path is correct, and the
mistake is invisible for as long as nobody changes it. The deployed image was
built with `PYTHON_VERSION=3.13.15` — a fully pinned base tag, which is the
*better* thing to write if you care about reproducing a build, and which is
therefore the change a careful person makes. But `PYTHON_VERSION` is a tag on
`python:<tag>-slim-bookworm`, and a virtualenv's library directory is always
`pythonX.Y`. It never carries the patch level. The two strings coincide for
`3.13` and diverge for `3.13.15`.

COPY does not complain about a destination directory that does not exist. It
creates it. So the build created

    /app/.venv/lib/python3.13.15/site-packages/chip_chat/vision_vocabulary.py

beside the real, populated

    /app/.venv/lib/python3.13/site-packages/chip_chat/{agent,api,catalog,…,vision,web}

and exited 0. The tree it created is on no `sys.path` anywhere. Nothing between
the moment `make vocabulary` generated the file and the moment a visitor
uploaded a photograph ever tried to import it, so the build passed, the push
passed, the deploy passed, `/healthz` returned 200, and the only signal was one
line in a start-up log and a politely worded refusal weeks of turns later.

Confirmed on the shipping Dockerfile before changing it, rather than reasoned
about: building it at `PYTHON_VERSION=3.13.15` exits 0, and in the resulting
image `python -c "import chip_chat.vision_vocabulary"` raises
`ModuleNotFoundError` while `find` locates the file under `python3.13.15` and
`sysconfig.get_paths()["purelib"]` reports `python3.13`.

## The decision

**Discover the destination; do not spell it. And make the build prove the import
rather than assume it.**

```dockerfile
COPY --chown=cilantro:cilantro build/vision_vocabulary.py /tmp/vision_vocabulary.py
RUN set -eu; \
    purelib="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"; \
    test -d "$purelib/chip_chat" || { …; exit 1; }; \
    cp /tmp/vision_vocabulary.py "$purelib/chip_chat/vision_vocabulary.py"; \
    rm /tmp/vision_vocabulary.py; \
    python -c 'import chip_chat.vision_vocabulary as v; print("vocabulary:", v.__file__)'
```

Two things are worth stating separately, because they fix two different
problems and only one of them is the bug that was reported.

**The destination is now the interpreter's own answer.** `purelib` is asked of
the same `python` that will later be asked to import the module, so the two
cannot disagree. The previous arrangement had a second source of truth for a
string that exactly one program consumes, and a second source of truth is only
ever as good as the last person who remembered it existed. This costs one RUN
layer and about a third of a second.

**The build now fails when the module does not import.** This matters as much as
the placement fix and arguably more, because the placement fix repairs one bug
while the assertion retires the class. The reason a wrong path survived review,
build, push and deploy is that no step in that chain asked the only question
that mattered. The import runs in the *runtime* stage — proving it in the build
stage would prove something about an interpreter that is thrown away — as the
`cilantro` user that will run the process, against the `python` on `PATH`. The
generated module imports nothing but `enum`, which is what makes an
unconditional build-time assertion cheap.

This is the same argument the repository makes everywhere else about loud
failure. `guard.py` refuses to be observability; `FundedTurn` cannot be
constructed without the budget check; `SpanSchemaError` raises on a tree RFC-001
does not describe. A container image is the one artefact here that had no such
property, and the withdrawn lane is what that costs. Withdrawing a lane at
start-up is still right — a deployment with no vision deployment configured
should serve the other four lanes rather than refuse to boot — but a *build* has
no such excuse. It knows at build time whether the file it just placed is
importable, so it should say so at build time.

The staging hop through `/tmp` is not decoration: the destination cannot be
known until an instruction has run, and COPY cannot be given a destination
computed by an instruction. The 8 KB that consequently lingers in one
intermediate layer is a smaller price than the shell heredoc or the pinned
Dockerfile frontend the alternatives would have cost.

## Verification, 31 August 2026

Built for `linux/amd64` on Apple silicon, both with the default
`PYTHON_VERSION=3.13` and with the fully pinned `PYTHON_VERSION=3.13.15` that
broke it. Both builds print the assertion's own line —

    vocabulary: /app/.venv/lib/python3.13/site-packages/chip_chat/vision_vocabulary.py

— and in both images `import chip_chat.vision_vocabulary` succeeds and resolves
to that path. `/app/.venv/lib` now contains only `python3.13`: the stray
`python3.13.15` tree is gone rather than merely unused, the installed file is
`cilantro`-owned, and the staging copy is removed.

The assertion was also shown to fail, rather than assumed to: a variant
Dockerfile that keeps the build-time import but restores the old spelled COPY
fails the build at `PYTHON_VERSION=3.13.15` with
`ModuleNotFoundError: No module named 'chip_chat.vision_vocabulary'` on the RUN
that broke. That is the exact image that shipped, and it is now unbuildable.

`catalog/tests/test_vocabulary_placement.py` holds the static half — that the
Dockerfile still asks `sysconfig` rather than spelling a path, that no
`site-packages` path is derived from `PYTHON_VERSION`, that the runtime stage
still imports the module, and that the dotted name it imports is the one
`infra/terraform/variables.tf` defaults `CHIP_CHAT_VISION_VOCABULARY` to. It
builds nothing; a test that needs a daemon is not a test that runs in `make ci`.

## What was not measured

**How long the photo lane had been withdrawn in production.** This is the number
a reader will want and it is not available. The evidence in hand is a start-up
log from the running revision and two reports from one user-testing session on
31 August 2026; neither establishes a start. The Container Apps revision history
would give the date the offending image was first rolled out, and the log
analytics retention window may or may not still hold the earlier start-ups, but
neither was consulted before this fix was made and neither is reconstructed here.
So the honest statement is that the lane was withdrawn on the revision that was
serving on 31 August, and that every prior revision built with a patch-pinned
`PYTHON_VERSION` would have had the same defect. **How many revisions that is,
and over what span, is unknown.**

**How many visitors uploaded a photograph into the withdrawn lane.** Related and
also unknown. The upload route charges `DEFAULT_UPLOAD_TOKEN_CHARGE` (1,500
tokens) and writes the image to blob storage before the turn discovers there is
no lane, so there is a countable population of stored images that nothing ever
looked at and budgets that were spent for nothing. Counting it means querying
the uploads container and the spend ledger, which was not done. The route's
refusal — the second, smaller half of #110 — is not this change.

**The layer-size cost of the extra RUN.** Not measured; the file is 8,490 bytes
and the assertion writes no bytecode (`PYTHONDONTWRITEBYTECODE=1`), so the
expectation is that it is noise against a ~300 MB image, but an expectation is
what it is and it is not in a table here.

## What this does not change

**RFC-001 §07.** The vocabulary is still generated from the built catalogue and
still is not committed, for exactly the reason the Dockerfile's comment has
always given: a checked-in copy is a hand-maintained list with an extra step, and
it would be wrong on precisely the deployment where somebody re-harvested. Only
*how* the generated file is placed has changed. `make image` still depends on
`make vocabulary`, and a bare `docker build .` still requires that the
generation has run — except that forgetting it is now a failed build instead of a
silently withdrawn lane, which is the improvement.

**The other two preconditions of `build_photo_lane()`.** A missing catalogue in
blob storage, and a missing vision deployment or uploads container, are separate
causes with separate warnings. This change fixes the one the deployment's log
actually named. Whether the deployed revision also lacks either of the others is
not established here, and `/healthz/lanes` remains the way to ask.

**The upload route's behaviour when the lane is withdrawn.** #110 asks for the
route to refuse — and not store, and not charge — when `Lanes.photo` is `None`.
That is a change in `api/`, owned separately, and nothing above substitutes for
it: a lane can be withdrawn for reasons that have nothing to do with this file.
