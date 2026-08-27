"""A trace, on its way to becoming a dataset entry.

#77's first acceptance criterion is a stopwatch: *a trace can be promoted into a
dataset entry in under two minutes*. That number is the design constraint and not
a nice-to-have -- the ticket says so in the next line, *cheap enough to do in a
couple of minutes while looking at a trace, or it will not happen* -- and it
rules out most of the obvious designs. A promotion flow that asks a person to
compose a JSON object by hand takes ten minutes and gets used twice.

So the work is split by who can do it. **Everything derivable from the trace is
derived**: the message, the lane the turn actually took, the tool it actually
called, the persona, whether a draft had been confirmed, and the terms the
question leans on. **Everything that is a judgement stays a judgement**: which
PRD requirements this covers, what has to be observed for it to count as passed,
and why the case is worth having. A promotion that guessed the second set would
be manufacturing ground truth, which is the one thing a golden set may not
contain.

**The observed lane is a starting point, not the label.** This is the trap.
:attr:`Candidate.observed_tool` is what the agent *did*, and the interesting
traces are exactly the ones where what it did was wrong -- that is why a monitor
flagged them. A draft that wrote the observed tool into ``tool`` and stopped
would promote the bug as the expected behaviour and turn the golden set into a
record of what the product does rather than of what it should. So the draft
carries the observation under a different name, in a comment field a human has to
resolve, and :mod:`chip_chat.eval.promote.apply` refuses a draft that still
carries the placeholder.

**The selection is the monitor's.** #77 says *selection driven by the monitors --
anything an online eval scored badly is a candidate*, and
:func:`from_alerts` is that sentence: a candidate carries the alert that produced
it, the ledger records it, and *why is this case in the set* has an answer that
is not somebody's memory.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from chip_chat.eval.golden.lanes import Lane, lane_of
from chip_chat.eval.online.monitors import Alert
from chip_chat.eval.online.signals import LiveTurn
from chip_chat.otel.schema import ToolName

__all__ = ["NEEDS_A_HUMAN", "Candidate", "draft", "from_alerts", "observation"]

NEEDS_A_HUMAN: Final = "TODO"
"""What a draft carries where a person has to decide.

Deliberately loud and deliberately invalid. :func:`~chip_chat.eval.promote.apply.
apply_draft` refuses a draft still carrying it, so the failure mode is a refusal
rather than a case whose expected behaviour is whatever the agent happened to do.
"""

_ID_WORDS: Final = 6
"""Words of the message that go into a suggested case id. Enough to recognise,
short enough to type."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One production turn somebody thought was worth keeping.

    Attributes:
        trace_id: Where it came from. The ledger's join back to the trace.
        message: What the visitor said.
        reply: What came back. Carried for the person doing the labelling and
            never written into the case -- a golden case holds a question and a
            standard, never a transcript of one answer.
        observed_tool: The tool the turn actually reached for, or empty. See the
            module docstring on why this is not the label.
        observed_lane: The lane that tool is in.
        persona: The archetype the turn ran under.
        confirmed: Whether a confirmed draft was on the desk.
        menu_terms: Published terms the message leans on, where the retrieval
            named them.
        monitors: The alerts that selected this trace, by monitor name.
        why_selected: One line about what the monitor saw.
    """

    trace_id: str
    message: str
    reply: str = ""
    observed_tool: str = ""
    observed_lane: Lane = Lane.NONE
    persona: str = ""
    confirmed: bool = False
    menu_terms: tuple[str, ...] = ()
    monitors: tuple[str, ...] = ()
    why_selected: str = ""

    @property
    def suggested_id(self) -> str:
        """A case id built from the message, for a person to accept or replace.

        Prefixed ``live-`` so that where an entry came from is legible in every
        report that prints an id, without a provenance column that would change
        every existing entry's digest and therefore the dataset's version.
        """
        words = [
            "".join(character for character in word if character.isalnum())
            for word in self.message.lower().split()
        ]
        stem = "-".join(word for word in words if word)[:60].strip("-")
        return f"live-{stem or self.trace_id[:8]}"


def from_alerts(turn: LiveTurn, alerts: Sequence[Alert]) -> Candidate:
    """Build a candidate from a live turn the monitors flagged.

    Args:
        turn: The turn, as :mod:`chip_chat.eval.online.signals` read it.
        alerts: What fired on it. Empty is allowed -- a person browsing traces
            may promote one nothing fired on, and *"I was reading it and it was
            wrong"* is a legitimate selection that the ledger should record as
            such rather than dress up as a monitor.

    Returns:
        The candidate.
    """
    calls = () if turn.trajectory is None else turn.trajectory.calls
    tool = calls[0].tool.value if calls else ""
    return Candidate(
        trace_id=turn.trace_id,
        message=turn.message,
        reply=turn.reply,
        observed_tool=tool,
        observed_lane=_lane(tool),
        persona=turn.persona_id,
        menu_terms=(),
        monitors=tuple(dict.fromkeys(alert.monitor for alert in alerts)),
        why_selected="; ".join(alert.detail for alert in alerts),
    )


def draft(candidate: Candidate) -> dict[str, Any]:
    """A golden-case draft, with the derivable half filled in.

    Args:
        candidate: The trace.

    Returns:
        A mapping in ``eval/golden/cases.json``'s shape, with every field a
        person has to decide set to :data:`NEEDS_A_HUMAN`. The three that are
        left are the three that cannot be derived: which PRD requirements this
        covers, what has to be observed for it to count as passed, and why the
        case is worth having.

        ``tool`` is deliberately among them, even though the trace names one.
        See the module docstring: the interesting traces are the ones where what
        the agent did was wrong.
    """
    body: dict[str, Any] = {
        "id": candidate.suggested_id,
        "message": candidate.message,
        "tool": NEEDS_A_HUMAN,
        "lane": NEEDS_A_HUMAN,
        "requirements": [NEEDS_A_HUMAN],
        "checks": [],
        "why": NEEDS_A_HUMAN,
    }
    if candidate.persona:
        body["persona"] = candidate.persona
    if candidate.confirmed:
        body["confirmed"] = True
    if candidate.menu_terms:
        body["menu_terms"] = list(candidate.menu_terms)
    body["_observed"] = {
        "trace_id": candidate.trace_id,
        "tool": candidate.observed_tool or "nothing",
        "lane": candidate.observed_lane.value,
        "reply": candidate.reply,
        "monitors": list(candidate.monitors),
        "why_selected": candidate.why_selected,
    }
    return body


def observation(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``_observed`` block a draft carries, or an empty mapping.

    Underscore-prefixed because it is scaffolding rather than case data:
    :func:`~chip_chat.eval.promote.apply.apply_draft` strips it before the case
    reaches ``cases.json``, and moves what it says into the provenance ledger,
    where a fact about where a case came from belongs. Leaving it on the case
    would put provenance in the manifest that the dataset's digest is computed
    from, which would rebase every existing entry's digest the day the first
    trace was promoted.
    """
    found = body.get("_observed")
    return found if isinstance(found, dict) else {}


def _lane(tool: str) -> Lane:
    """The lane a tool name is in, or :attr:`Lane.NONE` for no tool."""
    try:
        return lane_of(ToolName(tool))
    except ValueError:
        return Lane.NONE
