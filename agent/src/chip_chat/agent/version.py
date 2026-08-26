"""The hosted agent version: what Foundry is told to run, and how it exports.

A hosted agent is a *versioned* resource. You do not configure a running agent;
you register a version -- an image reference plus an environment -- and Foundry
runs that. The consequence that shapes this module is documented in
``docs/service-inventory.md`` section 2.1 and repeated in the decision record:

    **Environment variables are immutable per agent version.**

So the Phase 8 move from the local backend to the hosted one (issue #78) is not
editing a setting. It is cutting a new version whose only diff is
``OTEL_EXPORTER_OTLP_ENDPOINT``, ``_PROTOCOL``, ``_HEADERS`` and the connection
holding the credentials. This module is where that diff is expressed, which is
why it renders a manifest rather than mutating anything:

.. code-block:: bash

    make agent-version                                    # print the manifest
    uv run python -m chip_chat.agent.version render --image <ref>
    uv run python -m chip_chat.agent.version register --image <ref>

**Credentials never appear in the manifest.** ``OTEL_EXPORTER_OTLP_HEADERS``
carries an API key and a space id for the hosted backend, and the supported way
to supply it is a *CustomKeys connection* on the project, referenced by
expression. :func:`manifest` refuses to emit a literal value for that variable at
all, so there is no version of this file that leaks a key into an agent
definition or into a ``terraform plan``.

**On ``register``.** The registration call is written from the published Agents
data-plane shape and is *not* exercised by this repository's tests -- the same
honesty ``chip_chat.agent.threads`` applies to the retention probe applies here.
``render`` is the part with logic in it and the part that is tested; ``register``
is a POST of that document, and it prints the service's answer verbatim so the
first run tells you the truth rather than a wrapper's summary of it.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from chip_chat.agent.foundry import credential
from chip_chat.otel.service import agent_service_name

__all__ = [
    "OTEL_HEADERS_KEY",
    "AgentVersion",
    "AgentVersionError",
    "main",
    "manifest",
]

_AI_SCOPE = "https://ai.azure.com/.default"
_DEFAULT_API_VERSION = "v1"

OTEL_HEADERS_KEY = "OTEL_EXPORTER_OTLP_HEADERS"
"""The one variable that may hold a credential, and therefore may not hold one."""

_DEFAULT_CONNECTION = "otel-secrets"
_DEFAULT_PROTOCOL = "http/protobuf"
_HEADERS_SECRET = "otlp_headers"


class AgentVersionError(RuntimeError):
    """The version described would not be a version worth registering."""


def _connection_expression(connection: str) -> str:
    """The expression Foundry resolves against a CustomKeys connection."""
    return f"${{{{connections.{connection}.credentials.{_HEADERS_SECRET}}}}}"


@dataclass(frozen=True, slots=True)
class AgentVersion:
    """One registerable version of the hosted agent."""

    image: str
    """Fully qualified image reference. ``registry/repository@sha256:...`` for
    anything that will be registered: a version pointing at a moving tag is not
    a version, and the trace it produces cannot be attributed to a build."""

    name: str = ""
    """The agent resource's name. Also, forcibly, its ``service.name``."""

    otlp_endpoint: str = ""
    """Where the agent's spans go. Immutable once this version is registered."""

    otlp_protocol: str = _DEFAULT_PROTOCOL

    headers_connection: str = _DEFAULT_CONNECTION
    """CustomKeys connection holding the OTLP headers. Never the headers."""

    environment: str = "local"
    """``deployment.environment`` on every span this agent emits."""

    chat_deployment: str = ""
    vision_deployment: str = ""

    client_id: str = ""
    """``AZURE_CLIENT_ID`` for the user-assigned identity the container runs as."""

    description: str = "Cilantro, the Chip Chat agent. Hosted agent (D8)."

    def environment_variables(self) -> dict[str, str]:
        """The environment this version is frozen with.

        Raises:
            AgentVersionError: If the OTLP headers were supplied literally. The
                only accepted value for that variable is a connection reference.
        """
        variables = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.otlp_endpoint,
            "OTEL_EXPORTER_OTLP_PROTOCOL": self.otlp_protocol,
            OTEL_HEADERS_KEY: _connection_expression(self.headers_connection),
            # So that what the container reports as its own name and what the
            # platform stamps on its spans are the same string. See
            # chip_chat.otel.service.turn_service_names.
            "CHIP_CHAT_AGENT_NAME": self.name,
            "CHIP_CHAT_ENVIRONMENT": self.environment,
        }
        if self.chat_deployment:
            variables["CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT"] = self.chat_deployment
        if self.vision_deployment:
            variables["CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT"] = self.vision_deployment
        if self.client_id:
            variables["AZURE_CLIENT_ID"] = self.client_id
        if not self.otlp_endpoint:
            raise AgentVersionError(
                "OTEL_EXPORTER_OTLP_ENDPOINT is empty. It is immutable once this "
                "version exists, so an agent registered without it exports to "
                "Application Insights only and costs a version to correct. Set "
                "OTEL_EXPORTER_OTLP_ENDPOINT or pass --otlp-endpoint."
            )
        return variables

    def check_image(self, *, strict: bool) -> None:
        """Refuse an image reference that cannot identify a build.

        Args:
            strict: Require a digest. On for ``register``, off for ``render`` --
                printing the manifest for a locally built ``:dev`` tag is a
                reasonable thing to want, registering one is not.

        Raises:
            AgentVersionError: If the reference is empty, floating, or -- under
                ``strict`` -- not pinned by digest.
        """
        if not self.image:
            raise AgentVersionError("no image reference; pass --image")
        if self.image.endswith(":latest"):
            raise AgentVersionError(
                f"{self.image!r} is a moving tag. An agent version is a fixed "
                "thing; pin the image by digest."
            )
        if strict and "@sha256:" not in self.image:
            raise AgentVersionError(
                f"{self.image!r} is not pinned by digest. Read the digest with:\n"
                "  az acr repository show-manifests --name <registry> "
                "--repository chip-chat-agent"
            )

    @classmethod
    def from_env(
        cls, image: str, env: Mapping[str, str] | None = None, **overrides: str
    ) -> "AgentVersion":
        """Build a version from the environment the rest of the tooling reads."""
        source = os.environ if env is None else env
        values: dict[str, str] = {
            "image": image,
            "name": agent_service_name(source),
            "otlp_endpoint": source.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip(),
            "headers_connection": (
                source.get("CHIP_CHAT_OTEL_CONNECTION", "").strip() or _DEFAULT_CONNECTION
            ),
            "environment": source.get("CHIP_CHAT_ENVIRONMENT", "").strip() or "local",
            "chat_deployment": source.get(
                "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT", ""
            ).strip(),
            "vision_deployment": source.get(
                "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT", ""
            ).strip(),
            "client_id": source.get("AZURE_CLIENT_ID", "").strip(),
        }
        values.update({key: value for key, value in overrides.items() if value})
        return cls(**values)


