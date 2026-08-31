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
# The destination is *discovered*, not spelled, and that is the fix for #110.
# This used to be a single COPY to
# `/app/.venv/lib/python${PYTHON_VERSION}/site-packages/...`, which quietly
# assumed that PYTHON_VERSION and the virtualenv's site-packages directory are
# the same string. They are not. PYTHON_VERSION is a base-image tag and may be
# fully pinned -- `python:3.13.15-slim-bookworm` is a perfectly reasonable thing
# for somebody chasing a reproducible build to write -- while a virtualenv's
# library directory is *always* `pythonX.Y` and never carries the patch level.
# Build the deployed image with `PYTHON_VERSION=3.13.15` and COPY does not fail:
# it creates `/app/.venv/lib/python3.13.15/site-packages/chip_chat/`, a
# directory tree that is on no `sys.path` anywhere, and the build exits 0. The
# only symptom is at runtime, and it is a quiet one, because `build_photo_lane`
# treats a missing vocabulary as a reason to withdraw the photo lane rather than
# as a reason to refuse to start -- so the container comes up healthy and every
# photograph a visitor uploads is answered "matching a meal from a photo isn't
# available on this turn". Asking the interpreter where its own `purelib` is
# costs one RUN and cannot drift from the interpreter that will do the import.
#
# The `python -c "import chip_chat.vision_vocabulary"` on the last line is not
# belt-and-braces; it is the point. A silent COPY into the wrong directory
# survived code review, a build, a push and a deploy, and was found by a tester
# uploading a photograph. Nothing between the generation of that file and the
# visitor's turn had ever asserted that the module was importable. Now the build
# does, in the final stage, as the user that will run the process, against the
# interpreter on PATH -- so any future way of getting the placement wrong stops
# being a deployment that lies about being healthy and becomes a build that
# fails on the line that broke it. The generated module imports nothing but
# `enum`, which is what makes this assertion cheap enough to be unconditional.
COPY --chown=cilantro:cilantro build/vision_vocabulary.py /tmp/vision_vocabulary.py
RUN set -eu; \
    purelib="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"; \
    test -d "$purelib/chip_chat" || { \
        echo "no chip_chat namespace package under $purelib -- the venv did not copy"; \
        exit 1; }; \
    cp /tmp/vision_vocabulary.py "$purelib/chip_chat/vision_vocabulary.py"; \
    rm /tmp/vision_vocabulary.py; \
    python -c 'import chip_chat.vision_vocabulary as v; print("vocabulary:", v.__file__)'

WORKDIR /app
EXPOSE 8000

# One worker. The spend cap's counters are process-local (api/README.md, "What
# is not here yet"), so a second worker would be a second ledger and the daily
# ceiling would mean twice what it says. Concurrency comes from the event loop,
# which is what an app that spends its time waiting on a model needs anyway.
CMD ["uvicorn", "chip_chat.api.asgi:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
