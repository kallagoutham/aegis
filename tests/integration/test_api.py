"""API-level tests that exercise the real app without a database.

The database dependency is overridden rather than mocked away entirely, so these
tests still cover routing, authentication, validation, error rendering, and the
middleware stack - everything except persistence.

Tests that genuinely need PostgreSQL are marked ``integration`` and live in
``test_database.py``.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
import pytest

from aegis.main import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """A test client over the real application.

    ``raise_server_exceptions=False`` lets the registered exception handlers run
    and produce a response, which is what we want to assert on. The default
    re-raises instead, so a handler bug would surface as a test error rather
    than as the 500 a real client would receive.
    """
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestMetaEndpoints:
    """Unauthenticated service endpoints."""

    def test_root_reports_service_identity(self, client):
        response = client.get("/")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Aegis"
        assert body["api"] == "/api/v1"

    def test_liveness_does_not_touch_dependencies(self, client):
        # Must succeed even with no database configured.
        response = client.get("/api/v1/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    def test_metrics_endpoint_is_mounted(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "aegis_http_requests_total" in response.text

    def test_openapi_schema_is_served(self, client):
        response = client.get("/api/v1/openapi.json")
        assert response.status_code == 200
        assert "paths" in response.json()


class TestRequestCorrelation:
    """Request id propagation."""

    def test_response_carries_a_request_id(self, client):
        response = client.get("/")
        assert response.headers.get("X-Request-ID")

    def test_inbound_request_id_is_preserved(self, client):
        response = client.get("/", headers={"X-Request-ID": "trace-from-upstream"})
        assert response.headers["X-Request-ID"] == "trace-from-upstream"

    def test_each_request_gets_a_distinct_id(self, client):
        first = client.get("/").headers["X-Request-ID"]
        second = client.get("/").headers["X-Request-ID"]
        assert first != second

    def test_server_timing_header_is_present(self, client):
        assert client.get("/").headers.get("X-Response-Time-Ms")


class TestSecurityHeaders:
    """Baseline hardening headers."""

    @pytest.mark.parametrize(
        "header,expected",
        [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ],
    )
    def test_header_is_set(self, client, header, expected):
        assert client.get("/").headers.get(header) == expected

    def test_content_security_policy_is_restrictive(self, client):
        assert "default-src 'none'" in client.get("/").headers.get("Content-Security-Policy", "")


class TestAuthenticationRequired:
    """Protected endpoints reject unauthenticated callers."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/v1/auth/me"),
            ("get", "/api/v1/auth/sessions"),
            ("post", "/api/v1/auth/sessions"),
            ("get", "/api/v1/incidents"),
            ("post", "/api/v1/incidents"),
            ("post", "/api/v1/incidents/investigate"),
            ("post", "/api/v1/knowledge/search"),
            ("get", "/api/v1/knowledge/stats"),
            ("post", "/api/v1/chat"),
        ],
    )
    def test_missing_token_is_rejected(self, client, method, path):
        # httpx's .get() takes no body, so only POST carries one.
        response = client.post(path, json={}) if method == "post" else client.get(path)
        assert response.status_code in (401, 403), f"{method.upper()} {path} returned {response.status_code}"

    def test_malformed_token_is_rejected(self, client):
        response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
        assert response.status_code == 401

    def test_session_token_cannot_access_user_endpoints(self, client):
        # The token-confusion guard, exercised through the real dependency stack.
        from aegis.utils.auth import create_session_token

        token = create_session_token(uuid.uuid4())
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token.access_token}"})
        assert response.status_code == 401


class TestErrorFormat:
    """Every error is an RFC 9457 problem document."""

    def test_unknown_route_returns_problem_json(self, client):
        response = client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert {"type", "title", "status", "code", "detail"} <= set(body)

    def test_auth_error_includes_request_id(self, client):
        body = client.get("/api/v1/auth/me").json()
        assert "request_id" in body

    def test_validation_error_lists_offending_fields(self, client):
        response = client.post("/api/v1/auth/register", json={"email": "a@b.com"})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "validation_error"
        assert any(error["field"] == "password" for error in body["errors"])

    def test_database_outage_is_a_retryable_503_not_a_500(self, client):
        # No PostgreSQL is running in this suite, so any endpoint whose auth
        # dependency reads the user row exercises the database error path.
        from aegis.utils.auth import create_user_token

        token = create_user_token(uuid.uuid4())
        response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token.access_token}"})
        assert response.status_code == 503
        assert response.json()["code"] == "database_unavailable"
        assert response.headers.get("Retry-After")


class TestRegistrationValidation:
    """Registration input rules, exercised end to end."""

    def test_weak_password_is_rejected(self, client):
        response = client.post(
            "/api/v1/auth/register",
            json={"email": "responder@example.com", "password": "short"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    def test_malformed_email_is_rejected(self, client):
        response = client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "password": "a-perfectly-fine-passphrase"},
        )
        assert response.status_code == 422


class TestCors:
    """Cross-origin configuration."""

    def test_preflight_is_answered_for_allowed_origin(self, client):
        response = client.options(
            "/api/v1/health/live",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code in (200, 204)
        assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"
