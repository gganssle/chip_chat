"""The configuration layer is the part of the Foundry wiring with logic in it.

The verification scripts themselves cost tokens and need Azure credentials, so
they are not run here. What is worth testing is the promise issue #8 actually
made: that a deployment name is a configuration value and never a literal.
"""

import pytest

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError

COMPLETE_ENV = {
    "CHIP_CHAT_FOUNDRY_ENDPOINT": "https://aif-example.cognitiveservices.azure.com/",
    "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "gpt-5-mini",
    "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT": "gpt-4.1-mini",
}


def test_from_env_reads_both_lanes() -> None:
    config = FoundryConfig.from_env(COMPLETE_ENV)

    assert config.endpoint == COMPLETE_ENV["CHIP_CHAT_FOUNDRY_ENDPOINT"]
    assert config.chat_deployment == "gpt-5-mini"
    assert config.vision_deployment == "gpt-4.1-mini"


def test_lanes_are_swappable_without_touching_code() -> None:
    """The acceptance criterion, as a test.

    Repointing a lane at a different deployment -- which is what a Phase 9 eval
    experiment does -- must be an environment change and nothing else.
    """
    experiment = FoundryConfig.from_env(
        {**COMPLETE_ENV, "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "gpt-4.1-mini-candidate"}
    )

    assert experiment.deployment_for("chat") == "gpt-4.1-mini-candidate"
    assert experiment.deployment_for("vision") == "gpt-4.1-mini"


def test_api_version_is_pinned_by_default() -> None:
    """A floating api-version is a silently changing response shape."""
    assert FoundryConfig.from_env(COMPLETE_ENV).api_version == "2024-10-21"


def test_api_version_is_overridable() -> None:
    config = FoundryConfig.from_env(
        {**COMPLETE_ENV, "CHIP_CHAT_FOUNDRY_API_VERSION": "2025-01-01-preview"}
    )

    assert config.api_version == "2025-01-01-preview"


def test_entra_is_the_default_credential_path() -> None:
    assert FoundryConfig.from_env(COMPLETE_ENV).uses_entra


def test_an_api_key_switches_off_entra() -> None:
    config = FoundryConfig.from_env(
        {**COMPLETE_ENV, "CHIP_CHAT_FOUNDRY_API_KEY": "not-a-real-key"}
    )

    assert not config.uses_entra
    assert config.api_key == "not-a-real-key"


@pytest.mark.parametrize(
    "missing",
    [
        "CHIP_CHAT_FOUNDRY_ENDPOINT",
        "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT",
        "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT",
    ],
)
def test_a_missing_variable_fails_at_construction(missing: str) -> None:
    """Not at the first inference call, where it reads as an auth problem."""
    env = {key: value for key, value in COMPLETE_ENV.items() if key != missing}

    with pytest.raises(FoundryConfigError, match=missing):
        FoundryConfig.from_env(env)


def test_a_blank_variable_is_treated_as_missing() -> None:
    env = {**COMPLETE_ENV, "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "   "}

    with pytest.raises(FoundryConfigError, match="CHAT_DEPLOYMENT"):
        FoundryConfig.from_env(env)


def test_an_unknown_lane_is_refused() -> None:
    with pytest.raises(FoundryConfigError, match="unknown lane"):
        FoundryConfig.from_env(COMPLETE_ENV).deployment_for("audio")
