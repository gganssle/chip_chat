# `eval` — measuring the things that are otherwise opinions

Golden set, labeled photo set, adversarial suite, allergen red team, Arize
experiments. Four of those ship today, the first two are promoted into one
versioned dataset, and the dataset is what the trajectory and grounding evals
score against. One more set sits below all of them and holds no model at all:
the labeled **retrieval** set, which scores the knowledge lane's retriever
before anything can paraphrase over it.

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
python -m chip_chat.eval.golden --lanes wired              # against the deployment
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

**The adversarial suite** — issues
[#30](https://github.com/gganssle/chip_chat/issues/30) and
[#82](https://github.com/gganssle/chip_chat/issues/82). Twenty-eight attacks on
the two properties PRD section 05 makes pass-or-fail, including the concurrency
test RFC-001 section 05 asks for by name — run sustained, and refused where the
round could not have caught anything.

```
chip_chat.eval.adversarial
├── attacks     the suite: families, breaches, and what a manifest may not be
├── canaries    the secret that makes a disclosure a count rather than a reading
├── soak        how hot a round got, and whether anybody had to wait for a connection
├── run         every attack through a target, and some of them at once for a while
├── scoring     outcomes, and the two gates as counts that never average
├── postmortem  where each attack died, which is what `held` throws away
├── coverage    #30's and #82's scope, as clauses
├── report      the baseline, as Markdown
├── slice       the week-one loop, several visitors, one order desk between them
└── testing     targets broken one way each, so the detectors are demonstrated
```

```bash
python -m chip_chat.eval.adversarial --check        # free
python -m chip_chat.eval.adversarial --structural   # free, and the one to run
make adversarial-redteam                            # sustained, and the one CI blocks on
```

**The versioned dataset** — issue
[#72](https://github.com/gganssle/chip_chat/issues/72). Both sets above, flattened
into rows and given a version that is a hash of their content, so that #73 can run
a prompt change as an experiment against a fixed thing and the comparison means
something.

```
chip_chat.eval.dataset
├── entries   one flat row per case and per frame
├── versions  the fingerprint, and the column it rides in
├── build     both manifests in, one dataset out
├── store     the seam, and Arize AX behind it
├── publish   create it, or add a version -- never mutate
└── testing   a store that remembers, for driving a publish
```

```bash
python -m chip_chat.eval.dataset --check     # free, and holds the repo to its own build
python -m chip_chat.eval.dataset --write     # after adding a case or a frame
uv run --with arize python -m chip_chat.eval.dataset --upload
```

[`dataset/README.md`](dataset/README.md) is the write-up: what a version is, why
it is a hash rather than a number, and the three things a publish will not do.

**Tool selection and trajectories** — issue
[#74](https://github.com/gganssle/chip_chat/issues/74). The headline metric, and
the only thing in `eval/` that reads the **span tree** rather than a return
value: which tool was reached for, in what order, and whether that was the lane
the dataset row expected. PRD §05 sets it at ≥ 95%, the highest bar in the table.

```
chip_chat.eval.trajectory
├── expectations  the dataset's rows, and the two tables behind them
├── trees         a turn's spans, read back as the calls it made
├── shapes        four ways it was wrong, and the order they are asked in
├── scoring       per-lane accuracy, and the gap under the target
├── run           every row through a source
├── slice         the week-one loop, recorded
├── coverage      #74's scope, as clauses the rows meet or do not
└── report        the baseline, as Markdown
```

```bash
python -m chip_chat.eval.trajectory --check      # free
python -m chip_chat.eval.trajectory --ceiling    # free, and the one to run
```

[`trajectory/README.md`](trajectory/README.md) is the write-up: why the four
failure shapes are counted apart, why the wrong-query check is deliberately
weak, and what a split trace does to every number in the document.

**Groundedness and citation presence** — issue
[#75](https://github.com/gganssle/chip_chat/issues/75). The other two headline
metrics, and the ones that make the allergen boundary real rather than
aspirational: groundedness of food and policy claims at ≥ 0.95, and menu claims
made without a citation at **zero**. It reads the same span trees the trajectory
eval does and asks a different question of them — not *which tool*, but *what
did that tool actually return, and is the answer attached to it*.

```
chip_chat.eval.grounding
├── questions   the dataset's rows, and what each one is owed
├── evidence    the retriever.search spans, as the passages the turn really had
├── run         the response, the seam it arrives through, and the judge
├── verdicts    five findings, and the order they are asked in
├── scoring     one rate, four counts, and a category held to counts alone
├── coverage    #75's scope, as clauses the rows meet or do not
├── report      the baseline, as Markdown
├── slice       the week-one loop, answered and recorded
└── testing     spans and verdicts by hand, and the ceiling
```

```bash
python -m chip_chat.eval.grounding --check      # free
python -m chip_chat.eval.grounding --ceiling    # free, and the one to run
```

[`grounding/README.md`](grounding/README.md) is the write-up: why the two
metrics never share an average, why the allergen and dietary category is held to
counts rather than to a higher percentage, and why three of its five findings
report *unmeasured* rather than a number.

**The labeled retrieval set** — issue
[#50](https://github.com/gganssle/chip_chat/issues/50). Forty questions, the
published places that answer each one, and the ablation RFC-001 §08's design
choice is defended by. The only eval here with **no model anywhere in it**, which
is the whole of its argument: *"retrieval bugs are nearly impossible to diagnose
once a model is paraphrasing over them."*

```
chip_chat.eval.retrieval
├── questions       the set: questions, and the places that answer them
├── corpus          those places, resolved against a corpus release
├── configurations  the four arms: keyword, vector, hybrid, + reranker
├── run             every question through a retriever, per arm
├── scoring         recall@3, hit@3, MRR, P@1; restraint; breaches
├── coverage        #50's scope, as clauses
├── report          the baseline, as Markdown
└── testing         an index in memory, so the sweep is free
```

```bash
python -m chip_chat.eval.retrieval --check                    # free
python -m chip_chat.eval.retrieval --offline --chunks <path>  # free, and the one to run
```

[`retrieval/README.md`](retrieval/README.md) is the write-up: why a label names a
*place* rather than a chunk id, why recall is counted over labels, and what the
first measured sweep found — the demo bar at **100%**, RFC-001 §08's hybrid
argument confirmed with vector-only at **0%** on every menu-row category, and two
thresholds that turn out to be too low.

**The allergen and dietary red team** — issue
[#84](https://github.com/gganssle/chip_chat/issues/84). The seven ways somebody
gets a wrong answer out of a system about the one subject where a wrong answer is
a safety problem, and the third launch gate — the one PRD §10 makes blocking by a
different sentence from the two in §05.

```
chip_chat.eval.dietary
├── probes     the set, and what the honest turn owes each
├── run        the reply, the seam, and the two settlers
├── hand       a person's reading, and the day it expires
├── verdicts   four findings, and the refusal that has two ways
├── scoring    counts that must be zero, and one that must not be
├── coverage   #84's scope, as clauses the set meets or does not
├── report     the baseline, as Markdown
├── slice      the week-one loop, and what it cannot be asked
└── testing    targets broken one way each, and a free ceiling run
```

```bash
python -m chip_chat.eval.dietary --check                    # free
python -m chip_chat.eval.dietary --check --catalog <dir>    # and the premises
python -m chip_chat.eval.dietary --ceiling    # free, and the one to run
```

[`dietary/README.md`](dietary/README.md) is the write-up: the seven attacks, why
the manifest will not load without a question the corpus plainly answers, why
over-refusal is measured and not gated, and how a hand verdict expires. The
decision it measures is `docs/decisions/allergen-boundary.md`.

All five sets live beside their code — [`golden/`](golden/),
[`photos/`](photos/), [`adversarial/`](adversarial/), [`retrieval/`](retrieval/)
and [`dietary/`](dietary/) — each with a `README.md` to read before adding an
entry and a `BASELINE.md` for what has and has not been measured. Adding to the
first two changes the dataset's version, and `make dataset` is how the committed
build catches up; three of them are not in the dataset, for three reasons that
are the same reason — an attack has no expected output for an experiment to be
scored against, a retrieval label's expected output is a *passage* rather than a
response, and a probe's is a judgement about prose rather than a value a run
could be compared to.

[`trajectory/`](trajectory/) and [`grounding/`](grounding/) are on the same
terms and hold no set of their own: both score the dataset's rows, so there is
nothing in either to add an entry to.

**Experiments** — issue
[#73](https://github.com/gganssle/chip_chat/issues/73). The thing that turns *"I
tweaked the prompt and it feels better"* into a number. An experiment takes a
**configuration** -- a prompt revision, a model deployment, retrieval settings,
matcher thresholds -- from `eval/experiments/CONFIGURATIONS.json` and scores it
against the versioned dataset, per lane *and* per requirement, because an
aggregate that improved while a lane fell is a regression the aggregate calls an
improvement.

```
chip_chat.eval.experiment
├── configurations  the four axes, as data with a fingerprint
├── turns           one pass over the rows, read three ways
├── run             a configuration and a factory in, an experiment out
├── results         the flattened form two runs can be compared in
├── compare         what moved, per metric, per lane, per requirement
└── report          both documents, caveats above the numbers
```

```bash
python -m chip_chat.eval.experiment --check                  # free
python -m chip_chat.eval.experiment --ceiling --run shipped  # free, blind to the prompt
make experiment-compare                                      # two prompts, one dataset
```

[`experiments/README.md`](experiments/README.md) is the write-up: what an arm is,
why the prompt enters the fingerprint as a digest rather than as a name, why the
runner spends one model call per row rather than three, and what a comparison
under the routing oracle is and is not evidence of.

**Online evals and monitors** — issue
[#76](https://github.com/gganssle/chip_chat/issues/76), and the only set here
that scores questions nobody wrote down. There is no expected lane on a
stranger's question, so a monitor may fire only on something wrong **on its
face**: a claim with nothing retrieved, a photo match that resolved nothing and
escalated nothing, a refusal on a turn whose own retrieval answered it, an
identifier belonging to somebody else, a turn over its latency or cost ceiling.

```
chip_chat.eval.online
├── signals   one live turn, as the thing a monitor may look at
├── sampling  which turns get a judge, and the arithmetic behind the rate
├── monitors  the six fears, each as a condition that can fire
├── budget    judge tokens, inside the daily cap rather than beside it
├── run       sample, judge, monitor, alert -- and count what it cost
└── testing   each condition produced deliberately, so each monitor is demonstrated
```

```bash
python -m chip_chat.eval.online --check   # free, and non-zero while spend is unaccounted
python -m chip_chat.eval.online --drill   # free: every condition, produced
```

[`online/README.md`](online/README.md) is the write-up: why the rate is 20%,
which three classes ignore it, why four of the six monitors run on every turn
rather than on the sample, and what a drill is and is not evidence of.

**Promotion** — issue
[#77](https://github.com/gganssle/chip_chat/issues/77). The loop that makes going
public pay for itself: a production trace the monitors flagged, into a golden-set
entry, in under two minutes. Everything the trace can supply is derived;
everything that is a judgement -- which requirements, what has to be observed,
why the case is worth having -- is left as `TODO` and refused if it stays.

```
chip_chat.eval.promote
├── candidates  a flagged turn, and the draft it becomes
├── ledger      where every entry came from, and which sources are permanent
└── apply       validation, the append, and the provenance row -- in that order
```

```bash
python -m chip_chat.eval.promote --check                         # free, and in CI
python -m chip_chat.eval.promote --drafts capture.json > cases.json
python -m chip_chat.eval.promote --apply cases.json && make dataset
```

**Measured, because the criterion is a stopwatch.** The two commands cost
**0.7 seconds of machine time** between them on the committed set -- 0.4s to
draft, 0.4s to validate and append -- which leaves the whole of the two minutes
for the only part that needs a person: deciding which requirements the case
covers, what has to be observed for it to count as passed, and why it is worth
having. That is the split the design is built around, and it is the reason the
draft refuses to guess the expected tool.

Provenance lives in `eval/dataset/PROVENANCE.json`, **beside** the dataset and
never inside it: a `provenance` column on a dataset entry would rebase every
existing digest and move the version for a reason that has nothing to do with the
rows. The same file records the **permanent** sources -- the adversarial suite's
twenty-eight attacks, run by `make adversarial-redteam`, which CI blocks on -- and
`promote-check` fails when an attack is added to the manifest without being
recorded there. That is #77's *each attack you survive becomes a permanent eval*
with a check behind it rather than a promise in a document.

## Which lanes a run had, and why every document says so

Every number in this directory is a number about a *configuration*, and the axis
that moves it furthest is not the prompt or the model. It is which of the five
lanes were wired when the run happened.

`chip_chat.agent.lanes.CONDITIONAL_TOOLS` withholds `ask_account_question`,
`get_recommendations` and `match_meal_from_photo` from a deployment that cannot
answer them — #64's argument, and the right one: *a tool definition the model
can see and nothing can answer is worse than an absent one.* The consequence for
an eval is that an unwired run scores those lanes' rows at zero **because the
tool the row expects does not exist in the process doing the scoring**. That is
not a model failure and it is not a lane outage. It is a different measurement.

On 27 August 2026 it was an invisible one. `make experiment-baseline` was re-run
after the account and personalization lanes were wired onto the deployment and
after the chat deployment's capacity went from 10,000 tokens a minute to
200,000, and it came back byte-identical: 14.7% task completion, 42.9% tool
selection. Both were the correct answer to the question the harness had asked,
which was *how does the unwired slice score* — every entry point took
`NO_LANES` and none of them could be told otherwise. `docs/launch-readiness.md`
had been holding a launch target against it.

So `chip_chat.eval.wiring` carries both halves of the fix, and all four runners
that can execute a deployment take `--lanes`:

```bash
python -m chip_chat.eval.golden      --lanes wired --out eval/golden/BASELINE.md
python -m chip_chat.eval.trajectory  --lanes wired --out eval/trajectory/BASELINE.md
python -m chip_chat.eval.grounding   --lanes wired --judge
python -m chip_chat.eval.experiment  --lanes wired --run shipped --judge
```

Three properties, and each one is a way this could have gone wrong instead:

**`none` is the default and it stays the default.** `make ci` is free, offline
and credential-free, which is a rule here rather than an oversight — a gate that
needs a logged-in human is not a gate. Every free target above runs unwired.

**`wired` builds what the deployment builds, or refuses.** It calls
`chip_chat.api.connect.snowflake_connect` and `chip_chat.api.app.build_lanes` —
the deployment's own two functions, not a copy — reads the roster through the
pool's one unbound checkout, and binds every case's session to the rank-one
`regular` fixture through a session store, because `VisitorPool` will not take a
`demo_id` from a caller and this harness does not get an exception. Without a
Snowflake credential it raises rather than falling back, since a silent fall back
to the unwired slice is exactly the failure above with a heading that denies it.

**Every result says which.** The lane configuration is part of the deployment
name, so it lands in the *Deployment*, *Traces from* and *Answered by* lines of
the four baselines; a recorded experiment result carries it in its own column;
and a comparison whose sides do not both state it is **refused rather than
drawn**. That last one is the discipline `eval/retrieval` already keeps for an
arm whose vector half dropped. A stated difference is something a reader can
weigh. An unstated one produces a delta that looks exactly like a better model
and may be a lane coming up, and on 27 August that difference was the entire
content of a baseline nobody could read.

`eval/experiments/BASELINE.md` is the wired run and
`eval/experiments/BASELINE-NO-LANES.md` is the unwired one, kept beside it rather
than overwritten. `make experiment-wiring` subtracts them.

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

`eval/trajectory` sits across the golden set rather than beside it. It runs the
same rows and asks a different question of the same turns: not *was the answer
right* but *how did the turn get there*, read off the `tool.<tool_name>` spans
RFC-001 §09 froze. So the golden set can say a turn reached the expected tool,
and only the trajectory eval can say it reached three when one would do, or that
the query it sent bore no relation to what was asked. It scores the dataset's
routing rows and no photographs, for the same reason the golden set holds one
vision case: a frame the photo set runs directly was never routed to.

`eval/grounding` sits across the golden set the same way, and is divided from
`eval/trajectory` by *which spans it reads*. The trajectory eval reads
`tool.<tool_name>` and asks which lane the turn entered; this one reads
`retriever.search` and asks what came back out of it, then holds the response to
that. So the trajectory eval can say a turn reached `search_menu_knowledge`, and
only this one can say the search returned nothing and the turn answered anyway.
They share a span type deliberately — one adapter between a recording and a
reader, not two — and nothing else.

The golden set already carries `cites` and `grounded` as checks, and that is not
a duplication either. There they are *checks on a case*, reported unscored
because no deployment can settle them; here they are the register: `grounded`
means the published data answers this row, `declines` means it does not, and the
pair is what makes over-refusal and under-refusal two different findings rather
than one. The golden set says whether a case passed. This says which way it was
wrong, in a category where the direction is the whole point.
`eval/retrieval` sits **below** all of them rather than beside any, and the one
it is nearest to is `eval/grounding` — near enough to be worth saying where they
part. Both are about what the retriever returned. Grounding reads the
`retriever.search` span and asks whether *the answer the model wrote* is attached
to what came back; this asks whether what came back was the right thing in the
first place, with no model in the loop to write anything. A turn that retrieved
three wrong passages and answered faithfully from them is perfectly grounded and
completely wrong, and grounding is structurally blind to that by design: it holds
the response to the evidence, and cannot hold the evidence to the question.

So it runs the retriever directly — no agent, no lane, no model — and every
number in it is a property of the index, the chunking and the query construction.
Nothing here can be fixed or broken by a prompt. That is not a smaller version of
the golden set's knowledge cases either; it is the half of them every whole-turn
set is blind to. Task completion, groundedness and a human reading the transcript
all report success on a fluent, cited answer drawn from the wrong three passages.
The failure is invisible from up there and trivially visible from down here.

So the golden set keeps six knowledge cases for *routing and answer shape* and
delegates *did retrieval find it* here — twelve of the retrieval set's questions
carry the golden case they came from, and `retrieval.coverage` requires that they
do. It is the same division `eval/photos` draws in the vision lane, one layer
further down: run the lane directly, score the thing the whole-turn set cannot
see.

`eval/dietary` is divided from `eval/grounding` by *which questions*, not by
which arithmetic. It inherits the arithmetic wholesale — counts rather than a
rate, over-refusal measured beside under-refusal and deliberately not gated —
because the two evals score the same property and a model tuned to pass one has
to pass the other. What it does not inherit is the questions. The grounding
eval's rows come from the golden set, which is thirty-four questions chosen to
cover the PRD: none of them is phrased by somebody frightened, none invites a
derivation, and none asks for advice. Promoting fifteen red-team phrasings into
the golden set would have changed what the golden set is, and moved the dataset's
version for a reason that has nothing to do with the dataset. So the grounding
eval scores the product's ordinary answers on this subject and the red team
attacks the boundary directly, and the pair is read together.

`eval/adversarial` is divided from both on a different axis. It does not ask
*what is the right answer* — it asks *what does it take to get a wrong one*, and
a question with a right answer cannot be evidence about that. So the golden set
holds one plainly-phrased A3 case for the ordinary path and delegates every other
phrasing, plus the concurrency case, to the suite; `DELEGATIONS` names A3 and S2
as measured there, and `adversarial.coverage` checks that both really are. The
delegation is a promise in one direction and a test in the other.

It is divided from `eval/dietary` on the same axis, in the other direction.
A probe there *has* a right answer — report what is published, cite it, decline
the rest — so scoring one as an attack would mean scoring the correct behaviour
as a breach that failed to land. The suite keeps its `invention` family and its
one allergen attack, and no gate is computed over that family, deliberately: PRD
§05 makes two things pass-or-fail and invention is not one of them. The third
gate is PRD §10's, and `eval/dietary` is where it is counted.

The inversion is worth stating once. In the golden set an unmeasured check is
neutral: *how well did it do* survives being partly unmeasured. In the
adversarial suite an unmeasured attack **blocks its gate**, because *did anything
get out* does not — a suite that measures nothing and a product that is safe
produce the same document, and only one of them should be allowed to say `pass`.

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
