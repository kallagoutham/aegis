"""Aegis application entrypoint.

Assembles the FastAPI app: lifecycle, middleware, exception handlers, and
routes.

Two things here are easy to get wrong and worth reading closely:

**Middleware order.** Starlette applies middleware in reverse registration
order on the way in. The stack is registered bottom-up so that, at request time,
:class:`RequestContextMiddleware` runs *first* and every subsequent layer's logs
carry the request id.

**Error handling.** Every failure leaves through one of the handlers below and
returns an RFC 9457 ``application/problem+json`` document. Uncaught exceptions
never expose internals to the caller - the traceback goes to the log with the
request id attached, and the client gets that id to quote in a bug report.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import (
    Any,
)

from fastapi import (
    FastAPI,
    Request,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from aegis.api import api_router
from aegis.core.config import (
    Environment,
    settings,
)
from aegis.core.exceptions import AegisError
from aegis.core.limiter import limiter
from aegis.core.logging import logger
from aegis.core.metrics import setup_metrics
from aegis.core.middleware import (
    AccessLogMiddleware,
    MetricsMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from aegis.services.database import (
    check_database_health,
    check_pgvector_available,
    dispose_engine,
)

PROBLEM_JSON = "application/problem+json"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown.

    Startup verifies dependencies but does not *require* them. A replica that
    refuses to start when the database is briefly unavailable cannot report its
    own health, cannot serve ``/metrics``, and turns a transient blip into a
    crash loop. It starts, reports itself unready, and recovers when the
    dependency returns.
    """
    logger.info(
        "application_starting",
        project=settings.PROJECT_NAME,
        version=settings.VERSION,
        environment=settings.ENVIRONMENT.value,
        api_prefix=settings.API_V1_STR,
    )

    database = await check_database_health()
    if database["healthy"]:
        if await check_pgvector_available():
            logger.info("startup_check_passed", component="pgvector")
        else:
            logger.error(
                "pgvector_extension_missing",
                impact="knowledge base retrieval will fail",
                remedy="run: CREATE EXTENSION IF NOT EXISTS vector;",
            )
    else:
        logger.error("startup_check_failed", component="database", error=database.get("error"))

    if not settings.OPENAI_API_KEY.get_secret_value():
        logger.warning("openai_api_key_missing", impact="all model and embedding calls will fail")

    logger.info("application_started")

    yield

    logger.info("application_stopping")

    from aegis.core.langgraph.graph import get_agent

    try:
        await get_agent().aclose()
    except Exception as exc:
        logger.warning("agent_shutdown_failed", error=str(exc))

    await dispose_engine()
    logger.info("application_stopped")


def create_app() -> FastAPI:
    """Build and configure the application.

    A factory rather than a module-level instance so tests can construct an app
    with patched settings instead of inheriting whatever was live at import time.
    """
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=settings.DESCRIPTION,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        # Interactive docs are useful locally and an unnecessary surface in
        # production, where the OpenAPI schema is still available to clients.
        docs_url="/docs" if not settings.ENVIRONMENT.is_deployed else None,
        redoc_url="/redoc" if not settings.ENVIRONMENT.is_deployed else None,
        lifespan=lifespan,
    )

    _configure_middleware(app)
    _configure_exception_handlers(app)

    app.state.limiter = limiter
    app.include_router(api_router, prefix=settings.API_V1_STR)

    if settings.METRICS_ENABLED:
        setup_metrics(app)

    @app.get("/", tags=["meta"], summary="Service metadata")
    async def root() -> dict[str, Any]:
        """Return basic service identification."""
        return {
            "name": settings.PROJECT_NAME,
            "version": settings.VERSION,
            "environment": settings.ENVIRONMENT.value,
            "api": settings.API_V1_STR,
            "docs": "/docs" if not settings.ENVIRONMENT.is_deployed else None,
            "health": f"{settings.API_V1_STR}/health",
        }

    return app


def _configure_middleware(app: FastAPI) -> None:
    """Register the middleware stack.

    Registered in reverse execution order: the last one added runs first.
    """
    # Runs last on the way in, first on the way out.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeadersMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        # Credentialed requests plus a wildcard origin is rejected by browsers
        # anyway; config validation blocks that combination in deployed
        # environments so it fails at startup rather than in the client.
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Response-Time-Ms", "X-RateLimit-Remaining"],
        max_age=600,
    )

    app.add_middleware(MetricsMiddleware)
    app.add_middleware(AccessLogMiddleware)
    # Added last, so it runs first and every other layer sees the request id.
    app.add_middleware(RequestContextMiddleware)


