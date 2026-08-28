# syntax=docker/dockerfile:1
#
# The monitors image.
#
# `chip_chat.eval.online` has had six monitors and a budget line since #76 and
# has never run anywhere but a laptop, because it had no live trace source and
# no schedule. It has both now — the source is the Phoenix container app in
# `infra/terraform/observability.tf` and the schedule is the Container Apps job
# beside it — and a Container Apps job needs an image.
#
# It is a third image rather than a flag on the app's, and the reason is one
# line of the root `Dockerfile`: that build is `uv sync --package chip-chat-api`,
# which deliberately narrows the install to the service and what it depends on,
# and `chip-chat-eval` is not among them. Nothing in `api/` imports `eval` and
# the dependency runs one way only — so putting the monitors in the app image
# would mean shipping the whole eval tree to every web replica in order to run
# it nowhere.
#
# Build from the REPOSITORY ROOT, because the lockfile is at the root:
#
#   make monitors-image-push ACR_LOGIN_SERVER=...   # az acr build, no local daemon
#   docker build -f eval/Dockerfile -t chip-chat-monitors:dev .
#
# Everything is pinned, for the same reason `agent/Dockerfile` is: a base image
# that changes underneath you is not a base image.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.12.6
ARG DEBIAN_RELEASE=trixie

# --- Build ------------------------------------------------------------------

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS build

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src
COPY . .

# --frozen  the committed uv.lock is the build input; a build that quietly
#           re-resolves is a build whose output you cannot reproduce.
# --package chip-chat-eval, which pulls agent, api, catalog, harvest, otel,
#           search and vision behind it — the offline evals score the real
#           components rather than copies, and the online loop reuses their
#           readers, so the same tree is what makes a live number comparable to
#           a BASELINE.md one.
# --no-dev  ruff, mypy and pytest are gates, not runtime.
#
# No `--mount=type=cache` here, unlike `agent/Dockerfile`, and it is not an
# oversight: that is BuildKit syntax, and the supported way to build this
# without a local Docker daemon is `az acr build`, whose classic builder does
# not have BuildKit. A cache mount would make this file build on a laptop and
# fail in the registry, which is the worst of the three possible outcomes.
RUN uv sync --frozen --no-dev --no-editable --package chip-chat-eval

# --- Runtime ----------------------------------------------------------------

FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS runtime

# Not root. This container reads spans over HTTP and calls a model; it writes
# nothing to its own filesystem and has no business being able to.
RUN useradd --create-home --uid 10001 cilantro

COPY --from=build --chown=cilantro:cilantro /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER cilantro
WORKDIR /home/cilantro

# The default is the free one, so a `docker run` with no arguments and no
# credentials prints the policy, the six monitors and the budget line rather
# than failing at a backend it was not told about. The scheduled job overrides
# the arguments with --phoenix and a window; see observability.tf.
ENTRYPOINT ["python", "-m", "chip_chat.eval.online"]
CMD ["--check"]
