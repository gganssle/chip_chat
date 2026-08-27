"""Fixtures for driving the scorer, and the one run that is free and honest.

Two things live here, and they answer different questions.

:func:`ceiling` runs the dataset's routing rows through the week-one slice with
lane selection **handed to it** --
:class:`~chip_chat.eval.golden.testing.RoutingOracle` calls, for each message,
exactly the tool that row expects. What comes out is not a score for the agent,
for the reason that module states at length: nothing about model quality
survives a model that was told the answer.

What it *is* is the plumbing at its ceiling, read through the trace rather than
through the loop's own messages. Give a deployment perfect lane selection and
this is the trajectory eval's number anyway -- and every shape left in it is a
property of the wiring: a tool that is not registered cannot be routed to, so it
comes back ``no_tool`` however good the model is. Those failures are
reproducible for free, they need no credentials, and they are the ones worth
fixing before spending money on a real run.

:func:`turn_spans` and :class:`ScriptedSource` are the other thing: span trees
built by hand, so the shapes and the per-lane arithmetic can be driven against
outcomes computed on paper. A tree of invented spans measures the invention and
nothing else -- which is fine, because what is under test there is the classifier
rather than a deployment.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from openinference.semconv.trace import SpanAttributes

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.testing import RoutingOracle
from chip_chat.eval.trajectory.expectations import Expectation, expectations
from chip_chat.eval.trajectory.run import run_trajectories
from chip_chat.eval.trajectory.slice import SliceTraceSource
from chip_chat.eval.trajectory.trees import TraceSpan, Trajectory, read_trajectory
from chip_chat.otel.schema import SpanName, ToolName, tool_span_name

__all__ = [
    "CEILING_CAVEAT",
    "CEILING_SOURCE",
    "ScriptedSource",
    "ceiling",
    "turn_spans",
]

CEILING_SOURCE: Final = "week-one slice, routing handed to it (routing-oracle)"
"""What the ceiling run calls itself, so a report from one is obviously from one."""

CEILING_CAVEAT: Final = (
    "**This is not a score for the agent.** Lane selection was handed to the "
    "deployment: `RoutingOracle` calls, for each message, exactly the tool the "
    "row expects. Nothing about model quality survives a model that was told "
    "the answer.\n"
    "\n"
    "What the number below measures is the plumbing at its ceiling -- give a "
    "deployment perfect routing and this is what the trajectory eval gets out "
    "of it anyway. Every shape left in it is a property of the wiring: the "
    "week-one slice registers six of the eleven tools, and a tool that is not "
    "registered cannot be routed to, so it comes back `no_tool` however good "
    "the model is. A span tree cannot tell that apart from a model that chose "
    "not to call, which is why this paragraph exists."
)
"""What a ceiling run's numbers are worth, printed above them.

Not optional and not decoration. A reader who arrives at a table headed
*tool-selection accuracy* without this will read a fixture's ceiling as the
agent's accuracy, and the number is low enough that they will act on it.
"""

_TRACE: Final = "1"
_OTHER_TRACE: Final = "2"


def ceiling(
    golden: GoldenSet, dataset: Dataset, *, only: Sequence[str] | None = None
) -> tuple[Trajectory, ...]:
    """Run the dataset's routing rows against the slice, with routing handed to it.

    Args:
        golden: The set the dataset was promoted from. The oracle reads it, and
            the source looks each row's case back up in it.
        dataset: The built dataset, whose rows are the register.
        only: Entry ids to run. ``None`` runs all of them.

    Returns:
        One trajectory per row run. What fails here fails for a reason no model
        could have fixed -- see the module docstring.
    """
    source = SliceTraceSource(golden=golden, model=RoutingOracle(golden))
    return run_trajectories(expectations(dataset), source, only=only)


def turn_spans(
    calls: Sequence[tuple[ToolName, Mapping[str, Any]]],
    *,
    steps: int = 1,
    split: bool = False,
) -> tuple[TraceSpan, ...]:
    """Build one turn's spans by hand, in the shape RFC-001 section 09 fixes.

    Args:
        calls: The tool calls the turn made, in order, each with the arguments
            its span recorded.
        steps: How many ``agent.step`` spans to spread the calls across. Calls
            are dealt round-robin, so ``steps=2`` over three calls puts two in
            the first step and one in the second.
        split: Emit the agent half under a second trace id and with no parent,
            which is what a turn looks like when trace context did not
            propagate across the app-to-agent boundary. Issue #103.

    Returns:
        The spans, parents before children.
    """
    across = max(steps, 1)
    spans = [
        TraceSpan(
            name=SpanName.CHAT_TURN.value,
            span_id="turn",
            parent_id=None,
            trace_id=_TRACE,
            started=0,
            service="chip-chat-api",
        )
    ]
    agent_trace = _OTHER_TRACE if split else _TRACE
    for index in range(across):
        spans.append(
            TraceSpan(
                name=SpanName.AGENT_STEP.value,
                span_id=f"step-{index}",
                parent_id=None if split else "turn",
                trace_id=agent_trace,
                started=1 + index,
                service="chip-chat-agent",
            )
        )
    for order, (tool, arguments) in enumerate(calls):
        spans.append(
            TraceSpan(
                name=tool_span_name(tool),
                span_id=f"tool-{order}",
                parent_id=f"step-{order % across}",
                trace_id=agent_trace,
                attributes={SpanAttributes.TOOL_PARAMETERS: _json(arguments)},
                started=100 + order,
                service="chip-chat-agent",
            )
        )
    return tuple(spans)


@dataclass(frozen=True, slots=True)
class ScriptedSource:
    """A trace source that answers from a script, for driving the scorer.

    Attributes:
        script: Entry id to the spans that row's turn emitted. A row absent
            from it comes back with no spans at all, which is how *the source
            had nothing for this row* is exercised.
        name: What the report calls it. Says what it is, because a fixture that
            named itself after a deployment would put a measurement of a script
            in a document about a model.
    """

    script: Mapping[str, Sequence[TraceSpan]] = field(default_factory=dict)
    name: str = "scripted trace source (a fixture, not a deployment)"

    def trajectory(self, expectation: Expectation) -> Trajectory:
        """Read the scripted spans for one row."""
        return read_trajectory(
            expectation.entry_id, tuple(self.script.get(expectation.entry_id, ()))
        )


def _json(arguments: Mapping[str, Any]) -> str:
    """Arguments as the JSON string ``tool_call`` puts on the span."""
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))