def _problem_response(
    request: Request,
    *,
    status_code: int,
    title: str,
    detail: str,
    code: str,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build an RFC 9457 problem document response."""
    body: dict[str, Any] = {
        "type": f"https://docs.aegis.dev/errors/{code}",
        "title": title,
        "status": status_code,
        "code": code,
        "detail": detail,
        "instance": request.url.path,
    }
    if extra:
        body.update(extra)

    request_id = getattr(request.state, "request_id", None)
    if request_id:
        # Echoed into the body so a user can quote it without inspecting headers.
        body["request_id"] = request_id

    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_JSON)


def _configure_exception_handlers(app: FastAPI) -> None:
    """Register handlers translating exceptions into problem documents."""

    @app.exception_handler(AegisError)
    async def handle_domain_error(request: Request, exc: AegisError) -> JSONResponse:
        """Render a domain exception.

        ``exc.context`` is logged but never serialised - it routinely holds
        internal identifiers and upstream error text.
        """
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "domain_error",
            code=exc.code,
            status=exc.status_code,
            detail=exc.detail,
            **exc.context,
        )
        response = _problem_response(
            request,
            status_code=exc.status_code,
            title=exc.__class__.__name__,
            detail=exc.detail,
            code=exc.code,
        )
        # 502/503 mean a dependency is down, which is transient by nature. Tell
        # clients when to retry so they back off instead of hammering a service
        # that is already struggling.
        if exc.status_code in (502, 503):
            response.headers["Retry-After"] = "5"
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Render a request validation failure with per-field messages."""
        errors = [
            {
                # Drop the leading "body"/"query" segment; callers care about the
                # field name, not FastAPI's internal location tuple.
                "field": " -> ".join(str(part) for part in error["loc"] if part not in ("body", "query")),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        logger.info("request_validation_failed", error_count=len(errors), errors=errors)
        return _problem_response(
            request,
            # Literal rather than status.HTTP_422_*: Starlette renamed the
            # constant and the old name now emits a DeprecationWarning from
            # inside this handler, which turns every 422 into a 500.
            status_code=422,
            title="ValidationError",
            detail="The request payload failed validation.",
            code="validation_error",
            extra={"errors": errors},
        )

    @app.exception_handler(RateLimitExceeded)
    async def handle_rate_limit(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        """Render a rate limit rejection."""
        logger.warning("rate_limit_exceeded", limit=str(exc.detail))
        response = _problem_response(
            request,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            title="RateLimitExceeded",
            detail=f"Rate limit exceeded: {exc.detail}.",
            code="rate_limit_exceeded",
        )
        # Tell well-behaved clients when to come back instead of leaving them
        # to retry immediately and compound the load.
        response.headers["Retry-After"] = "60"
        return response

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        """Render a database failure as a 503 rather than an opaque 500.

        Connection refusals, pool timeouts, and failovers all arrive here. They
        are a dependency being unavailable, not a bug in the request, so the
        caller gets a retryable status and a specific code instead of a generic
        internal error. The driver's message goes to the log only - it routinely
        contains the connection string.
        """
        logger.error(
            "database_error",
            error_type=type(exc).__name__,
            error=str(exc),
            path=request.url.path,
            exc_info=True,
        )
        response = _problem_response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="DatabaseUnavailable",
            detail="The database is currently unavailable. Retry shortly.",
            code="database_unavailable",
        )
        response.headers["Retry-After"] = "5"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Render framework HTTP exceptions in the same problem format."""
        return _problem_response(
            request,
            status_code=exc.status_code,
            title="HTTPError",
            detail=str(exc.detail),
            code=f"http_{exc.status_code}",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all for unanticipated failures.

        The traceback goes to the log with the request id attached. The client
        receives a generic message plus that id - enough to report the problem,
        not enough to learn about internals.
        """
        logger.error(
            "unhandled_exception",
            error_type=type(exc).__name__,
            error=str(exc),
            path=request.url.path,
            exc_info=True,
        )
        detail = (
            f"{type(exc).__name__}: {exc}"
            if settings.ENVIRONMENT is Environment.DEVELOPMENT
            else "An unexpected error occurred. Quote the request id when reporting this."
        )
        return _problem_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            title="InternalServerError",
            detail=detail,
            code="internal_error",
        )


app = create_app()

__all__ = ["app", "create_app"]
