"""The week-one slice, as something whose span tree can be read back.

:mod:`chip_chat.eval.golden.slice` runs a case and reports what it *observed* --
tools off the conversation the loop sent, a card, a receipt. This runs the same
case and reads the ``tool.<tool_name>`` **spans**, which is a different claim
about the same turn and the reason both exist.

The difference is worth stating once, because it is what #74 is for. The golden
set's adapter reads the loop's own messages: it is measuring what the loop did,
using the loop's own record of it. This one measures what the *trace* says the
loop did, which is what every dashboard, monitor and online eval will read, and
what a hosted agent behind an HTTP boundary will be scored on when there is no
in-process conversation to consult. A trajectory that cannot be reconstructed
from the spans is a trajectory nobody outside this process can score, whatever
the messages say.

**The dataset row and the golden case are the same thing under two names.** The
scorer's register comes from the dataset, because a version is what makes two
scores comparable; the slice's seam takes a
:class:`~chip_chat.eval.golden.cases.GoldenCase`, because a case carries the
confirmed-draft state that turns *"yes, place it"* into a turn that can be run
at all. :data:`~chip_chat.eval.dataset.entries.GOLDEN_PREFIX` is the join, and
looking the case up beats reconstructing it: an unflattening would be a second
copy of the promotion in #72, free to disagree with the first.
"""

from dataclasses import dataclass

from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.model import ChatModel
from chip_chat.agent.prompt import SystemPrompt
from chip_chat.eval.dataset.entries import GOLDEN_PREFIX
from chip_chat.eval.golden.cases import GoldenCase, GoldenSet
from chip_chat.eval.golden.run import DEFAULT_SESSION
from chip_chat.eval.golden.slice import SliceDeployment
from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.trees import (
    Trajectory,
    from_readable_spans,
    read_trajectory,
)
from chip_chat.otel.testing import span_recorder

__all__ = ["RECORDER_COMPONENT", "SliceTraceSource"]

RECORDER_COMPONENT = "eval"
"""What the recording's ``service.name`` is built from.

One service name, not two: this is one process, so the app-and-agent split that
:func:`~chip_chat.otel.service.turn_service_names` describes does not exist here.
That is the sense in which a green run against this source does **not** discharge
the #103 dependency -- a turn that never crossed a process boundary cannot show
that trace context survives one. ``make trace-boundary`` is that check, and
:attr:`~chip_chat.eval.trajectory.scoring.TrajectoryScores.split_traces` is where
a failure of it would surface once the hosted agent is what answers.
"""


@dataclass(frozen=True, slots=True)
class SliceTraceSource:
    """The in-process agent loop, recorded as spans.

    Attributes:
        golden: The set the dataset was promoted from, for looking a row's case
            back up. See the module docstring.
        model: The chat model to run the loop against. A real deployment
            produces a real number; a scripted double produces a measurement of
            the script -- :mod:`chip_chat.eval.trajectory.testing` says which of
            those it is.
        lanes: The backing services the slice runs against. Without the photo
            lane the slice is never offered ``match_meal_from_photo``, and the
            vision row's trajectory is honestly empty rather than wrong; the
            account and personalization lanes withdraw two more tools the same
            way.
        session_prefix: What each row's session id is built from.
        prompt: The system prompt revision to run under, passed straight
            through to :class:`~chip_chat.eval.golden.slice.SliceDeployment`.
            ``None`` is the revision the agent ships with. This is what an
            experiment varies; see :mod:`chip_chat.eval.experiment`.
    """

    golden: GoldenSet
    model: ChatModel
    lanes: Lanes = NO_LANES
    session_prefix: str = DEFAULT_SESSION
    prompt: SystemPrompt | None = None

    @property
    def name(self) -> str:
        """The source, as the report names it."""
        return f"week-one slice on {self.model.deployment}, read from spans"

    def trajectory(self, expectation: Expectation) -> Trajectory:
        """Run one row and read its span tree.

        Args:
            expectation: The dataset row.

        Returns:
            The trajectory, or one carrying an ``error`` where the row has no
            case in the set or the deployment refused the turn.
        """
        case = self._case(expectation)
        if case is None:
            return Trajectory(
                entry_id=expectation.entry_id,
                error=f"no case in {self.golden.source} for this row",
            )
        deployment = SliceDeployment(
            self.model,
            lanes=self.lanes,
            session_prefix=self.session_prefix,
            prompt=self.prompt,
        )
        with span_recorder(RECORDER_COMPONENT) as recorder:
            observation = deployment.turn(case)
            recorded = recorder.finished_spans()
        if observation.error is not None:
            # The deployment's own reason beats "no spans were recorded", which
            # is what the tree would otherwise say about a turn that never ran.
            return Trajectory(entry_id=expectation.entry_id, error=observation.error)
        return read_trajectory(expectation.entry_id, from_readable_spans(recorded))

    def _case(self, expectation: Expectation) -> GoldenCase | None:
        """The golden case a dataset row was promoted from, if it is in the set."""
        case_id = expectation.entry_id.removeprefix(GOLDEN_PREFIX)
        for case in self.golden:
            if case.case_id == case_id:
                return case
        return None
