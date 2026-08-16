"""Health and readiness endpoints.

Liveness and readiness are deliberately different checks, because Kubernetes
does different things with them:

* **Liveness** answers "is this process wedged?" It must not touch external
  dependencies. A liveness probe that checks the database will restart every
  replica during a database blip - converting a recoverable dependency outage
  into a full restart storm at the worst possible moment.
* **Readiness** answers "should this replica receive traffic?" It checks the
  dependencies a request actually needs, so an unready replica is pulled from
  the load balancer while staying alive to recover.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
)
from typing import Any

from fastapi import (
    APIRouter,
    Request,
    Response,
    status,
)

from aegis.core.config import settings
from aegis.core.limiter import limiter
from aegis.services.database import (
    check_database_health,
    check_pgvector_available,
)

router = APIRouter()

_STARTED_AT = datetime.now(UTC)


@router.get("/live", summary="Liveness probe")
async def liveness() -> dict[str, Any]:
    """Report that the process is running and its event loop is responsive.

    Checks nothing external, by design.
    """
    return {
        "status": "alive",
        "uptime_seconds": round((datetime.now(UTC) - _STARTED_AT).total_seconds(), 1),
    }


@router.get("/ready", summary="Readiness probe")
async def readiness(response: Response) -> dict[str, Any]:
    """Report whether this replica can serve requests.

    Returns 503 when a hard dependency is unavailable, which removes the replica
    from the load balancer without restarting it.
    """
    database = await check_database_health()
    checks: dict[str, Any] = {
        "database": {
            "healthy": database["healthy"],
            **{key: value for key, value in database.items() if key != "healthy"},
        }
    }

    if database["healthy"]:
        # Only meaningful if the database is reachable at all.
        pgvector_ok = await check_pgvector_available()
        checks["pgvector"] = {
            "healthy": pgvector_ok,
            "detail": "extension installed" if pgvector_ok else "vector extension missing; retrieval disabled",
        }

    checks["llm"] = {
        "healthy": bool(settings.OPENAI_API_KEY.get_secret_value()),
        "detail": "api key configured" if settings.OPENAI_API_KEY.get_secret_value() else "OPENAI_API_KEY unset",
    }

    ready = all(check["healthy"] for check in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "not_ready",
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT.value,
        "checks": checks,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("", summary="Aggregate health")
@limiter.limit(settings.rate_limit_for("health"))
async def health(request: Request, response: Response) -> dict[str, Any]:
    """Return a combined health view for dashboards and manual checks."""
    readiness_result = await readiness(response)
    return {
        **readiness_result,
        "uptime_seconds": round((datetime.now(UTC) - _STARTED_AT).total_seconds(), 1),
    }


__all__ = ["router"]
