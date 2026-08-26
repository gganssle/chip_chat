"""The agent version manifest. Immutable per version, so it has to be right once."""

import json

import pytest

from chip_chat.agent.version import (
    OTEL_HEADERS_KEY,
    AgentVersion,
    AgentVersionError,
    main,
    manifest,
)

ENV = {
    "CHIP_CHAT_AGENT_NAME": "cilantro-agent",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example-backend.test",
    "CHIP_CHAT_ENVIRONMENT": "demo",
    "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "gpt-5-mini",
    "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT": "gpt-4.1-mini",
    "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
}

DIGEST = "reg.azurecr.io/chip-chat-agent@sha256:" + "a" * 64


def test_the_three_exporter_variables_are_all_present() -> None:
    # All three, or the switch in #78 is not "a new version whose only diff is
    # the exporter variables" -- it is a change of shape.
    variables = AgentVersion.from_env(DIGEST, ENV).environment_variables()
    assert variables["OTEL_EXPORTER_OTLP_ENDPOINT"] == ENV["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert variables["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert OTEL_HEADERS_KEY in variables


def test_the_otlp_headers_are_a_connection_reference_and_never_a_value() -> None:
    version = AgentVersion.from_env(DIGEST, {**ENV, "CHIP_CHAT_OTEL_CONNECTION": "ax"})
    value = version.environment_variables()[OTEL_HEADERS_KEY]

    assert value == "${{connections.ax.credentials.otlp_headers}}"
    # There is no field to put a key in, which is the property worth having:
    # the manifest is printed, committed to a run log and pasted into issues.
    assert "api_key" not in json.dumps(manifest(version))


def test_the_agent_name_is_also_what_the_container_reports() -> None:
    # service.name is forced to the agent's name. If the container reported a
    # different one, every dashboard would be filtering on a name that does not
    # exist in the data.
    variables = AgentVersion.from_env(DIGEST, ENV).environment_variables()
    assert (
        variables["CHIP_CHAT_AGENT_NAME"]
        == manifest(AgentVersion.from_env(DIGEST, ENV))["name"]
    )
    assert variables["CHIP_CHAT_AGENT_NAME"] == "cilantro-agent"


def test_a_version_with_no_exporter_endpoint_is_refused() -> None:
    version = AgentVersion.from_env(DIGEST, {**ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": ""})
    with pytest.raises(AgentVersionError, match="immutable"):
        version.environment_variables()


def test_a_moving_tag_is_refused_outright() -> None:
    version = AgentVersion.from_env("reg.azurecr.io/chip-chat-agent:latest", ENV)
    with pytest.raises(AgentVersionError, match="moving tag"):
        version.check_image(strict=False)


def test_registering_an_untagged_digestless_image_is_refused() -> None:
    version = AgentVersion.from_env("reg.azurecr.io/chip-chat-agent:dev", ENV)
    # Rendering one is fine -- that is what a local build wants.
    version.check_image(strict=False)
    with pytest.raises(AgentVersionError, match="not pinned by digest"):
        version.check_image(strict=True)


def test_the_manifest_names_the_image_and_the_kind() -> None:
    document = manifest(AgentVersion.from_env(DIGEST, ENV), strict_image=True)
    assert document["container"]["image"] == DIGEST
    assert document["kind"] == "hosted"


def test_the_deployments_travel_as_configuration_not_as_literals() -> None:
    variables = AgentVersion.from_env(DIGEST, ENV).environment_variables()
    assert variables["CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT"] == "gpt-5-mini"
    assert variables["CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT"] == "gpt-4.1-mini"


def test_render_prints_the_manifest_and_the_immutability_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)

    assert main(["render", "--image", DIGEST]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["container"]["image"] == DIGEST
    assert "IMMUTABLE" in captured.err


def test_render_without_an_image_fails_with_a_usable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)

    assert main(["render"]) == 1
    assert "--image" in capsys.readouterr().err


def test_register_without_a_project_endpoint_says_where_to_find_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CHIP_CHAT_FOUNDRY_PROJECT_ENDPOINT", raising=False)

    assert main(["register", "--image", DIGEST]) == 1
    assert "foundry_project_endpoints" in capsys.readouterr().err
