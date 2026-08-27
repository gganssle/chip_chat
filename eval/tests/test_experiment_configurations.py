"""The four axes, as data. #73's *nothing hardcoded*, as assertions.

Two things are under test and they pull in opposite directions. The manifest has
to be *refused* when it contradicts itself, because a configuration that
contradicts itself produces numbers that look exactly like numbers; and the
fingerprint has to be *stable* under everything that is not a change, because a
fingerprint that moved with the weather would make every comparison a warning.
"""

import ast
import json
from pathlib import Path

import pytest

from chip_chat.eval.experiment.configurations import (
    DEFAULT_MANIFEST,
    ConfigurationError,
    ExperimentConfiguration,
    MatcherThresholds,
    RetrievalSettings,
    configurations,
    named,
)

PACKAGE = (
    Path(__file__).resolve().parents[1] / "src" / "chip_chat" / "eval" / "experiment"
)


def _manifest(tmp_path: Path, *entries: dict[str, object]) -> Path:
    path = tmp_path / "configurations.json"
    path.write_text(json.dumps({"configurations": list(entries)}), encoding="utf-8")
    return path


def _arm(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {"name": "an-arm", "why": "because"}
    body.update(changes)
    return body


def test_the_shipped_manifest_holds_at_least_two_comparable_arms(
    repo_root: Path,
) -> None:
    """#73's demo criterion needs two of them, so one arm is a broken state."""
    arms = configurations(repo_root / DEFAULT_MANIFEST)

    assert len(arms) >= 2
    assert {arm.name for arm in arms} >= {"shipped", "lean-lanes"}


def test_every_shipped_arm_loads_its_prompt(repo_root: Path) -> None:
    """A misspelled revision costs nothing here and a whole run later."""
    monkeyed = repo_root
    for arm in configurations(monkeyed / DEFAULT_MANIFEST):
        directory = arm.prompt_directory
        resolved = arm if directory is None else _rooted(arm, monkeyed)
        assert resolved.prompt().text.strip()


def _rooted(arm: ExperimentConfiguration, root: Path) -> ExperimentConfiguration:
    from dataclasses import replace

    assert arm.prompt_directory is not None
    return replace(arm, prompt_directory=root / arm.prompt_directory)


def test_two_arms_with_the_same_name_are_refused(tmp_path: Path) -> None:
    """One comparison of two things silently becoming one of a thing with itself."""
    manifest = _manifest(tmp_path, _arm(), _arm())

    with pytest.raises(ConfigurationError, match="two configurations"):
        configurations(manifest)


def test_an_arm_without_an_argument_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, {"name": "nameless-purpose"})

    with pytest.raises(ConfigurationError, match="needs a `why`"):
        configurations(manifest)


def test_a_malformed_retrieval_block_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, _arm(retrieval={"top": "five"}))

    with pytest.raises(ConfigurationError, match="whole number"):
        configurations(manifest)


def test_a_floor_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="probability"):
        MatcherThresholds(floors={"protein": 1.5})


def test_a_top_below_one_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="at least 1"):
        RetrievalSettings(top=0)


def test_named_says_what_there_is_when_the_name_is_wrong(tmp_path: Path) -> None:
    arms = configurations(_manifest(tmp_path, _arm(name="one"), _arm(name="two")))

    with pytest.raises(ConfigurationError, match="known: one, two"):
        named(arms, "three")


def test_the_fingerprint_moves_with_the_prompt_text_not_the_revision_name(
    tmp_path: Path,
) -> None:
    """#60's argument, one level up: an edited revision is a different arm."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system-x.md").write_text("first", encoding="utf-8")
    from chip_chat.agent.prompt import load

    load.cache_clear()
    arm = ExperimentConfiguration(
        name="x", prompt_revision="x", prompt_directory=prompts, why="w"
    )
    before = arm.fingerprint

    (prompts / "system-x.md").write_text("second", encoding="utf-8")
    # `chip_chat.agent.prompt.load` is memoised, and the memo is keyed by
    # revision and directory rather than by content -- which is correct for a
    # process serving one build and wrong for a test that edits a file.
    load.cache_clear()

    assert arm.fingerprint != before


def test_the_fingerprint_is_stable_across_two_identical_arms(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system-x.md").write_text("stable", encoding="utf-8")
    one = ExperimentConfiguration(
        name="x", prompt_revision="x", prompt_directory=prompts, why="w"
    )
    two = ExperimentConfiguration(
        name="x", prompt_revision="x", prompt_directory=prompts, why="different why"
    )

    assert one.fingerprint == two.fingerprint


def test_a_missing_revision_is_refused_before_a_run_rather_than_during_one(
    tmp_path: Path,
) -> None:
    arm = ExperimentConfiguration(
        name="x", prompt_revision="nope", prompt_directory=tmp_path, why="w"
    )

    with pytest.raises(ConfigurationError, match="no prompt revision"):
        arm.prompt()


def test_an_empty_deployment_resolves_from_the_environment() -> None:
    """A recorded result must never say *whatever was configured*."""
    arm = ExperimentConfiguration(name="x", why="w")

    resolved = arm.resolve({"CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "some-model"})

    assert resolved.chat_deployment == "some-model"


def test_a_named_deployment_beats_the_environment() -> None:
    arm = ExperimentConfiguration(name="x", chat_deployment="pinned", why="w")

    resolved = arm.resolve({"CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "some-model"})

    assert resolved.chat_deployment == "pinned"


def test_matcher_floors_reach_the_variables_the_matcher_already_reads() -> None:
    """Not a second way to set the same number. Issue #54's, taken at its word."""
    arm = ExperimentConfiguration(
        name="x", matcher=MatcherThresholds(floors={"protein": 0.7}), why="w"
    )

    assert arm.environment()["CHIP_CHAT_MATCHER_PROTEIN_THRESHOLD"] == "0.7"


def test_the_axes_nothing_applied_are_named_rather_than_left_to_read_as_flat() -> None:
    arm = ExperimentConfiguration(name="x", why="w")

    inert = arm.inert_axes(knowledge_lane=False, photo_lane=False, prompt_read=False)

    assert inert == ("prompt", "retrieval", "matcher")


def test_a_wired_lane_makes_its_axis_live() -> None:
    arm = ExperimentConfiguration(name="x", why="w")

    assert arm.inert_axes(knowledge_lane=True, photo_lane=True) == ()


@pytest.mark.parametrize("module", ["run.py", "configurations.py", "results.py"])
def test_the_harness_hardcodes_no_prompt_no_deployment_and_no_threshold(
    module: str,
) -> None:
    """#73's second sentence, enforced the way #78's vendor check is enforced.

    Walks the AST and collects runtime string literals only -- docstrings are
    excluded, because prose naming a deployment as an example is not the runner
    depending on one. A deployment name or a prompt fragment appearing as a
    value here would mean the thing being experimented on had been written into
    the code that runs the experiment.
    """
    tree = ast.parse((PACKAGE / module).read_text())
    docstrings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    literals = {
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
    }
    forbidden = ("gpt-", "you are cilantro", "text-embedding")
    offenders = [value for value in literals if any(word in value for word in forbidden)]
    assert not offenders, (
        f"{module} hardcodes something being experimented on: {offenders}"
    )
