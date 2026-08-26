"""The image is a build artefact of this repository, so its wiring is a test.

Nothing here builds a container -- that needs a daemon and belongs in CI. What it
checks is the handful of facts that are typed in more than one place and fail
silently when they drift: the image name, the entrypoint, and the build context.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "agent" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
MAKEFILE = REPO_ROOT / "Makefile"
OUTPUTS = REPO_ROOT / "infra" / "terraform" / "outputs.tf"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-image.yml"


def _make_default(variable: str) -> str:
    match = re.search(rf"^{variable}\s+\?= (.+)$", MAKEFILE.read_text(), re.MULTILINE)
    assert match is not None, f"{variable} has no default in the Makefile"
    return match.group(1).strip()


def test_the_dockerfile_is_where_the_makefile_looks() -> None:
    assert DOCKERFILE.is_file()
    assert "-f agent/Dockerfile" in MAKEFILE.read_text()


def test_the_build_context_is_the_repository_root() -> None:
    # The agent is a uv workspace member: its lockfile is at the root and
    # `uv sync --package` needs the workspace. Building from agent/ cannot work,
    # and the failure is a confusing one.
    assert "COPY . ." in DOCKERFILE.read_text()
    assert re.search(
        r"docker build -f agent/Dockerfile [^\n]*\s\.\s*$",
        MAKEFILE.read_text(),
        re.MULTILINE,
    )


def test_the_image_installs_from_the_committed_lockfile() -> None:
    # --frozen, or the image's dependency set is whatever resolved that morning
    # and the digest an agent version pins stops meaning anything.
    text = DOCKERFILE.read_text()
    assert "--frozen" in text
    assert "--package chip-chat-agent" in text
    # Workspace members install editable by default, which would leave the
    # virtualenv pointing at a build-stage path that the runtime stage lacks.
    assert "--no-editable" in text


def test_every_base_image_is_pinned() -> None:
    text = DOCKERFILE.read_text()
    for variable in ("PYTHON_VERSION", "UV_VERSION", "DEBIAN_RELEASE"):
        assert re.search(rf"^ARG {variable}=\S+", text, re.MULTILINE), variable
    assert ":latest" not in text


def test_the_entrypoint_is_the_container_module() -> None:
    assert '"chip_chat.agent.container"' in DOCKERFILE.read_text()


def test_the_image_does_not_run_as_root() -> None:
    assert re.search(r"^USER \w+", DOCKERFILE.read_text(), re.MULTILINE)


def test_the_exporter_configuration_is_not_baked_into_the_image() -> None:
    # It is immutable per agent version and it differs per backend. Baking it in
    # would make the #78 repoint an image rebuild instead of a new version.
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("ENV") or stripped.startswith("ARG"):
            assert "OTEL_EXPORTER_OTLP" not in stripped, line


def test_the_agent_name_in_the_image_is_the_service_name_it_reports() -> None:
    from chip_chat.otel.service import agent_service_name

    match = re.search(r"^ENV CHIP_CHAT_AGENT_NAME=(\S+)", DOCKERFILE.read_text(), re.M)
    assert match is not None
    assert match.group(1) == agent_service_name({})


def test_the_dockerignore_keeps_the_context_small() -> None:
    ignored = DOCKERIGNORE.read_text()
    for entry in (".git", ".venv", ".env", "**/__pycache__"):
        assert entry in ignored


def test_the_image_name_agrees_across_make_terraform_and_ci() -> None:
    name = _make_default("AGENT_IMAGE_NAME")
    assert name == "chip-chat-agent"
    # The registry output builds the repository path from the same name; a
    # rename in one place would otherwise publish to a repository nothing pulls.
    assert f"/{name}" in OUTPUTS.read_text()
    assert name in WORKFLOW.read_text()