def manifest(version: AgentVersion, *, strict_image: bool = False) -> dict[str, Any]:
    """Render ``version`` as the document Foundry is asked to register.

    Args:
        version: The version to describe.
        strict_image: Require the image to be pinned by digest.

    Returns:
        A JSON-serialisable manifest. No credential appears in it, by
        construction -- see :func:`AgentVersion.environment_variables`.

    Raises:
        AgentVersionError: If the version would not be worth registering.
    """
    version.check_image(strict=strict_image)
    return {
        "name": version.name,
        "description": version.description,
        "kind": "hosted",
        "container": {"image": version.image},
        "environment_variables": [
            {"name": name, "value": value}
            for name, value in sorted(version.environment_variables().items())
        ],
    }


def register(
    document: Mapping[str, Any],
    *,
    project_endpoint: str,
    api_version: str = _DEFAULT_API_VERSION,
) -> dict[str, Any]:
    """POST ``document`` to the project's agents collection.

    Raw ``urllib`` and a verbatim response, for the reason
    :mod:`chip_chat.agent.threads` gives: what is being learned here is what the
    *service* says, and a typed SDK model answers by discarding the fields that
    would settle it.

    Raises:
        AgentVersionError: If the call is refused. The body is included, because
            the body is the diagnosis.
    """
    token = credential().get_token(_AI_SCOPE).token
    url = f"{project_endpoint.rstrip('/')}/agents?api-version={api_version}"
    request = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(document).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            decoded: dict[str, Any] = json.loads(response.read())
            return decoded
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise AgentVersionError(
            f"POST agents returned HTTP {error.code}\n{detail}\n\n"
            "If this is a 404, the agents path or api-version has moved since "
            "docs/service-inventory.md was checked; --api-version takes another."
        ) from error


def main(argv: Sequence[str] | None = None) -> int:
    """Render or register one agent version. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.agent.version",
        description="The hosted agent's version manifest.",
    )
    parser.add_argument("action", choices=["render", "register"])
    parser.add_argument("--image", default="", help="fully qualified image reference")
    parser.add_argument("--otlp-endpoint", default="", help="overrides the environment")
    parser.add_argument(
        "--connection",
        default="",
        help=(
            f"CustomKeys connection holding {_HEADERS_SECRET} "
            f"(default {_DEFAULT_CONNECTION})"
        ),
    )
    parser.add_argument("--api-version", default=_DEFAULT_API_VERSION)
    options = parser.parse_args(argv)

    version = AgentVersion.from_env(
        options.image,
        otlp_endpoint=options.otlp_endpoint,
        headers_connection=options.connection,
    )
    try:
        document = manifest(version, strict_image=options.action == "register")
    except AgentVersionError as error:
        print(error, file=sys.stderr)
        return 1

    if options.action == "render":
        print(json.dumps(document, indent=2))
        print(
            "\nThese environment variables are IMMUTABLE once the version exists. "
            "Repointing\nthe exporter later (issue #78) means registering another "
            "version, not editing this one.",
            file=sys.stderr,
        )
        return 0

    project_endpoint = os.environ.get("CHIP_CHAT_FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not project_endpoint:
        print(
            "CHIP_CHAT_FOUNDRY_PROJECT_ENDPOINT is not set. Read it with:\n"
            "  terraform -chdir=infra/terraform output -json foundry_project_endpoints"
            '\nand take the "AI Foundry API" value.',
            file=sys.stderr,
        )
        return 1

    try:
        registered = register(
            document,
            project_endpoint=project_endpoint,
            api_version=options.api_version,
        )
    except AgentVersionError as error:
        print(error, file=sys.stderr)
        return 1

    print(json.dumps(registered, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
