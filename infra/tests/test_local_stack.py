"""The local stack's port lives in two files, so a test keeps them agreeing.

`compose.yaml` publishes Phoenix on a port and the `Makefile` sends spans to
that port. Nothing connects the two but a number typed twice, and the failure
when they drift is a silent one: the container is healthy, `make trace` reports
success, and the traces are somewhere nobody is looking.

It lives in `infra/` because the local stack is infrastructure, even though it is
the only piece of it that is not Terraform.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "compose.yaml"
MAKEFILE = REPO_ROOT / "Makefile"


def _make_default(variable: str) -> str:
    """Return the ``VAR ?= value`` default the Makefile declares for ``variable``."""
    match = re.search(rf"^{variable} \?= (.+)$", MAKEFILE.read_text(), re.MULTILINE)
    assert match is not None, f"{variable} has no default in the Makefile"
    return match.group(1).strip()


def test_the_compose_file_is_where_the_makefile_expects() -> None:
    assert COMPOSE.is_file()


def test_phoenix_is_published_on_the_port_the_makefile_traces_to() -> None:
    port = _make_default("PHOENIX_PORT")
    assert f'"${{PHOENIX_PORT:-{port}}}:6006"' in COMPOSE.read_text()


def test_the_traced_endpoint_is_built_from_that_port() -> None:
    assert _make_default("PHOENIX_URL") == "http://localhost:$(PHOENIX_PORT)"
    assert _make_default("OTEL_EXPORTER_OTLP_ENDPOINT") == "$(PHOENIX_URL)"


def test_the_image_is_pinned() -> None:
    # A dev loop that changes underneath you is not a dev loop; `latest` would
    # make "it worked yesterday" unanswerable.
    assert "image: arizephoenix/phoenix:version-" in COMPOSE.read_text()
