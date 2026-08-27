"""Fixtures for driving the scorer, and the one run that is free and honest.

Three things live here, and they answer different questions.

:func:`ceiling` runs the dataset's rows through the week-one slice with lane
selection **handed to it** --
:class:`~chip_chat.eval.golden.testing.RoutingOracle` calls, for each message,
exactly the tool that row expects. What comes out is not a score for the agent,
for the reason that module states at length: nothing about model quality
survives a model that was told the answer.

What it *is* is the plumbing at its ceiling. Give a deployment perfect lane
selection and this is still what #75 gets out of it -- and here that is worth
more than it is to #74, because the finding it produces is a *fact about
retrieval*: a turn routed to ``search_menu_knowledge`` that came back with no
passages retrieved nothing to be grounded in, and no prompt fixes that.

:func:`turn_spans` and :class:`ScriptedSource` are span trees and responses
built by hand, so the findings and the per-category arithmetic can be driven
against outcomes computed on paper. A tree of invented spans measures the
invention -- which is right here, because what is under test is the scorer.

:class:`ScriptedJudge` is the third, and it needs the same warning the photo
set's describer carries. It answers from a script, so a groundedness number
computed over it measures the script. It exists to drive the two judged findings
through their paths -- and there is **no** version of it that substitutes for a
model. The day a real judge lands behind
:class:`~chip_chat.eval.grounding.run.Judge` it will be #76's, it will cost
tokens, and its verdicts will be about prose rather than about a dictionary.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from openinference.semconv.trace import DocumentAttributes, SpanAttributes

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.golden.testing import RoutingOracle
from chip_chat.eval.grounding.evidence import read_evidence
from chip_chat.eval.grounding.questions import Question, questions
from chip_chat.eval.grounding.run import Turn, run_turns
from chip_chat.eval.grounding.slice import SliceTurnSource
from chip_chat.eval.trajectory.trees import TraceSpan
from chip_chat.otel.schema import SpanName, ToolName, tool_span_name

__all__ = [
    "CEILING_CAVEAT",
    "CEILING_SOURCE",
    "ScriptedJudge",
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
    "**And two of the five findings are unmeasurable here whatever the model "
    "does.** Nothing in the request path builds a `ResponseEnvelope`, so no "
    "citation id and no claim class reaches a reply, so `cited` and `minted` "
    "come back unscored on every row -- a fact about wiring (bead `cc-bap`), "
    "not about an agent that never cites. The two judged findings need a judge "
    "and there is none; #76's online evals are where one lands.\n"
    "\n"
    "What the numbers below therefore measure is `supported`: whether a turn "
    "that made a claim had retrieved anything at all to make it from. That one "
    "needs no judge and no credentials, it is the floor under groundedness, "
    "and every failure in it is a property of the wiring that no prompt work "
    "can move."
)
"""What a ceiling run's numbers are worth, printed above them.

