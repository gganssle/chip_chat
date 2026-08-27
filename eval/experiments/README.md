# `eval/experiments` — the configurations, and what a comparison is worth

Issue [#73](https://github.com/gganssle/chip_chat/issues/73). The sentence this
directory exists to replace is *"I tweaked the system prompt and it feels
better."*

```bash
make experiment-check      # free: the arms, their fingerprints, and the dataset
make experiment-ceiling    # free: the harness at its ceiling, blind to the prompt
make experiment-compare    # two prompt versions, one dataset, comparison rendered
```

## What is in here

| File | What it is |
| --- | --- |
| `CONFIGURATIONS.json` | The arms. Everything an experiment varies, as data. |
| `prompts/` | Candidate prompt revisions that have not earned their way into the agent package. |
| `results/` | Recorded results, one JSON per arm. What a later comparison reads. |
| `captures/` | Span trees from a run, for [#77](https://github.com/gganssle/chip_chat/issues/77)'s promotion path. |
| `BASELINE.md` | The current build's result, and the baseline the launch criteria are checked against. |
| `COMPARISON.md` | The rendered comparison. #73's demo criterion. |

## An arm is four axes and an argument

#73 names the four: **system prompt version, model deployment, retrieval
settings, matcher thresholds**. Every one of them is a field in
`CONFIGURATIONS.json` and none of them is in the code — there is a test that
walks the harness's AST and fails if a deployment name or a prompt fragment
appears as a runtime string literal in it.

Every arm also carries a `why`, and the loader refuses one without it. An arm
with no argument is an arm nobody can decide the result of: when the numbers come
back within noise of each other, *"was that the change working or the change not
mattering"* is answerable only if somebody wrote down what they expected.

### Why the prompt enters the fingerprint as a digest

An arm has a `fingerprint` — twelve hex characters over its canonical form. The
prompt enters it as the **content digest** rather than as the revision string,
which is `chip_chat.agent.prompt`'s own argument applied one level up: a
hand-maintained version answers *which prompt ran* only while everyone remembers
to bump it, and the run that matters is exactly the run where somebody edited the
text and did not. Two results carrying the same configuration fingerprint and the
same dataset version scored the same thing, and nobody had to arrange it.

### Why deployments default to the environment

An arm that is *about* the model names one. An arm that is about the prompt leaves
both deployment fields empty, which means *whatever `CHIP_CHAT_FOUNDRY_*_DEPLOYMENT`
says* — because a deployment name written into this file would be a deployment
name in a second place, and `chip_chat.agent.foundry` exists precisely because one
place is already one too many. The recorded result carries the **resolved** name,
so a file never says *whatever was configured*.

## What a comparison under the routing oracle is, and is not

`make experiment-ceiling` is free and it is the one to run in CI. It is also the
one most likely to be misread, and the report says so above its own table.

The routing oracle answers each message with exactly the tool the golden case
expects. It reads the message and nothing else — **not the system prompt**. So two
arms differing only in their prompt produce byte-identical numbers under the
ceiling, and that document says *nothing read the change*, not *the change made no
difference*. The two look identical on a chart and mean opposite things, which is
why `prompt` appears in the result's `inert_axes` on every ceiling run.

The same applies one axis over. The week-one slice wires no knowledge lane and no
photo lane, so `retrieval` and `matcher` are recorded and inert on every run
against it. A flat line on an axis nothing applied is not evidence about that
axis.

## Why there are two breakdowns and not one

A **lane** is where the architecture is. A **requirement** is where the product is.
They partition the same rows differently — one case can cover two requirements,
one requirement can be covered by six cases — and the reason both tables exist is
in #73's own sentence: *rather than one aggregate number that hides a regression
in one lane behind an improvement in another*.

`Comparison.regressions` is assembled from the lane and requirement tables **as
well as** from the headline metrics, so a candidate that gains four points of task
completion while losing the account lane is reported as a regression. The
aggregate alone would have called it an improvement, and the aggregate alone is
what somebody looking at a chart sees.

### What counts as a regression

A **rate** has to move by at least 3% — the dataset is thirty-four rows and one
row is 2.9% of it, so a threshold below one row would report a regression every
time a single case flipped for a reason nobody can reproduce. A **count** has no
threshold at all: PRD §05 makes the gates zero, and one uncited claim more than
last time is one more than zero. A **requirement** has no threshold either,
because it is usually covered by one or two cases and a rate over two cases has
no resolution for a threshold to use.

## Why the runner is cheap

Three evals score an experiment — the golden set, the trajectory eval and the
grounding eval — and running their three runners in sequence would spend three
model calls per row to observe one turn three times. It is the same turn:
`eval/golden` reads the loop's own messages, `eval/trajectory` reads the
`tool.<tool_name>` spans, `eval/grounding` reads the `retriever.search` spans.

`chip_chat.eval.experiment.turns` runs each row **once**, inside one span
recorder, and hands the recording to the readers the three evals already use.
Thirty-four rows is thirty-four turns. That is the difference between a harness
somebody runs after every prompt edit and one they run before a demo, and #73
makes it an acceptance criterion for exactly that reason.

The scorers themselves are called unchanged. An experiment that computed its own
groundedness would be a second definition of the metric, free to disagree with the
one `eval/grounding/BASELINE.md` reports — and the first time the two documents
disagreed, nobody would know which was wrong.

## The arms that ship

**`shipped`** — the build as it stands, on whatever the environment says the chat
deployment is. This is the baseline PRD §12's launch criteria are checked against,
and `eval/experiments/results/shipped.json` is where it is recorded.

**`lean-lanes`** — the system prompt with the five-lane section compressed from
prose with worked examples into a table. Every other section is byte-identical to
`v1`, so a difference in lane selection is attributable to that one edit. This is
the ordinary change somebody makes and then says feels the same.

**`rerank-off`** — the shipped prompt with semantic reranking withdrawn. Recorded
and inert until a knowledge lane is wired, and the report says so rather than
leaving somebody to read a flat line as evidence that the reranker does not
matter.

## Adding an arm

1. Add an object to `CONFIGURATIONS.json`. It needs a `name` and a `why`;
   everything else has a default.
2. A candidate prompt goes in `prompts/system-<revision>.md` and the arm names
   `"prompt_directory": "eval/experiments/prompts"`. It stays out of the agent
   package until it has earned its way in.
3. `make experiment-check` — free, and it fails on an arm that contradicts itself
   before a single model call.
4. `make experiment-compare` when you want the number.
