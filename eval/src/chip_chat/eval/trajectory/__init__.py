"""Did it pick the right lane? The metric the whole architecture exists to get right.

Issue [#74](https://github.com/gganssle/chip_chat/issues/74) in one sentence:
score the **trajectory** -- which tool was reached for, in what order, and
whether that was the lane the dataset row expected -- over the span trees a turn
emits, and break the result down by lane and by failure shape. The system design
calls lane selection *the single question the entire architecture turns on*; PRD
section 05 sets it at ≥ 95%, the highest bar in its table.

Eight modules, and the order they run in is the order to read them:

================ ================================================= =================
Module           What it holds                                     Answers
================ ================================================= =================
``expectations`` The dataset's rows, and the two tables            what was owed
``trees``        A turn's spans, read back as calls                what happened
``shapes``       Four ways it was wrong, and the precedence        which kind
``scoring``      Per-lane accuracy, and the gap under the target   how bad
``run``          Every row through a source                        against what
``slice``        The week-one loop, recorded                       the first one
``coverage``     #74's scope, as clauses the rows meet or do not   is it askable
``report``       The baseline, as Markdown                         written down
================ ================================================= =================

.. code-block:: python

    dataset = build_dataset(GoldenSet.load(...), LabeledSet.load(...))
    rows = expectations(dataset)
    scores = score(rows, run_trajectories(rows, source))
    print(scores.tool_selection, scores.shapes)

Four decisions are worth having in view before adding a row, a shape or a
source. Each is written up where it lives; this is the map.

**It reads span names, and that is the whole point.** A trajectory is
reconstructed from ``tool.<tool_name>`` spans -- the names RFC-001 section 09
froze and #14 shipped before any agent existed. Everything else in the tree is
incidental to this eval; those names are its schema. The alternative, reading
the loop's own messages the way :mod:`chip_chat.eval.golden.slice` does, cannot
score a hosted agent across a process boundary, and a hosted agent is what
decision D8 chose.

**A trace that split is unscored, never failed.** The bead behind #74 says it
first: this depends on #103 being correct. The app and the agent emit under two
``service.name`` values, and without W3C propagation the turn arrives as two
unrelated traces with every tool span still present. A reader that collected
them anyway would report a number computed over half a tree.
:mod:`chip_chat.eval.trajectory.trees` refuses, ``--ceiling`` exits non-zero on
it, and it is the only thing that mode gates.

**The four shapes are counted apart because they have different owners.** Wrong
lane is a tool description. No tool is a groundedness risk, or a tool nobody
registered. Extra tools is a cost. Right lane, wrong query is an ask that did
not survive the call. One aggregate would send all four to the same person.

**The dataset is the register, so two scores can be compared.** Rows come from
#72's versioned dataset rather than from a manifest, and the report prints the
version. A score against "the set as it was that afternoon" is a number nobody
can reproduce, which is the failure #72 exists to prevent.

``eval/trajectory/README.md`` is the write-up: what each shape means, why the
query check is deliberately weak, and what a run against the routing oracle is
and is not worth.
"""

from chip_chat.eval.trajectory.coverage import (
    CLAUSES,
    RATE_NEEDS,
    Clause,
    Coverage,
    coverage,
)
from chip_chat.eval.trajectory.expectations import (
    QUERY_ARGUMENT,
    SANCTIONED,
    Expectation,
    ExpectationError,
    expectations,
)
from chip_chat.eval.trajectory.report import Report, build_report, render
from chip_chat.eval.trajectory.run import TraceSource, run_trajectories
from chip_chat.eval.trajectory.scoring import (
    TOOL_SELECTION_TARGET,
    LaneTrajectories,
    TrajectoryScores,
    score,
)
from chip_chat.eval.trajectory.shapes import (
    FAILURE_SHAPES,
    Judgement,
    Shape,
    classify,
)
from chip_chat.eval.trajectory.slice import SliceTraceSource
from chip_chat.eval.trajectory.trees import (
    ToolCall,
    TraceSpan,
    Trajectory,
    from_readable_spans,
    read_trajectory,
)

__all__ = [
    "CLAUSES",
    "FAILURE_SHAPES",
    "QUERY_ARGUMENT",
    "RATE_NEEDS",
    "SANCTIONED",
    "TOOL_SELECTION_TARGET",
    "Clause",
    "Coverage",
    "Expectation",
    "ExpectationError",
    "Judgement",
    "LaneTrajectories",
    "Report",
    "Shape",
    "SliceTraceSource",
    "ToolCall",
    "TraceSource",
    "TraceSpan",
    "Trajectory",
    "TrajectoryScores",
    "build_report",
    "classify",
    "coverage",
    "expectations",
    "from_readable_spans",
    "read_trajectory",
    "render",
    "run_trajectories",
    "score",
]
