# syntax=docker/dockerfile:1.7
#
# Multi-stage build for Aegis.
#
# The previous single-stage image shipped build-essential, libpq-dev, and the
# full pip/uv toolchain into production - roughly 400 MB of compilers that also
# widen the attack surface. Splitting the build means the runtime image carries
# only the interpreter, the installed virtualenv, and the application.
#
# Dependencies are installed from the lockfile in a layer that depends only on
# pyproject.toml and uv.lock, so editing application code does not invalidate
# the (slow) dependency layer.

# =====================================================================
# Stage 1: build the virtualenv
# =====================================================================
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# build-essential and libpq-dev are needed only to compile wheels that have no
# prebuilt distribution for this platform. They stay in this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Dependency layer: cached until pyproject.toml or the lockfile changes.
COPY pyproject.toml uv.lock* README.md ./
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache -r pyproject.toml

# Application layer: changes on every commit, so it comes last.
COPY aegis/ ./aegis/
RUN VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-deps -e .

# =====================================================================
# Stage 2: runtime
# =====================================================================
FROM python:3.13-slim AS runtime

ARG APP_ENV=production
ARG VERSION=1.0.0

ENV APP_ENV=${APP_ENV} \
    VERSION=${VERSION} \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random

# libpq5 is the runtime library only - not the -dev headers.
# curl is required by the container HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 aegis \
    && useradd --system --uid 1001 --gid aegis --create-home aegis

WORKDIR /app

COPY --from=builder --chown=aegis:aegis /opt/venv /opt/venv
COPY --chown=aegis:aegis aegis/ ./aegis/
COPY --chown=aegis:aegis migrations/ ./migrations/
COPY --chown=aegis:aegis alembic.ini pyproject.toml ./
COPY --chown=aegis:aegis scripts/docker-entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/logs \
    && chown -R aegis:aegis /app

# Everything below runs unprivileged. A container compromise then does not
# start as root inside the namespace.
USER aegis

EXPOSE 8000

# Liveness only - it must not depend on the database, or a brief database blip
# would make the orchestrator kill every healthy replica.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/api/v1/health/live || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "aegis.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
