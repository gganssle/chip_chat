"""The things an experiment varies, as one value that can be named and hashed.

Issue #73's second sentence is the whole design of this module: *configurations
are the things that vary; nothing being experimented on should be hardcoded
anywhere*. So the four axes the ticket names -- system prompt version, model
deployment, retrieval settings, matcher thresholds -- are fields here, every one
of them is written down in ``eval/experiments/CONFIGURATIONS.json`` rather than
in code, and the runner takes one of these and nothing else.

**A configuration has a fingerprint for the same reason the dataset has a
version.** ``eval/dataset/README.md`` makes the argument once and it does not
need making twice: an ordinal reads better and is wrong exactly when it matters,
which is the day somebody edits a configuration, runs it, and reports the result
under the old name. :attr:`ExperimentConfiguration.fingerprint` is twelve hex
digits of the canonical form, it includes the prompt's *digest* rather than its
revision string, and two results carrying the same pair of fingerprints -- this
one and the dataset's -- are comparable without anybody arranging it.

**Deployments default to the environment, deliberately.** A configuration that
wrote ``gpt-5-mini`` into the file would be a deployment name in a second place,
and :mod:`chip_chat.agent.foundry` exists precisely because a deployment name in
one place is already one too many. So the two deployment fields are empty by
default and mean *whatever ``CHIP_CHAT_FOUNDRY_*_DEPLOYMENT`` says*; a
configuration that is **about** the model names it, and one that is about the
prompt does not. :meth:`ExperimentConfiguration.resolve` is where the empty
string becomes the name that actually ran, so a recorded result never says
*"whatever was configured"*.

**Two of the four axes are declared here and applied elsewhere, and the
distinction is reported rather than hidden.** The prompt and the deployment
reach the model on every run. Retrieval settings reach a
:class:`~chip_chat.search.lane.KnowledgeLane` and matcher thresholds reach
:class:`~chip_chat.vision.matcher.SlotRules`, and a run against the week-one
slice wires neither lane -- so on that source those two axes are recorded and
inert. :meth:`ExperimentConfiguration.inert_axes` says which, the report prints
it above the numbers, and nobody reads a flat line as evidence that a threshold
does not matter.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from chip_chat.agent.prompt import PROMPT_DIR, REVISION, SystemPrompt
from chip_chat.agent.prompt import load as load_prompt
from chip_chat.eval.dataset.entries import digest_of

__all__ = [
    "DEFAULT_MANIFEST",
    "MATCHER_VARIABLE",
    "ConfigurationError",
    "ExperimentConfiguration",
    "MatcherThresholds",
    "RetrievalSettings",
    "configurations",
    "named",
]

DEFAULT_MANIFEST: Final = Path("eval/experiments/CONFIGURATIONS.json")
"""Where the configurations live. Data, in the repository, under review."""

MATCHER_VARIABLE: Final = "CHIP_CHAT_MATCHER_{slot}_THRESHOLD"
"""How a slot floor reaches :meth:`chip_chat.vision.matcher.SlotRules.from_env`.

