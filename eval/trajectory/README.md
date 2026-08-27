# `trajectory` — did it pick the right lane?

Issue [#74](https://github.com/gganssle/chip_chat/issues/74). The five-lane
architecture exists to get one number right, and this is where that number is
computed: **tool-selection accuracy, target ≥ 95%**, over the span trees a turn
emits, broken down by lane and by the shape of the failure.

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
python -m chip_chat.eval.trajectory --ceiling --out eval/trajectory/BASELINE.md
```

## Why this reads spans and the golden set does not

`eval/golden` scores whole turns and reads the tools off the conversation the
loop sent. That is the right reading for *what did this deployment do* and it is
unavailable the moment the agent is on the other side of a process boundary,
which decision D8 made it. Every dashboard, every monitor and every online eval
reads the trace instead.

So this package reconstructs the trajectory from `tool.<tool_name>` spans — the
names RFC-001 §09 fixed and issue #14 shipped before any agent existed. That is
what those names were frozen *for*. Rename one and this eval stops seeing it,
loudly, which is the whole argument for a span schema.

`eval/tests/test_trajectory_ceiling.py` holds the two readings to each other:
the spans and the loop's messages have to agree about what was called, or one of
the numbers in `eval/` is about something other than the turn.

## The dependency on #103, and what happens when it is not met

A turn crosses from `chip-chat-api` to `chip-chat-agent`. If W3C trace context
does not propagate across that boundary, the turn arrives as **two unrelated
traces**: a `chat.turn` with the guards under it, and an orphan `agent.step`
carrying every tool call.

Nothing about that looks like an error. Every tool span is present, so a reader
that simply collected them would produce a confident number over half a tree.

`Trajectory` therefore carries its trace ids, a split turn is **unscored**
rather than failed, `TrajectoryScores.split_traces` counts them, and the report
prints that count above the rates with the issue named. A run exits non-zero on
a split trace and on nothing else. Check the propagation itself with
`make trace-boundary`; the in-process ceiling cannot, because a turn that never
crossed a boundary cannot show that context survives one.

## The four shapes

They are counted apart because they have different owners.

| Shape | What it is | Who fixes it |
| --- | --- | --- |
| `wrong_lane` | Chose, and chose the other thing | The tool descriptions in `agent/surface.py` |
| `no_tool` | Answered from what the model already knew | A prompt, or a tool nobody registered |
| `extra_tools` | Got there, and paid for calls the turn did not need | Cost, not correctness |
| `wrong_query` | Right lane, wrong ask | The lane's own prompt |

One aggregate would send all four to the same person.

**Precedence.** A turn can be wrong several ways at once and gets one shape:
unreadable, then no tool, then wrong lane, then extra tools, then wrong query.
Each is asked only once the one above it is ruled out, so the counts partition
the set and read as *where the turns went* rather than as overlapping tallies.

**`no_tool` cannot see a tool that was not offered.** A model that chose not to
call and a deployment that never registered the tool produce the same span tree.
The ceiling run below is mostly the second, and says so.

**A sanctioned second call is not extra.** *"Get me my usual but add guac"*
legitimately reaches `get_usual_order` and then `propose_order`; scoring the
draft as waste would mark the correct trajectory wrong. `SANCTIONED` in
`expectations.py` is that table, it is total over the eleven tools, and its
chains run one way only — a read may be followed into the action lane's draft,
and the action lane may not reach back for a read. A companion the row *forbids*
is not sanctioned, because a case that names a tool as the wrong answer has said
something more specific than the table does.

## Why the wrong-query check is deliberately weak

`wrong_query` fires when the query shares **no** content word with what the
visitor said or with the menu terms the row leans on — and when the call passed
no query at all. That catches a query that drifted off the question entirely.

It does not catch the subtle paraphrase that quietly changes the ask: *"steak
allergens"* for *"is the steak safe for a severe soy allergy"* shares `steak`
and passes here. That one is a judgement about meaning. It belongs behind
`chip_chat.eval.golden.run.Judge` with `grounded` and `declines`, and
[#76](https://github.com/gganssle/chip_chat/issues/76)'s online evals are where
a model lands behind it. A keyword rule claiming to settle it would produce a
number measuring the keyword rule.

The check is also only observable on **two** of the eleven tools —
`search_menu_knowledge` and `ask_account_question` are the only ones that take
the ask as an argument. Everything else takes an id or a structure, where a
wrong value is a broken call rather than a badly-phrased question. The report
prints how many rows the check could see at all, so a `wrong_query: 0` is not
read as evidence that no query drifted.

## Two rates, and which one the target is on

`tool_selection` is the headline: the expected tool was reached and nothing the
row forbids was. That is the same reach-and-avoid rule
`chip_chat.eval.golden.scoring` applies, deliberately, so the two reports cannot
quote different numbers for the same metric. PRD §05's ≥ 95% is on this one.

`clean` is the stricter reading: the whole trajectory was right, extra calls and
drifted queries included. The gap between them is the cost and the sloppiness a
lane-selection rate is blind to by construction.

## Against live traffic

#74's fourth acceptance criterion. Here a row is known and its trace is fetched;
against production there is no row — a trace exists, and what it *should* have
done has to come from a judge on the turn's text.

The seam for that is not another method on `TraceSource`. It is
`scoring.score()`, which takes expectations and trajectories as two matched
sequences and has never been told where either came from. An online runner
assembles the pairs and calls it, so the shapes, the precedence and the per-lane
arithmetic are the same code — which is the only way the live number and the
dataset number mean the same thing. `trees.TraceSpan` is six fields and
`from_readable_spans` is one adapter; a backend fetch is a second one.

## What the ceiling run is worth

[`BASELINE.md`](BASELINE.md) is written by `--ceiling`, which runs the rows
through the week-one slice with lane selection **handed to it**. It costs
nothing and needs no credentials.

It is not a score for the agent. Nothing about model quality survives a model
that was told the answer. What it measures is the plumbing at its ceiling: give
a deployment perfect lane selection and this is the trajectory eval's number
anyway. Every shape left in it is a property of the wiring, and no amount of
prompt work will move it.
