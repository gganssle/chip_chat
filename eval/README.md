# `eval` — measuring the things that are otherwise opinions

Golden set, adversarial suite, Arize experiments. Two of those ship today.

**The golden set** — issue [#29](https://github.com/gganssle/chip_chat/issues/29).
Thirty-four questions across the five lanes, each carrying the lane it should
route to, the PRD requirements it covers, and what has to be observed for it to
count as passed.

```
chip_chat.eval.golden
├── requirements  the PRD's identifiers, and what is delegated where
├── lanes         the five lanes, and which tool is in which
├── cases         the set: shapes, refusals, staleness check
├── coverage      #29's scope, as checks
├── run           every case through a deployment
├── scoring       per-lane pass rates, and the PRD's targets
├── report        the baseline, as Markdown
└── slice         the week-one agent loop, wearing the runner's seam
```

```bash
python -m chip_chat.eval.golden --check                    # free
python -m chip_chat.eval.golden --check --catalog <dir>    # and the menu terms
python -m chip_chat.eval.golden --catalog <dir> --out eval/golden/BASELINE.md
```

**The labeled photo set and its scorer** — issue
[#56](https://github.com/gganssle/chip_chat/issues/56). The vision lane's ground
truth, which the golden set delegates to rather than duplicating.

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

Both sets live beside their code — [`golden/`](golden/) and [`photos/`](photos/)
— each with a `README.md` to read before adding an entry and a `BASELINE.md` for
what has and has not been measured.

## Where the line between them is

They overlap nowhere and between them there is no gap, which is not a
coincidence — it is why the division is drawn where it is.

`eval/photos` runs the photo lane **directly**, from a blob reference through
stages 4 and 5. So it can score components against a person's reading of the
same frame, and lane selection is invisible to it: no model ever chose to call
the tool.

`eval/golden` runs **whole turns**. So it can score whether a photo message
reaches `match_meal_from_photo` at all, and component accuracy is invisible to
it: one case cannot say whether the salsa was right.

The golden set therefore holds exactly one vision case — routing — and delegates
`V2`–`V7` to the photo set by name, with the argument recorded in
`requirements.DELEGATIONS`.

## The golden set's design, in three claims

### Unscored is a third verdict, and it is load-bearing

A deployment declares which signals it can observe about a turn, and a check
whose signal is missing comes back `unscored` — never failed, never passed.

This is not fastidiousness. `chip_chat.agent.envelope` — decision D9's response
envelope, where a citation is an id the retriever returned rather than a sentence
the model wrote — exists and is imported by no caller, so no deployment in this
repository can report a citation. Scoring those checks as failures would produce
a report saying the agent never cites its sources, which is a claim about wiring
dressed as a claim about a model. Three of the checks need a judge for the same
reason and get the same treatment.

### Every requirement is covered by a case, or delegated with an argument

Three outcomes rather than two, and the middle one is what keeps the register
honest in both directions. Fold delegations into "covered" and the vision lane
looks scored here when it is scored in `eval/photos`; fold them into "uncovered"
and a complete set can never go green, so nobody reads the report.

Each delegation names its target and its reason, because a delegation without an
argument behind it is a gap somebody labeled to make a number look better.

### The set is held to the menu, the way ground truth is

`GoldenSet.against(catalog)` checks every published term a case leans on against
a built catalogue, and refuses the set on one bad term — the same move
`LabeledSet.against(vocabulary)` makes below, for the same reason.

It has a second job here. `cc-z1i` records that RFC-001 §07's generated vision
enums are not wired to the live catalogue yet, so the vocabulary in the tree can
drift from what is orderable with nothing to say so. A golden set that cannot
detect its own staleness would keep passing while the menu moved underneath it.

## The photo set's design, in four claims

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

Two of them, and the second is newer. `chip_chat.eval.golden.testing` holds a
`RoutingOracle`: a chat model that reads the golden set and calls, for each
message, exactly the tool that case expects. Running the set against it produces
a *ceiling* rather than a score — give a deployment perfect lane selection and
this is what the golden set still cannot get out of it. Every failure it leaves
is a property of the wiring, reproducible for free, and unfixable by any amount
of prompt work. Nothing about model quality survives a model that was told the
answer, and `eval/golden/BASELINE.md` says so where the numbers are.

`chip_chat.eval.photos.testing` builds a thirty-one frame set of coloured
rectangles and a describer that answers from a script. That is how the scorer's
arithmetic gets driven at full size against numbers computed by hand: start from
a run that is right by construction, introduce one known mistake, check that the
one cell that should have moved is the one that did.

It is a fixture for the arithmetic and would be a fraud as a dataset. No vision
model reads a bowl off a rectangle, so any score computed over one measures the
stub that produced it. The module says so in its first paragraph, and there is
no version of it that substitutes for photographs.
