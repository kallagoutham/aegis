"""Rate limiting.

Limits are keyed by authenticated subject where possible, falling back to client
IP. Keying purely on IP - as the previous implementation did - means every user
behind one corporate NAT or one Kubernetes egress gateway shares a single
bucket, so the first busy user locks out the rest of the team. During an
incident that is precisely the wrong failure.

Limits are in-process. That is honest for a single-instance deployment and
approximate across replicas: N replicas permit roughly N times the configured
rate. For strict global limits, point slowapi at Redis via ``storage_uri`` - the
knob is left here rather than hidden.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from aegis.core.config import settings


def rate_limit_key(request: Request) -> str:
    """Derive the rate limit bucket for a request.

    Prefers the authenticated subject so limits are per-user rather than per-IP.
    ``request.state.subject_id`` is set by the auth dependency, which runs after
    this for the first request of a connection - hence the IP fallback rather
    than an error.

    Args:
        request: The incoming request.

    Returns:
        A bucket key.
    """
    subject = getattr(request.state, "subject_id", None)
    if subject:
        return f"user:{subject}"

    # X-Forwarded-For is trusted only because this service is expected to run
    # behind a proxy that overwrites it. Exposed directly to the internet, a
    # client could forge the header and evade limits entirely.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"

    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=settings.RATE_LIMIT_DEFAULT,
    enabled=settings.RATE_LIMIT_ENABLED,
    headers_enabled=True,  # emit X-RateLimit-* so clients can self-throttle
)


__all__ = ["limiter", "rate_limit_key"]
