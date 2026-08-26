# `eval` — measuring the things that are otherwise opinions

Golden set, adversarial suite, Arize experiments. What ships today is the first
of those for the vision lane: **the labeled photo set and its scorer**, issue
[#56](https://github.com/gganssle/chip_chat/issues/56).

```
chip_chat.eval.photos
├── labels     what a person says is in each photograph
├── coverage   #56's scope, as checks the set passes or fails
├── run        every frame through the real lane
├── scoring    component P/R/F1, detection, outcomes
└── report     the baseline, as Markdown
```

```bash
python -m chip_chat.eval.photos --check      # free
python -m chip_chat.eval.photos --catalog <dir> --out eval/photos/BASELINE.md
```

The set itself lives in [`photos/`](photos/) — read its `README.md` before
adding a frame, and `BASELINE.md` for what has and has not been measured.

## The design, in four claims

### The ground truth is held to the same rule the model is

RFC-001's D3 makes it structurally impossible for the vision model to name a
food the menu does not sell: its enums are generated from the live catalogue.
Ground truth gets the same treatment. `LabeledSet.against(vocabulary)` checks
every labeled term against the vocabulary the describer was constrained by, and
refuses the whole set on one bad term — because a label naming `carnitas` on a
menu that does not sell it would score the model *wrong for being right*, and a
manifest with one term from another catalogue build has no claim on the rest.

Three more refusals, each a way the set could quietly stop being ground truth:
a per-meal label on a frame holding two meals (the slots describe the picture,
not either meal — `docs/decisions/multi-meal-photos.md`); a required slot that
is neither given a term nor named unreadable (silence is not absence); a slot
claimed as both read and unreadable.

### A slot the photograph does not answer is scored in neither direction

There is rice inside a foil-wrapped burrito and nobody looking at the photograph
knows which. Scoring that slot as "should have said white rice" measures
clairvoyance. Scoring it as "should have said nothing" rewards a describer that
gives up. So `unreadable` slots are dropped from the truth set *and* from the
prediction set — and the number of times the model filled one anyway is reported
separately, because a describer naming the rice inside a sealed wrapper is
guessing rather than reading, and that is worth knowing even though it cannot be
a false positive.

### Components are scored twice, before and after the floors

`described` is stage 4's slots as the model returned them. `believed` is
`Resolution.seen` — what stage 5 was willing to act on after each slot's
confidence floor. The PRD's *photo → order* metric is the second one, because a
slot below its floor never reaches an order.

Reporting only one would answer the wrong question. Issue #54 shipped its floors
as an argument from what each mistake costs rather than as measurements, and
said so: *"these are the numbers issue #56 exists to move."* One aggregate F1
cannot move them. The gap between the two tables, per slot, can — a floor too
high shows as `believed` recall falling away from `described` recall with
precision barely moving; a floor too low shows as the two being identical and
precision poor in both.

### A good score on the wrong set is invisible to any score

Thirty clean overhead bowls would produce an excellent F1 and prove nothing, and
nothing in the scorer can see it. So `coverage.py` holds #56's prose scope as
executable requirements — how many low-light frames, how many partially eaten,
how many multi-meal, both vessels, both proteins — and the report prints
coverage *above* the scores rather than below them. Two of those requirements
come from `docs/decisions/multi-meal-photos.md` rather than from #56, and carry
their source, so nobody deletes them later as arbitrary.

## Fixtures are not datasets

`chip_chat.eval.photos.testing` builds a thirty-one frame set of coloured
rectangles and a describer that answers from a script. That is how the scorer's
arithmetic gets driven at full size against numbers computed by hand: start from
a run that is right by construction, introduce one known mistake, check that the
one cell that should have moved is the one that did.

It is a fixture for the arithmetic and would be a fraud as a dataset. No vision
model reads a bowl off a rectangle, so any score computed over one measures the
stub that produced it. The module says so in its first paragraph, and there is
no version of it that substitutes for photographs.