Issue #54's third acceptance criterion made the floors settable without a code
change, and this is the experiment harness taking it at its word rather than
inventing a second way to set the same number.
"""

_FINGERPRINT_NOTE: Final = "prompt-digest"


class ConfigurationError(ValueError):
    """A configuration that cannot be believed.

    Raised while the manifest is being read, never while a run is being scored
    -- the rule every set in ``eval/`` follows, because a configuration that
    contradicts itself produces numbers that look exactly like numbers.
    """


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    """What the knowledge lane is asked for, when there is one.

    Attributes:
        top: Passages to return. RFC-001 §08's ``k``.
        rerank: Whether to ask the index for semantic reranking. The expensive
            half of the hybrid design, and therefore the one worth an arm.
    """

    top: int = 5
    rerank: bool = True

    def __post_init__(self) -> None:
        if self.top < 1:
            raise ConfigurationError(f"retrieval.top must be at least 1, got {self.top}")

    def as_json(self) -> Mapping[str, Any]:
        """The canonical form, for the fingerprint and for a recorded result."""
        return {"top": self.top, "rerank": self.rerank}


@dataclass(frozen=True, slots=True)
class MatcherThresholds:
    """Per-slot confidence floors, as an experiment names them.

    Attributes:
        floors: Slot name to floor, e.g. ``{"protein": 0.7}``. Empty means the
            argued starting points of
            :meth:`chip_chat.vision.matcher.SlotRules.defaults` -- which is a
            configuration too, and is recorded as one rather than as silence.
    """

    floors: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for slot, floor in self.floors.items():
            if not 0.0 <= floor <= 1.0:
                raise ConfigurationError(
                    f"the {slot} floor must be a probability in [0, 1], got {floor}"
                )

    def environment(self) -> Mapping[str, str]:
        """These floors, as the variables the matcher already reads.

        Returns:
            A mapping to overlay on the environment. Empty where the
            configuration names no floor, which leaves the defaults in place.
        """
        return MappingProxyType(
            {
                MATCHER_VARIABLE.format(slot=slot.upper()): str(floor)
                for slot, floor in sorted(self.floors.items())
            }
        )

    def as_json(self) -> Mapping[str, Any]:
        """The canonical form."""
        return {slot: self.floors[slot] for slot in sorted(self.floors)}


@dataclass(frozen=True, slots=True)
class ExperimentConfiguration:
    """One arm of an experiment: everything that varies, and nothing that does not.

    Attributes:
        name: What the experiment is called. The join key between a run, a
            recorded result and a comparison, so it has to be stable.
        prompt_revision: Which revision of the system prompt to run under.
        prompt_directory: Where to read it from. ``None`` is the directory the
            agent ships, so ``v1`` means the shipped prompt without copying it.
            A candidate revision lives beside the configurations instead, which
            keeps an experiment's prompt out of the deployed package until it
            has earned its way in.
        chat_deployment: Deployment answering the agent's turns. Empty means
            whatever the environment says; see the module docstring.
        vision_deployment: Deployment answering the photo lane.
        retrieval: What the knowledge lane is asked for.
        matcher: The photo matcher's slot floors.
        why: What this arm is for. Required, for the reason every manifest in
            ``eval/`` requires one: an arm without an argument is an arm nobody
            can decide the result of.
    """

    name: str
    prompt_revision: str = REVISION
    prompt_directory: Path | None = None
    chat_deployment: str = ""
    vision_deployment: str = ""
    retrieval: RetrievalSettings = field(default_factory=RetrievalSettings)
    matcher: MatcherThresholds = field(default_factory=MatcherThresholds)
    why: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ConfigurationError("a configuration needs a name")
        if not self.why.strip():
            raise ConfigurationError(
                f"{self.name}: a configuration needs a `why`; an arm without an "
                "argument is an arm nobody can decide the result of"
            )

    def prompt(self) -> SystemPrompt:
        """Load the revision this arm runs under.

        Returns:
            The prompt, carrying the version every ``chat.turn`` span under this
            arm will record.

        Raises:
            ConfigurationError: If no such revision exists where it was looked
                for. Raised here rather than at the first model call, so a
                misspelled revision costs nothing.
        """
        directory = PROMPT_DIR if self.prompt_directory is None else self.prompt_directory
        try:
            return load_prompt(self.prompt_revision, directory=directory)
        except FileNotFoundError as error:
            raise ConfigurationError(
                f"{self.name}: no prompt revision {self.prompt_revision!r} in {directory}"
            ) from error

    def resolve(self, env: Mapping[str, str]) -> "ExperimentConfiguration":
        """Fill the empty deployment fields in from the environment.

        Args:
            env: The environment the run will use.

        Returns:
            The same configuration with both deployments named. A recorded
            result carries the resolved form, because *whatever was configured*
            is not a value anybody can compare two runs on.
        """
        return replace(
            self,
            chat_deployment=(
                self.chat_deployment
                or env.get("CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT", "").strip()
            ),
            vision_deployment=(
                self.vision_deployment
                or env.get("CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT", "").strip()
            ),
        )

    def environment(self) -> Mapping[str, str]:
        """The variables this configuration sets, to overlay on a run.

        Returns:
            The matcher floors and the two deployment names, as the variables
            the code that reads them already reads. Everything an experiment
            varies that is *already* configuration reaches its consumer this
            way rather than through a second keyword argument, which is the
            only arrangement in which a configuration and a deployment cannot
            disagree.
        """
        overlay = dict(self.matcher.environment())
        if self.chat_deployment:
            overlay["CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT"] = self.chat_deployment
        if self.vision_deployment:
            overlay["CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT"] = self.vision_deployment
        return MappingProxyType(overlay)

    def inert_axes(
        self,
        *,
        knowledge_lane: bool,
        photo_lane: bool,
        prompt_read: bool = True,
    ) -> tuple[str, ...]:
        """Which axes this run records but cannot apply.

        Args:
            knowledge_lane: Whether a retriever is wired.
            photo_lane: Whether the photo lane is wired.
            prompt_read: Whether whatever answers the rows actually reads the
                system prompt. False under the routing oracle, which answers
                from the golden set and would produce two identical results for
                two different prompts -- a comparison that looks like *this
                change made no difference* and is in fact *nothing read the
                change*. That is the single most misleading document this
                harness could emit, so the flag exists to stop it emitting one
                silently.

        Returns:
            The names of the axes that will not reach anything, in a fixed
            order. See the module docstring: a flat line on an axis nothing
            applied is not evidence about that axis.
        """
        inert: list[str] = []
        if not prompt_read:
            inert.append("prompt")
        if not knowledge_lane:
            inert.append("retrieval")
        if not photo_lane:
            inert.append("matcher")
        return tuple(inert)

    def as_json(self) -> Mapping[str, Any]:
        """The canonical form, for the fingerprint and for a recorded result."""
        return {
            "name": self.name,
            "prompt_revision": self.prompt_revision,
            "prompt_directory": (
                "" if self.prompt_directory is None else str(self.prompt_directory)
            ),
            "chat_deployment": self.chat_deployment,
            "vision_deployment": self.vision_deployment,
            "retrieval": self.retrieval.as_json(),
            "matcher": self.matcher.as_json(),
        }

    @property
    def fingerprint(self) -> str:
        """Twelve hex digits identifying everything this arm varies.

        The prompt enters as its *content* digest rather than as its revision
        string, so an edited revision is a different configuration whether or
        not anybody bumped the name. That is :mod:`chip_chat.agent.prompt`'s own
        argument, applied one level up.
        """
        payload = dict(self.as_json())
        payload[_FINGERPRINT_NOTE] = self.prompt().digest
        return digest_of(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        )

    def describe(self) -> str:
        """One line naming this arm and what identifies it."""
        return f"{self.name} ({self.fingerprint}) — prompt {self.prompt().version}"


def configurations(
    manifest: Path = DEFAULT_MANIFEST,
) -> tuple[ExperimentConfiguration, ...]:
    """Read every configuration from the manifest.

    Args:
        manifest: The JSON file. Relative paths inside it -- ``prompt_directory``
            -- are read relative to the repository root the command was run
            from, which is the same rule every other manifest in ``eval/``
            follows.

    Returns:
        The configurations, in file order.

    Raises:
        ConfigurationError: If the file cannot be read, is not an object with a
            ``configurations`` array, holds a duplicate name, or holds an arm
            that contradicts itself.
    """
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"cannot read {manifest}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"{manifest} is not valid JSON: {error}") from error
    if not isinstance(payload, dict) or not isinstance(
        payload.get("configurations"), list
    ):
        raise ConfigurationError(
            f"{manifest} must be an object with a `configurations` array"
        )
    arms = tuple(
        _configuration(entry, manifest, index)
        for index, entry in enumerate(payload["configurations"])
    )
    _distinct(arms, manifest)
    return arms


def named(arms: Sequence[ExperimentConfiguration], name: str) -> ExperimentConfiguration:
    """Return the arm called ``name``.

    Args:
        arms: What was loaded.
        name: The arm wanted.

    Returns:
        The configuration.

    Raises:
        ConfigurationError: If there is no such arm. The message lists the ones
            there are, because the usual cause is a typo and the usual next
            command is the same one spelled right.
    """
    for arm in arms:
        if arm.name == name:
            return arm
    known = ", ".join(arm.name for arm in arms) or "none"
    raise ConfigurationError(f"no configuration called {name!r}; known: {known}")


def _configuration(entry: Any, manifest: Path, index: int) -> ExperimentConfiguration:
    """Read one arm, refusing one that is not an object."""
    where = f"{manifest} entry {index}"
    if not isinstance(entry, dict):
        raise ConfigurationError(f"{where} is not an object")
    try:
        return ExperimentConfiguration(
            name=_text(entry, "name", where),
            prompt_revision=_text(entry, "prompt_revision", where, default=REVISION),
            prompt_directory=_directory(entry, where),
            chat_deployment=_text(entry, "chat_deployment", where, default=""),
            vision_deployment=_text(entry, "vision_deployment", where, default=""),
            retrieval=_retrieval(entry.get("retrieval"), where),
            matcher=_matcher(entry.get("matcher"), where),
            why=_text(entry, "why", where, default=""),
        )
    except ConfigurationError as error:
        raise ConfigurationError(f"{where}: {error}") from None


def _retrieval(value: Any, where: str) -> RetrievalSettings:
    if value is None:
        return RetrievalSettings()
    if not isinstance(value, dict):
        raise ConfigurationError(f"{where}: `retrieval` must be an object")
    top = value.get("top", RetrievalSettings().top)
    rerank = value.get("rerank", RetrievalSettings().rerank)
    if not isinstance(top, int) or isinstance(top, bool):
        raise ConfigurationError(f"{where}: `retrieval.top` must be a whole number")
    if not isinstance(rerank, bool):
        raise ConfigurationError(f"{where}: `retrieval.rerank` must be a boolean")
    return RetrievalSettings(top=top, rerank=rerank)


def _matcher(value: Any, where: str) -> MatcherThresholds:
    if value is None:
        return MatcherThresholds()
    if not isinstance(value, dict):
        raise ConfigurationError(f"{where}: `matcher` must be an object")
    floors: dict[str, float] = {}
    for slot, floor in value.items():
        if isinstance(floor, bool) or not isinstance(floor, int | float):
            raise ConfigurationError(f"{where}: the {slot} floor must be a number")
        floors[str(slot)] = float(floor)
    return MatcherThresholds(floors=floors)


def _directory(entry: Mapping[str, Any], where: str) -> Path | None:
    value = entry.get("prompt_directory")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"{where}: `prompt_directory` must be a string")
    return Path(value)


def _text(
    entry: Mapping[str, Any], key: str, where: str, *, default: str | None = None
) -> str:
    value = entry.get(key, default)
    if value is None:
        raise ConfigurationError(f"{where}: `{key}` is required")
    if not isinstance(value, str):
        raise ConfigurationError(f"{where}: `{key}` must be a string")
    return value


def _distinct(arms: Sequence[ExperimentConfiguration], manifest: Path) -> None:
    """Refuse two arms with the same name.

    A duplicate is not a warning. Two arms sharing a name means one comparison
    of two things silently became a comparison of one thing with itself, and the
    document it produces reads exactly like a change that made no difference.
    """
    seen: set[str] = set()
    for arm in arms:
        if arm.name in seen:
            raise ConfigurationError(
                f"{manifest}: two configurations called {arm.name!r}"
            )
        seen.add(arm.name)
