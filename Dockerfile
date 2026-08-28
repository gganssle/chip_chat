# The chat app's image. Built for linux/amd64 because Container Apps runs there
# and this repository is developed on Apple silicon -- see docs/deployment.md,
# which is the write-up issue #16 asks for.
#
# Two stages. The first resolves the dependency tree with uv against the
# committed lockfile; the second copies the finished virtualenv and nothing
# else, so no build tooling and no source of any package that is not needed at
# runtime ships to Azure.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.12.6

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# The whole workspace, because uv resolves against the workspace and the
# lockfile covers all of it. What is *installed* is narrowed below.
COPY pyproject.toml uv.lock ./
COPY agent/ agent/
COPY api/ api/
COPY catalog/ catalog/
COPY data-gen/ data-gen/
COPY databricks/ databricks/
COPY eval/ eval/
COPY harvest/ harvest/
COPY infra/ infra/
COPY otel/ otel/
COPY search/ search/
COPY snowflake/ snowflake/
COPY vision/ vision/
COPY web/ web/

# --package narrows the install to the API service and what it depends on:
# agent, catalog, search, snowflake, vision, web and otel. Nothing else in the
# workspace is installed, which is why the image does not carry the Databricks
# SDK.
#
# Every one of those has to be *copied* above even though only some are
# installed, because uv resolves against the workspace and a member missing from
# the context fails the resolve rather than being skipped. `catalog/` and
# `search/` were the two that had not been added: `api.drafts` prices from a
# `MenuCatalog` and `agent.lanes` names `chip_chat.search.lane`, and both
# arrived after this file was written.
#
# --locked fails rather than re-resolving. An image whose dependency tree was
# resolved at build time is an image nobody can reproduce.
#
# --no-editable installs real wheels, so the virtualenv in the next stage stands
# on its own and no source directory has to travel with it.
RUN uv sync --locked --no-dev --no-editable --package chip-chat-api

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# Nothing here needs to write to the filesystem, and the ops kill switch reads
# a path it does not own. Running as root would buy nothing.
RUN useradd --create-home --uid 10001 cilantro
USER cilantro

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHIP_CHAT_ENVIRONMENT=azure

COPY --from=build --chown=cilantro:cilantro /app/.venv /app/.venv

# The photo lane's vocabulary, generated rather than committed.
#
# RFC-001 section 07: "Every enum is generated from the live catalogue at build
# time, so the model's vocabulary cannot drift from what is orderable." That
# sentence is the reason this file is not in `vision/src/` where it would be
# convenient -- a checked-in copy is a hand-maintained list with an extra step,
# and it would be wrong on exactly the deployment where somebody re-harvested.
#
# So `make image` writes it first, out of the built catalogue in the landing
# zone, and this copies it into the installed namespace package where
# `CHIP_CHAT_VISION_VOCABULARY=chip_chat.vision_vocabulary` can import it. A
# bare `docker build .` therefore needs `make vocabulary` to have run; that is
# not an oversight, it is the build step being visible rather than implied.
#
# ARG is redeclared here because an ARG from before the FROM is out of scope in
# the stage that follows it.
ARG PYTHON_VERSION
COPY --chown=cilantro:cilantro build/vision_vocabulary.py \
     /app/.venv/lib/python${PYTHON_VERSION}/site-packages/chip_chat/vision_vocabulary.py

WORKDIR /app
EXPOSE 8000

# One worker. The spend cap's counters are process-local (api/README.md, "What
# is not here yet"), so a second worker would be a second ledger and the daily
# ceiling would mean twice what it says. Concurrency comes from the event loop,
# which is what an app that spends its time waiting on a model needs anyway.
CMD ["uvicorn", "chip_chat.api.asgi:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
