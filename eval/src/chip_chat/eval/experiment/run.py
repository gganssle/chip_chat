"""Running one configuration against the dataset, and scoring it three ways.

The runner takes a :class:`~chip_chat.eval.experiment.configurations.
ExperimentConfiguration` and a :class:`~chip_chat.eval.golden.run.Deployment`
factory, and returns an :class:`~chip_chat.eval.experiment.results.
ExperimentResult`. That is the whole of it, and the shape is the point: nothing
here reads an environment variable, chooses a model, or knows what a prompt says.
Everything the experiment is *about* arrives in the configuration, which is #73's
*nothing being experimented on should be hardcoded anywhere* stated as a
signature rather than as an intention.

**The factory, not the deployment.** A configuration names a prompt and a
deployment; turning those into something that answers a case needs a model
client, which needs credentials, which is exactly the dependency an experiment
harness must not have if it is going to be runnable from a laptop, from CI and
against a scripted double in a test. So the caller supplies
:class:`DeploymentFactory` -- one function from a configuration to a deployment
-- and :mod:`chip_chat.eval.experiment.__main__` is the only place in the package
that builds a real one.

**One pass, three scorers.** :func:`~chip_chat.eval.experiment.turns.record_rows`
runs each row once and reads it three ways;
:func:`chip_chat.eval.golden.scoring.score`,
:func:`chip_chat.eval.trajectory.scoring.score` and
:func:`chip_chat.eval.grounding.scoring.score` are then called on those readings
unchanged. Not re-implemented, not wrapped, not adjusted: an experiment that
computed its own groundedness would be a second definition of the metric, free
to disagree with the one ``eval/grounding/BASELINE.md`` reports, and the first
time the two documents disagreed nobody would know which was wrong.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.experiment.configurations import ExperimentConfiguration
from chip_chat.eval.experiment.results import ExperimentResult, build_result
from chip_chat.eval.experiment.turns import Recorded, record_rows
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.run import Deployment
from chip_chat.eval.golden.scoring import score as score_golden
from chip_chat.eval.grounding.questions import questions
from chip_chat.eval.grounding.run import Judge as GroundingJudge
from chip_chat.eval.grounding.scoring import score as score_grounding
from chip_chat.eval.trajectory.expectations import expectations
from chip_chat.eval.trajectory.scoring import score as score_trajectory

__all__ = ["DeploymentFactory", "Experiment", "run_experiment"]


class DeploymentFactory(Protocol):
    """Turns a configuration into something that can answer a case.

    One method wide, for the reason :class:`~chip_chat.eval.golden.run.
    Deployment` is two: the harness must be drivable by a test that has no
    credentials, and a factory is the smallest thing that lets the caller decide
    what a configuration *means* without the runner knowing.
    """

    def __call__(self, configuration: ExperimentConfiguration) -> Deployment:
        """Build the deployment this arm runs against.

        Args:
            configuration: The arm. Everything the deployment should honour --
                the prompt revision, the model deployment, the settings that
                reach a lane -- is on it.

        Returns:
            The deployment.
        """
        ...


@dataclass(frozen=True, slots=True)
class Experiment:
    """One run, before it is flattened.

    Kept because the report of a single run wants the failures, and the
    flattened :class:`~chip_chat.eval.experiment.results.ExperimentResult` does
    not carry them -- a recorded result is for comparing, and thirty-four
    failure paragraphs are for reading once.

    Attributes:
        configuration: The arm that ran, resolved.
        result: The flattened, recordable form.
        recorded: Every row, run once and read three ways.
    """

    configuration: ExperimentConfiguration
    result: ExperimentResult
    recorded: tuple[Recorded, ...]


def run_experiment(
    configuration: ExperimentConfiguration,
    golden: GoldenSet,
    dataset: Dataset,
    factory: DeploymentFactory,
    *,
    environment: Mapping[str, str] | None = None,
    judge: GroundingJudge | None = None,
    judge_name: str = "",
    judge_tokens: int = 0,
    knowledge_lane: bool = False,
    photo_lane: bool = False,
    prompt_read: bool = True,
    only: Sequence[str] | None = None,
    caveat: str = "",
    now: datetime | None = None,
) -> Experiment:
    """Score one configuration against the dataset.

    Args:
        configuration: The arm to run.
        golden: The set the dataset was promoted from, for looking a row's case
            back up.
        dataset: The built dataset. Its version is what makes two results
            comparable, which is why this takes a dataset rather than a
            manifest.
        factory: Builds the deployment from the configuration.
        environment: What to resolve the configuration's empty deployment
            fields against. ``None`` resolves against nothing, which is right
            for a test and wrong for a run.
        judge: Settles the two judged findings of #75. ``None`` leaves both
            unscored, and the report says so.
        judge_name: What to call that judge in the result.
        judge_tokens: What the judging cost, for the budget line.
        knowledge_lane: Whether a retriever is wired, which decides whether the
            retrieval axis was applied or merely recorded.
        photo_lane: The same for the matcher axis.
        prompt_read: Whether what answers the rows reads the system prompt at
            all. False under the routing oracle; see
            :meth:`~chip_chat.eval.experiment.configurations.
            ExperimentConfiguration.inert_axes`.
        only: Entry ids to run, for iterating on one row.
        caveat: What this run's numbers are worth, in prose.
        now: The clock, for a test that wants a fixed timestamp.

    Returns:
        The experiment.
    """
    resolved = configuration.resolve(environment or {})
    deployment = factory(resolved)
    rows = expectations(dataset)
    asked = questions(dataset)
    recorded = record_rows(golden, [row.entry_id for row in rows], deployment, only=only)
    by_id = {row.entry_id: row for row in recorded}

    golden_scores = score_golden(golden, [item.observation for item in recorded])
    trajectory_scores = score_trajectory(rows, [item.trajectory for item in recorded])
    grounding_scores = score_grounding(
        asked,
        [by_id[q.entry_id].turn for q in asked if q.entry_id in by_id],
        judge=judge,
    )
    return Experiment(
        configuration=resolved,
        recorded=recorded,
        result=build_result(
            resolved,
            dataset=dataset.name,
            dataset_version=dataset.version,
            source=deployment.name,
            golden_scores=golden_scores,
            trajectory_scores=trajectory_scores,
            grounding_scores=grounding_scores,
            judge=judge_name,
            judge_tokens=judge_tokens,
            inert_axes=resolved.inert_axes(
                knowledge_lane=knowledge_lane,
                photo_lane=photo_lane,
                prompt_read=prompt_read,
            ),
            caveat=caveat,
            now=now,
        ),
    )
