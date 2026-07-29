# =============================================================================
# RiseNext Voice AI — Python deployment units
#
# One Dockerfile, one stage per deployment unit. Build with --target:
#
#   docker build -f infrastructure/docker/Dockerfile --target api   -t rn-api   .
#   docker build -f infrastructure/docker/Dockerfile --target voice -t rn-voice .
#   docker build -f infrastructure/docker/Dockerfile --target worker -t rn-worker .
#
# Build context is the REPOSITORY ROOT (uv needs the whole workspace to resolve).
#
# STATUS: Phase 0 scaffolding. The application entrypoints referenced by the CMDs
# do not exist yet, so these images build the environment but cannot start. The
# structure is committed now because the uv-in-Docker layer caching pattern is
# fiddly and worth getting right once.
# =============================================================================

# -----------------------------------------------------------------------------
# base — shared dependency layer
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.8-python3.12-bookworm-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Manifests first, source second. Dependencies change far less often than code,
# so this ordering keeps the expensive layer cached across ordinary commits.
# Every workspace member's pyproject.toml is required for uv to resolve.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml           packages/core/
COPY packages/domain/pyproject.toml         packages/domain/
COPY packages/persistence/pyproject.toml    packages/persistence/
COPY packages/providers/pyproject.toml      packages/providers/
COPY packages/services/pyproject.toml       packages/services/
COPY packages/agent/pyproject.toml          packages/agent/
COPY packages/orchestration/pyproject.toml  packages/orchestration/
COPY apps/api/pyproject.toml                apps/api/
COPY apps/voice-gateway/pyproject.toml      apps/voice-gateway/
COPY apps/worker/pyproject.toml             apps/worker/

# -----------------------------------------------------------------------------
# runtime — non-root, minimal
# -----------------------------------------------------------------------------
FROM base AS runtime-base
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --home /app app

# -----------------------------------------------------------------------------
# api — control plane
# -----------------------------------------------------------------------------
FROM runtime-base AS api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package rn-api --no-install-workspace
COPY packages/ packages/
COPY apps/api/ apps/api/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package rn-api
USER app
EXPOSE 8000
# Multiple workers: the control plane is stateless, so processes are free.
CMD ["uvicorn", "rn_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

# -----------------------------------------------------------------------------
# voice — media plane
# -----------------------------------------------------------------------------
FROM runtime-base AS voice
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package rn-voice --no-install-workspace
COPY packages/ packages/
COPY apps/voice-gateway/ apps/voice-gateway/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package rn-voice
USER app
EXPOSE 8080
# ONE worker process. Live calls are pinned to the process holding their sockets,
# so forking workers would fragment connection ownership and capacity accounting.
# Scale by adding CONTAINERS, not workers. See docs/SCALABILITY.md.
#
# The grace period must exceed the longest call we allow: draining means "stop
# accepting new calls and let the running ones finish", and a call can run for
# tens of minutes. A short grace period cuts customers off mid-sentence.
STOPSIGNAL SIGTERM
CMD ["uvicorn", "rn_voice.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-graceful-shutdown", "1800"]

# -----------------------------------------------------------------------------
# worker — processing plane. Same image serves the scheduler via a different CMD.
# -----------------------------------------------------------------------------
FROM runtime-base AS worker
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package rn-worker --no-install-workspace
COPY packages/ packages/
COPY apps/worker/ apps/worker/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package rn-worker
USER app
# Scheduler override (EXACTLY ONE active replica — two schedulers means duplicate
# real phone calls; leadership is a Postgres advisory lock on a direct connection):
#   command: ["taskiq", "scheduler", "rn_worker.scheduler:scheduler"]
CMD ["taskiq", "worker", "rn_worker.broker:broker", "--ack-type", "when_executed"]
