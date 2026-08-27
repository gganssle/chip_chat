"""One pass over the dataset, read three ways.

#73's fourth acceptance criterion is *the runner is cheap enough to run on a
whim, or it will not be run*, and that criterion is what this module is for.

The three evals #73 scores against -- the golden set, the trajectory eval and the
grounding eval -- each ship a runner that answers every row itself, and running
all three in sequence would spend three model calls per row to observe one turn
three times. It is the same turn. The golden set reads the loop's own messages,
:mod:`chip_chat.eval.trajectory` reads the ``tool.<tool_name>`` spans and
:mod:`chip_chat.eval.grounding` reads the ``retriever.search`` spans, and all
three readings are available from one execution if somebody records it once.

So this runs each case exactly once, inside one span recorder, and hands the
result to the three *readers* -- :func:`~chip_chat.eval.trajectory.trees.
read_trajectory` and :func:`~chip_chat.eval.grounding.evidence.read_evidence`,
the same functions the two single-purpose slices call. Nothing about how a
trajectory or a piece of evidence is *read* lives here; what lives here is the
decision to read both off one recording. An experiment over thirty-four rows is
thirty-four turns, which is the difference between a harness somebody runs after
every prompt edit and one they run before a demo.

**A source that cannot report a signal still cannot report it.** The three
readings do not add up to more than the deployment declared. The week-one slice
does not report citations -- ``chip_chat.agent.envelope`` is imported by no
caller, bead ``cc-bap`` -- and every :class:`~chip_chat.eval.grounding.run.Turn`
built here carries the deployment's own
:attr:`~chip_chat.eval.golden.run.Observation.reports` through unaltered, so the
citation rule comes back unscored exactly as it does on a single-purpose run.
Reading one turn three ways buys three views of what happened; it does not buy a
field the deployment never filled in.

**One row's failure is one row's failure.** Every case runs inside its own
``try`` and an adapter error becomes an error recorded against all three
readings, for the reason :func:`chip_chat.eval.golden.run._run_one` gives at
greater length: an outage is not a model being wrong, and a deployment that
refuses the eleventh row must not cost the other twenty-three.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from chip_chat.eval.dataset.entries import GOLDEN_PREFIX
from chip_chat.eval.golden.cases import GoldenCase, GoldenSet
from chip_chat.eval.golden.run import Deployment, Observation
from chip_chat.eval.grounding.evidence import read_evidence
from chip_chat.eval.grounding.run import Turn
from chip_chat.eval.trajectory.trees import (
    TraceSpan,
    Trajectory,
    from_readable_spans,
    read_trajectory,
)
from chip_chat.otel.testing import span_recorder

__all__ = ["RECORDER_COMPONENT", "Recorded", "record_rows"]

RECORDER_COMPONENT = "eval"
"""What the recording's ``service.name`` is built from.

One service name, not two, exactly as in the two single-purpose slices: this is
one process, so a green run here does **not** discharge #103's propagation
dependency. ``make trace-boundary`` is that check.
"""


@dataclass(frozen=True, slots=True)
class Recorded:
    """One case, run once, read three ways.

    Attributes:
        entry_id: The dataset row, which is the join key all three scorers use.
        observation: What the deployment reported. Scored by
            :func:`chip_chat.eval.golden.scoring.score`.
        trajectory: The ``tool.<tool_name>`` spans, read back as calls. Scored
            by :func:`chip_chat.eval.trajectory.scoring.score`.
        turn: The response and the ``retriever.search`` spans. Scored by
            :func:`chip_chat.eval.grounding.scoring.score`.
        spans: The recording itself, kept so a run can be written out as a
            capture. #77's promotion path reads captures, and the cheapest
            source of a real span tree is a run that already produced one --
            re-deriving one from the trajectory and the evidence would be a
            lossy reconstruction of something this function was holding.
    """

    entry_id: str
    observation: Observation
    trajectory: Trajectory
    turn: Turn
    spans: tuple[TraceSpan, ...] = ()


def record_rows(
    golden: GoldenSet,
    entry_ids: Sequence[str],
    deployment: Deployment,
    *,
    only: Sequence[str] | None = None,
) -> tuple[Recorded, ...]:
    """Run each row once and record all three readings of it.

    Args:
        golden: The set the dataset was promoted from. The rows are looked back
            up in it rather than unflattened, which is the join
            :data:`~chip_chat.eval.dataset.entries.GOLDEN_PREFIX` exists for --
            an unflattening would be a second copy of #72's promotion, free to
            disagree with the first.
        entry_ids: The dataset rows to run, in dataset order.
        deployment: What answers them.
        only: Entry ids to run, for iterating on one row. ``None`` runs all.

    Returns:
        One :class:`Recorded` per row run, in dataset order.
    """
    return tuple(_recorded(golden, entry_ids, deployment, only))


def _recorded(
    golden: GoldenSet,
    entry_ids: Sequence[str],
    deployment: Deployment,
    only: Sequence[str] | None,
) -> Iterator[Recorded]:
    wanted = None if only is None else set(only)
    by_id = {case.case_id: case for case in golden}
    for entry_id in entry_ids:
        if wanted is not None and entry_id not in wanted:
            continue
        case = by_id.get(entry_id.removeprefix(GOLDEN_PREFIX))
        if case is None:
            yield _failed(
                entry_id, f"no case in {golden.source} for this row", deployment
            )
            continue
        yield _run_one(entry_id, case, deployment)


def _run_one(entry_id: str, case: GoldenCase, deployment: Deployment) -> Recorded:
    """Run one case inside a recorder and read the recording three ways."""
    try:
        with span_recorder(RECORDER_COMPONENT) as recorder:
            observation = deployment.turn(case)
            spans = recorder.finished_spans()
    except Exception as error:  # a deployment is somebody else's code
        return _failed(entry_id, f"{type(error).__name__}: {error}", deployment)

    if observation.error is not None:
        # The deployment's own reason beats "no spans were recorded", which is
        # what both readers would otherwise say about a turn that never ran.
        return _failed(entry_id, observation.error, deployment)

    tree = from_readable_spans(spans)
    return Recorded(
        spans=tree,
        entry_id=entry_id,
        observation=observation,
        trajectory=read_trajectory(entry_id, tree),
        turn=Turn(
            entry_id=entry_id,
            reply=observation.reply,
            citations=observation.citations,
            claim_class=observation.claim_class or "",
            evidence=read_evidence(entry_id, tree),
            reports=observation.reports,
            card=observation.card,
        ),
    )


def _failed(entry_id: str, reason: str, deployment: Deployment) -> Recorded:
    """One row that produced nothing, recorded as nothing in all three readings."""
    return Recorded(
        entry_id=entry_id,
        observation=Observation(
            case_id=entry_id.removeprefix(GOLDEN_PREFIX),
            error=reason,
            reports=deployment.reports,
        ),
        trajectory=Trajectory(entry_id=entry_id, error=reason),
        turn=Turn(entry_id=entry_id, error=reason, reports=deployment.reports),
    )
