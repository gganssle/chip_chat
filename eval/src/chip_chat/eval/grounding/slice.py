"""The week-one slice, as something whose answer *and* whose retrieval can be read.

:mod:`chip_chat.eval.golden.slice` runs a case and reports what it observed.
:mod:`chip_chat.eval.trajectory.slice` runs the same case and reads the
``tool.<tool_name>`` spans. This one runs it and takes **both**: the response
off the deployment, and the passages off the ``retriever.search`` spans, because
those are the two halves #75 scores against each other.

**It declares what the slice can report and nothing more.**
:data:`~chip_chat.eval.golden.slice.SLICE_SIGNALS` does not hold
``CITATIONS``, and this source passes it straight through rather than filling in
an empty tuple that would read as *"it cited nothing"*. Nothing in the request
path builds a :class:`~chip_chat.agent.envelope.ResponseEnvelope` -- the module
exists, is tested, and is imported by no caller -- so the citation rule and the
minted-source count come back unscored on every row here. Bead ``cc-bap`` is
that wiring, and until it lands ``eval/grounding/BASELINE.md`` reports the
citation gate as unmeasured rather than as met.

**What it does produce is the floor.** The slice's
``search_menu_knowledge`` opens a real ``retriever.search`` and records real
documents, so *did this turn retrieve anything at all before it made a menu
claim* is answerable today, for free, with no judge and no credentials. That is
the number the ceiling run is for.

**The dataset row and the golden case are the same thing under two names.** The
register comes from the dataset, because a version is what makes two scores
comparable; the slice's seam takes a
:class:`~chip_chat.eval.golden.cases.GoldenCase`, because a case carries the
confirmed-draft state that turns *"yes, place it"* into a turn that can be run
at all. :data:`~chip_chat.eval.dataset.entries.GOLDEN_PREFIX` is the join --
the same move :mod:`chip_chat.eval.trajectory.slice` makes, and for the same
reason: an unflattening here would be a second copy of #72's promotion, free to
disagree with the first.
"""

from dataclasses import dataclass

from chip_chat.agent.model import ChatModel
from chip_chat.eval.dataset.entries import GOLDEN_PREFIX
from chip_chat.eval.golden.cases import GoldenCase, GoldenSet
from chip_chat.eval.golden.run import DEFAULT_SESSION, Signal
from chip_chat.eval.golden.slice import SLICE_SIGNALS, SliceDeployment
from chip_chat.eval.grounding.evidence import read_evidence
from chip_chat.eval.grounding.questions import Question
from chip_chat.eval.grounding.run import Turn
from chip_chat.eval.trajectory.trees import from_readable_spans
from chip_chat.otel.testing import span_recorder
from chip_chat.vision.lane import PhotoLane

__all__ = ["RECORDER_COMPONENT", "SliceTurnSource"]

RECORDER_COMPONENT = "eval"
"""What the recording's ``service.name`` is built from.

One service name, not two: this is one process, so the app-and-agent split does
not exist here and a green run against this source does **not** discharge the
#103 dependency. ``make trace-boundary`` is that check;
:attr:`~chip_chat.eval.grounding.scoring.GroundingScores.split_traces` is where
a failure of it would surface once a hosted agent is what answers.
"""


@dataclass(frozen=True, slots=True)
class SliceTurnSource:
    """The in-process agent loop, answered and recorded.

    Attributes:
        golden: The set the dataset was promoted from, for looking a row's case
            back up. See the module docstring.
        model: The chat model to run the loop against. A real deployment
            produces a real number; a scripted double produces a measurement of
            the script.
        lane: The photo lane, where one is wired.
        session_prefix: What each row's session id is built from.
    """

    golden: GoldenSet
    model: ChatModel
    lane: PhotoLane | None = None
    session_prefix: str = DEFAULT_SESSION

    @property
    def name(self) -> str:
        """The source, as the report names it."""
        return f"week-one slice on {self.model.deployment}, retrieval read from spans"

    @property
    def reports(self) -> frozenset[Signal]:
        """What the slice can observe. Citations are not among them; see cc-bap."""
        return SLICE_SIGNALS

    def turn(self, question: Question) -> Turn:
        """Run one row, and read its retrieval back off the trace.

        Args:
            question: The dataset row.

        Returns:
            The turn, or one carrying an ``error`` where the row has no case in
            the set or the deployment refused it.
        """
        case = self._case(question)
        if case is None:
            return Turn(
                entry_id=question.entry_id,
                error=f"no case in {self.golden.source} for this row",
                reports=self.reports,
            )
        deployment = SliceDeployment(
            self.model, lane=self.lane, session_prefix=self.session_prefix
        )
        with span_recorder(RECORDER_COMPONENT) as recorder:
            observation = deployment.turn(case)
            recorded = recorder.finished_spans()
        if observation.error is not None:
            # The deployment's own reason beats "no spans were recorded", which
            # is what the evidence would otherwise say about a turn that never
            # ran.
            return Turn(
                entry_id=question.entry_id,
                error=observation.error,
                reports=self.reports,
            )
        return Turn(
            entry_id=question.entry_id,
            reply=observation.reply,
            citations=observation.citations,
            claim_class=observation.claim_class or "",
            evidence=read_evidence(question.entry_id, from_readable_spans(recorded)),
            reports=observation.reports,
            card=observation.card,
        )

    def _case(self, question: Question) -> GoldenCase | None:
        """The golden case a dataset row was promoted from, if it is in the set."""
        case_id = question.entry_id.removeprefix(GOLDEN_PREFIX)
        for case in self.golden:
            if case.case_id == case_id:
                return case
        return None
