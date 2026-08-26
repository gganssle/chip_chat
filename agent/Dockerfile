# syntax=docker/dockerfile:1
#
# The Cilantro agent image.
#
# Decision D8 chose a hosted agent -- our container, run by Foundry Agent
# Service -- and the decision record is blunt that this is the real bill: "an
# image, a registry, and a deployment step that has to exist before the Phase 7
# demo rather than after it." This is the image half of that.
#
# Build from the REPOSITORY ROOT, not from agent/, because the agent is a member
# of a uv workspace and its lockfile lives at the root:
#
#   docker build -f agent/Dockerfile -t chip-chat-agent:dev .
#   make agent-image                       # the same thing, with the tag filled in
#
# Everything is pinned. A base image that changes underneath you is not a base
# image, and this one is what a registered agent version points at by digest.

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
# --package only the agent and what it needs. The workspace holds eleven
#           packages and this image has business with two of them.
# --no-dev  ruff, mypy and pytest are gates, not runtime.
# --no-editable  workspace members are installed editable by default, which
#           would leave the virtualenv pointing at /src and make the runtime
#           stage below a lie. This copies them in properly.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package chip-chat-agent

# --- Runtime ----------------------------------------------------------------

FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS runtime

# Not root. Foundry runs this container; nothing in it needs to write to its own
# filesystem, and the agent's whole job is to talk to services over the network.
RUN useradd --create-home --uid 10001 cilantro

COPY --from=build --chown=cilantro:cilantro /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# service.name is forced to the agent resource's name by Foundry and
# OTEL_SERVICE_NAME is ignored, so this is what the container reports as its own
# name in order to agree with what the platform will stamp on its spans. Set it
# to the real agent resource name on the agent version. See
# chip_chat.otel.service.turn_service_names for why both names matter.
ENV CHIP_CHAT_AGENT_NAME=chip-chat-agent

USER cilantro
WORKDIR /home/cilantro

# The exporter variables are deliberately NOT baked in. They are immutable per
# agent version, they differ between the local backend and the hosted one, and
# baking them here would make the Phase 8 repoint (#78) an image rebuild rather
# than a new agent version. They arrive from the agent version's environment.

ENTRYPOINT ["python", "-m", "chip_chat.agent.container"]
CMD ["check"]
