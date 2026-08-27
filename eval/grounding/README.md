# `grounding` — is the answer attached to anything?

Issue [#75](https://github.com/gganssle/chip_chat/issues/75). Two of the
headline metrics, and the ones that make the allergen boundary real rather than
aspirational.

| Metric | Target |
| --- | --- |
| Groundedness of food and policy claims | ≥ 0.95 |
| Menu claims made without a citation | **0** |

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
python -m chip_chat.eval.grounding --ceiling --out eval/grounding/BASELINE.md
```

## Two metrics, two shapes, never averaged

Groundedness is a **rate** with a target of 0.95. A menu claim with no citation
is a **count** with a target of zero, and decision D9 is what made it one: a
citation is an id the retriever returned, so its absence is a fact about a
payload rather than an opinion about prose.

Putting them in the same average would produce a number nobody can act on, and
it would quietly make a launch gate negotiable — PRD §12 makes the citation gate
blocking, and a blocking gate rendered as 99.2% is a gate somebody argues about
at exactly the wrong moment. `scoring.py` keeps counts and rates in different
columns for the same reason `eval/golden` does.

## The five findings, and who owns each

| Finding | Kind | Who fixes it |
| --- | --- | --- |
| `cited` | rule | the response format, and whatever built it |
| `minted` | rule | the model, and D9 already stopped it reaching a visitor |
| `supported` | rule | retrieval, or the corpus |
| `grounded` | judgement | the prompt |
| `refusal` | judgement, both ways | the prompt, in opposite directions |

`supported` is the one worth explaining, because it is not in the ticket by that
name. A turn that made a food claim and whose `retriever.search` spans returned
**nothing** is ungrounded whatever a judge says about the prose — there is
nothing for the claim to be grounded *in*. It needs no judge and no credentials,
so it is the finding a free run actually produces, and it is the floor under the
number the judge will eventually compute. It is `eval/trajectory`'s `no_tool`
shape seen from the other side: *the prose reads fine and nothing in it is
attached to anything.*

## The evidence is what the turn had, not what the corpus holds

#75 is specific: *the retrieved documents are on the `retriever.search` span, so
the judge scores against what the system really had, not against the corpus in
general.* `evidence.py` is that sentence. A judge handed the corpus instead
would score a system that never opened it as perfectly grounded.

So where the retrieval cannot be read, `grounded` is **unscored** rather than
judged against something easier. That includes the case where the turn arrived
as two traces: the retrieval is then in one trace and the response in the other,
and nothing can show the passages belong to the answer. Issue
[#103](https://github.com/gganssle/chip_chat/issues/103), `make trace-boundary`,
and a run exits non-zero on it.

The span type is `eval/trajectory`'s. This package reads *different spans out of
the same tree*, so a second adapter would be a second thing to keep in step with
the SDK, free to disagree with the first about what a turn's trace said.

## Both directions of the refusal

Two distinct failures, and only one of them is visible to a scorer that counts
wrong answers:

- **under-refusal** — claiming something the corpus does not support;
- **over-refusal** — refusing where the corpus plainly had the answer.

Measuring only the first produces a system that hedges everything and scores
beautifully. So the register says, per row, which way the turn should have gone,
and it says it in the golden set's own vocabulary rather than a new one:
`grounded` on a case means the published data answers it, `declines` means it
does not, `cites` means the answer has to show what it read. Inventing a fourth
field would have put the same fact in two places, free to disagree.

One case says two of them at once, and it is the one the ticket is about.
`k3-allergen-safety-judgement` — *"will the steak be safe for my severe soy
allergy"* — owes a **refusal and a citation**: the published chart says which
items are marked for soy and does not say whether one is safe for a person, so
the honest turn declines and shows what it read. A refusal is not an excuse from
K2.

## The stricter bar is a count, not a bigger percentage

#75 asks for allergen and dietary questions to be *held to a stricter bar,
because that is where a confident wrong answer is a safety issue rather than an
accuracy issue.* The obvious reading is 0.98 instead of 0.95, and it is the
wrong one.

**A rate over allergen answers is a percentage of a safety property.** It says
how often the promise held, which is a sentence nobody would sign. So this
category is scored as counts that must be zero — uncited claims, minted
citations, claims with nothing retrieved, ungrounded claims, under-refusals —
and `GroundingScores.dietary_gate` is a boolean. The same move the adversarial
suite makes with its two gates, for the same reason.

**Over-refusal is measured and deliberately outside the gate.** A turn that
declined to guess about a soy allergy did the safe thing badly rather than the
unsafe thing. It is reported at the same size as the other direction, in the
same table, because ignoring it is how a hedging system passes; it is not gated,
because gating it would push in exactly the direction the ticket warns about.

### What puts a row in the category

A `dietary` flag on the golden case, declared. It is not derived, and the
alternatives were considered:

- **from the requirement ids** — K3 covers halal *and* cross-contact, K5 the two
  allergen ones, and *"what's vegetarian here"* is a K4 case and plainly a
  dietary question. No subset of the register draws the line.
- **from a word list** — *"are the black beans cooked in the same pot as the
  chicken"* is a cross-contact question containing no allergen word at all, and
  dropping it out of the category where a wrong answer costs the most is exactly
  the failure the category exists to prevent.

The word list survives in a smaller job. `cases.DIETARY_WORDS` runs in one
direction only: a case whose message asks about soy, dairy, gluten, halal or
vegetarian and is **not** marked is refused at load. Silence is not absence —
the same rule the photo set applies to a slot that is neither read nor named
unreadable.

Adding the flag moved the dataset's version, which is what
[#72](https://github.com/gganssle/chip_chat/issues/72)'s version is *for*: the
rows say something they did not say before, so a score taken against the old
version is a score against a different register and should not be silently
comparable. `make dataset` is how the committed build catches up.

## What is unmeasured today, and why that is the honest answer

Three of the five findings come back **unscored** on every run this repository
can make, and `BASELINE.md` says so above its own numbers.

`chip_chat.agent.envelope` — decision D9's response envelope, where a citation
is an id the retriever returned rather than a sentence the model wrote — exists,
is tested, and **is imported by no caller**. So no deployment here reports a
citation or a claim class, and no span carries one either. `cited` and `minted`
are therefore unscored, not failed: scoring them as failures would produce a
report saying the agent never cites its sources, which is a claim about wiring
dressed as a claim about a model. Bead `cc-bap` is that wiring, and the day it
lands both findings start producing numbers with no change here.

`grounded` and the refusal need a judge. Choosing one costs tokens and belongs to
[#76](https://github.com/gganssle/chip_chat/issues/76)'s online evals; what #75
owes is the two questions *named and scoreable*, so that a judge has something to
attach to. A keyword rule looking for *"I don't know"* would produce a number
measuring the keyword rule.

**A run does not go red about any of that.** A gate nobody measured has not
passed — the report prints `unmeasured`, which is neither pass nor fail — but a
build that failed because a wire is missing is a build somebody switches the
check off in. What a run *does* exit non-zero on is a measured gate breach and a
split trace.

## Against live traffic

#75's first acceptance criterion is both evals running against the dataset **and
against live traces**, and the seam for the second is not another adapter. It is
`scoring.score`, which takes questions and turns as two matched sequences and has
never been told where either came from. An online runner assembles the pairs out
of a backend — a trace gives the response and the `retriever.search` documents,
a judge supplies what the turn should have done — and calls the same function.
The findings, the gates and the per-category arithmetic are then the same code,
which is the only way the live number and the dataset number mean the same thing.

## What a ceiling run is worth

`--ceiling` runs the rows through the week-one slice with lane selection handed
to it. Nothing about model quality survives a model that was told the answer, so
it is **not a score for the agent** — `eval/trajectory/README.md` makes the
argument at length and it is the same argument.

What it is worth here is one number: `supported`. The slice's menu is three
hardcoded items with no published policy or rewards pages, so *"what happens if
my order is wrong when i pick it up"* reaches the knowledge lane and comes back
with nothing retrieved. That is a property of the corpus and the wiring which no
prompt work can move, it is reproducible for free, and it is the kind of thing
worth fixing before spending money on a real run.
