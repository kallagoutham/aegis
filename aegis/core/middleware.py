"""HTTP middleware: request correlation, metrics, and security headers.

Ordering is significant. Starlette runs middleware in reverse registration
order on the way in, so the correlation middleware must be added *last* in
:mod:`aegis.main` to run *first* here - every other layer's logs then carry the
request id.
"""

from __future__ import annotations

from collections.abc import Callable
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from aegis.core.config import (
    Environment,
    settings,
)
from aegis.core.logging import (
    bind_context,
    clear_context,
    logger,
)
from aegis.core.metrics import (
    http_request_duration_seconds,
    http_requests_in_flight,
    http_requests_total,
    route_template,
    status_class,
)

# Paths excluded from access logging. Health and metrics endpoints are polled
# every few seconds; logging them buries real traffic in noise.
_QUIET_PATHS = frozenset({"/health", "/health/live", "/health/ready", "/metrics", "/favicon.ico"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id and binds it to the logging context.

    The id is taken from an inbound ``X-Request-ID`` when present, so a trace
    started at the load balancer or an upstream service stays connected through
    Aegis rather than restarting here. It is echoed back on the response, which
    is what lets a user reporting "my investigation failed" hand over an id that
    finds the exact log lines.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Bind request context, then guarantee it is cleared."""
        inbound = request.headers.get("x-request-id", "")
        # Cap the length: an unbounded header value would otherwise be copied
        # into every log line for this request.
        request_id = inbound[:64] if inbound else uuid.uuid4().hex

        clear_context()
        bind_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        request.state.request_id = request_id

        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            # Always clear, or a pooled worker leaks one request's identifiers
            # into the next request's logs.
            clear_context()


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emits one structured log event per request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Log the request outcome and duration."""
        started = time.perf_counter()
        quiet = request.url.path in _QUIET_PATHS

        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.error(
                "request_failed",
                status=500,
                duration_ms=round(duration_ms, 2),
                error=str(exc),
                exc_info=True,
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000

        if not quiet:
            log = logger.warning if response.status_code >= 500 else logger.info
            log(
                "request_completed",
                status=response.status_code,
                duration_ms=round(duration_ms, 2),
                client=request.client.host if request.client else None,
            )

        # Exposed so clients and load tests can see server-side time without
        # conflating it with network latency.
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records request counts, durations, and in-flight concurrency."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Instrument the request, labelling by route template."""
        started = time.perf_counter()
        http_requests_in_flight.inc()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            http_requests_in_flight.dec()
            duration = time.perf_counter() - started

            # Resolved after the handler runs, because the route is only matched
            # during dispatch. Reading it earlier yields "unmatched" for
            # everything.
            route = route_template(request)
            http_requests_total.labels(method=request.method, route=route, status=status_class(status_code)).inc()
            http_request_duration_seconds.labels(method=request.method, route=route).observe(duration)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds baseline security response headers."""

    def __init__(self, app: ASGIApp) -> None:
        """Precompute the header set, since it never varies per request."""
        super().__init__(app)
        self._headers = {
            # Stop browsers guessing content types, which is how a JSON response
            # ends up executed as HTML.
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            # This is a JSON API; it never needs camera, microphone, or geolocation.
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
            # The API serves no HTML, so the strictest possible CSP applies.
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        }
        if settings.ENVIRONMENT is Environment.PRODUCTION:
            # HSTS only in production: sending it from a local HTTP dev server
            # would pin the browser to HTTPS for localhost, which is genuinely
            # painful to undo.
            self._headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Apply security headers to the response."""
        response = await call_next(request)
        for header, value in self._headers.items():
            response.headers.setdefault(header, value)
        return response


__all__ = [
    "AccessLogMiddleware",
    "MetricsMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
]