Not optional and not decoration. A reader who arrives at a table headed
*groundedness* without this will read a fixture's ceiling as the agent's
groundedness, and three of the five rows in that table are blank for reasons
that have nothing to do with a model.
"""

_TRACE: Final = "1"
_OTHER_TRACE: Final = "2"


def ceiling(
    golden: GoldenSet, dataset: Dataset, *, only: Sequence[str] | None = None
) -> tuple[Turn, ...]:
    """Run the dataset's rows against the slice, with routing handed to it.

    Args:
        golden: The set the dataset was promoted from. The oracle reads it, and
            the source looks each row's case back up in it.
        dataset: The built dataset, whose rows are the register.
        only: Entry ids to run. ``None`` runs all of them.

    Returns:
        One turn per row run. What fails here fails for a reason no model could
        have fixed -- see the module docstring.
    """
    source = SliceTurnSource(golden=golden, model=RoutingOracle(golden))
    return run_turns(questions(dataset), source, only=only)


def turn_spans(
    passages: Sequence[Mapping[str, Any]] = (),
    *,
    searches: int = 1,
    declined: bool = False,
    split: bool = False,
) -> tuple[TraceSpan, ...]:
    """Build one turn's spans by hand, in the shape RFC-001 section 09 fixes.

    Args:
        passages: The documents the retrieval returned, each an object with an
            ``id`` and optionally ``content``, ``score`` and ``metadata``.
            Dealt round-robin across the searches.
        searches: How many ``retriever.search`` spans to open. Zero is the
            turn that answered without looking anything up.
        declined: Mark every search as the RFC-001 section 10 outage path --
            the knowledge lane unavailable, which is not the corpus being empty.
        split: Emit the agent half under a second trace id and with no parent,
            which is what a turn looks like when trace context did not
            propagate across the app-to-agent boundary. Issue #103.

    Returns:
        The spans, parents before children.
    """
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
    if searches:
        spans.append(
            TraceSpan(
                name=SpanName.AGENT_STEP.value,
                span_id="step-0",
                parent_id=None if split else "turn",
                trace_id=agent_trace,
                started=1,
                service="chip-chat-agent",
            )
        )
    dealt: list[list[Mapping[str, Any]]] = [[] for _ in range(max(searches, 0))]
    for index, passage in enumerate(passages):
        if not dealt:
            break
        dealt[index % len(dealt)].append(passage)
    for index in range(max(searches, 0)):
        spans.append(
            TraceSpan(
                name=tool_span_name(ToolName.SEARCH_MENU_KNOWLEDGE),
                span_id=f"tool-{index}",
                parent_id="step-0",
                trace_id=agent_trace,
                started=50 + index,
                service="chip-chat-agent",
            )
        )
        spans.append(
            TraceSpan(
                name=SpanName.RETRIEVER_SEARCH.value,
                span_id=f"search-{index}",
                parent_id=f"tool-{index}",
                trace_id=agent_trace,
                attributes=_search_attributes(dealt[index], declined=declined),
                started=100 + index,
                service="chip-chat-agent",
            )
        )
    return tuple(spans)


def _search_attributes(
    passages: Sequence[Mapping[str, Any]], *, declined: bool
) -> dict[str, Any]:
    """The attributes ``record_documents`` and ``set_metadata`` leave on a search."""
    attributes: dict[str, Any] = {
        SpanAttributes.METADATA: _json(
            {"index": "menu-current", "declined": True}
            if declined
            else {
                "index": "menu-current",
                "confidence": "grounded" if passages else "none",
            }
        )
    }
    for index, passage in enumerate(passages):
        base = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{index}"
        attributes[f"{base}.{DocumentAttributes.DOCUMENT_ID}"] = passage["id"]
        attributes[f"{base}.{DocumentAttributes.DOCUMENT_CONTENT}"] = passage.get(
            "content", ""
        )
        if passage.get("score") is not None:
            attributes[f"{base}.{DocumentAttributes.DOCUMENT_SCORE}"] = passage["score"]
        if passage.get("metadata"):
            attributes[f"{base}.{DocumentAttributes.DOCUMENT_METADATA}"] = _json(
                passage["metadata"]
            )
    return attributes


@dataclass(frozen=True, slots=True)
class ScriptedSource:
    """A turn source that answers from a script, for driving the scorer.

    Attributes:
        script: Entry id to the turn that row produced, minus its evidence --
            supply the spans in :attr:`spans` and they are read into it. A row
            absent from both comes back with no spans at all, which is how *the
            source had nothing for this row* is exercised.
        spans: Entry id to the spans that row's turn emitted.
        reports: What this source claims it can observe. Scripted, because what
            most of these tests are about is the difference between *unscored*
            and *failed*.
        name: What the report calls it. Says what it is, because a fixture that
            named itself after a deployment would put a measurement of a script
            in a document about a model.
    """

    script: Mapping[str, Turn] = field(default_factory=dict)
    spans: Mapping[str, Sequence[TraceSpan]] = field(default_factory=dict)
    reports: frozenset[Signal] = field(default_factory=lambda: frozenset(Signal))
    name: str = "scripted turn source (a fixture, not a deployment)"

    def turn(self, question: Question) -> Turn:
        """The scripted turn for one row, with its evidence read in."""
        scripted = self.script.get(question.entry_id)
        evidence = read_evidence(
            question.entry_id, tuple(self.spans.get(question.entry_id, ()))
        )
        if scripted is None:
            return Turn(
                entry_id=question.entry_id, evidence=evidence, reports=self.reports
            )
        return Turn(
            entry_id=scripted.entry_id or question.entry_id,
            reply=scripted.reply,
            citations=scripted.citations,
            claim_class=scripted.claim_class,
            dropped_citations=scripted.dropped_citations,
            evidence=scripted.evidence or evidence,
            reports=scripted.reports or self.reports,
            card=scripted.card,
            error=scripted.error,
        )


@dataclass(frozen=True, slots=True)
class ScriptedJudge:
    """A judge that answers from a script. See the module docstring's warning.

    Attributes:
        groundings: Entry id to whether the claims were supported. A row absent
            from it gets ``undecided``, which is what a real judge returning
            ``None`` looks like from here.
        refusals: Entry id to whether the reply declined. Same rule.
    """

    groundings: Mapping[str, bool] = field(default_factory=dict)
    refusals: Mapping[str, bool] = field(default_factory=dict)

    def grounded(self, question: Question, turn: Turn) -> bool | None:
        """Whether the claims were supported, according to the script."""
        return self.groundings.get(question.entry_id)

    def refused(self, question: Question, turn: Turn) -> bool | None:
        """Whether the reply declined, according to the script."""
        return self.refusals.get(question.entry_id)


def _json(value: object) -> str:
    """One value as the JSON string a span attribute carries."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
