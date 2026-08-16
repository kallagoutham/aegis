#!/usr/bin/env bash
#
# Container entrypoint.
#
# Waits for PostgreSQL, applies migrations, then execs the given command.
#
# Migrations run here rather than in a separate init container because the
# schema and the code that reads it ship in the same image, so they cannot
# skew. For multi-replica deployments, Alembic takes an advisory lock, so
# concurrent starts serialise instead of racing.

set -euo pipefail

log() { printf '%s [entrypoint] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

DB_HOST="${POSTGRES_HOST:-db}"
DB_PORT="${POSTGRES_PORT:-5432}"
WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-60}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-true}"

log "environment: ${APP_ENV:-development}"

# ---------------------------------------------------------------------
# Wait for the database socket to accept connections.
#
# Uses a bash TCP redirect rather than pg_isready so the image does not need
# postgresql-client installed just to poll a port.
# ---------------------------------------------------------------------
log "waiting for postgres at ${DB_HOST}:${DB_PORT} (timeout ${WAIT_TIMEOUT}s)"
deadline=$(( SECONDS + WAIT_TIMEOUT ))
until (exec 3<>"/dev/tcp/${DB_HOST}/${DB_PORT}") 2>/dev/null; do
    if (( SECONDS >= deadline )); then
        log "ERROR: postgres unreachable after ${WAIT_TIMEOUT}s"
        exit 1
    fi
    sleep 1
done
exec 3<&- 2>/dev/null || true
log "postgres is accepting connections"

# ---------------------------------------------------------------------
# Apply migrations.
# ---------------------------------------------------------------------
if [[ "${RUN_MIGRATIONS}" == "true" ]]; then
    log "applying database migrations"
    if alembic upgrade head; then
        log "migrations applied"
    else
        log "ERROR: migrations failed"
        exit 1
    fi
else
    log "skipping migrations (RUN_MIGRATIONS=${RUN_MIGRATIONS})"
fi

# ---------------------------------------------------------------------
# Hand off to the container command.
#
# exec replaces this shell so the application becomes PID 1 and receives
# SIGTERM directly. Without it, orchestrator shutdown signals go to bash and
# the app is SIGKILLed after the grace period instead of draining cleanly.
# ---------------------------------------------------------------------
log "starting: $*"
exec "$@"
