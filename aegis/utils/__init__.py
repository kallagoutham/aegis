"""Shared utilities: authentication and input validation."""

from aegis.utils.auth import (
    TokenClaims,
    create_session_token,
    create_token,
    create_user_token,
    verify_token,
)
from aegis.utils.sanitization import (
    escape_html,
    looks_like_shell_injection,
    normalise_email,
    redact_secrets,
    reject_control_characters,
    strip_control_characters,
    truncate,
    validate_identifier,
)

__all__ = [
    "TokenClaims",
    "create_session_token",
    "create_token",
    "create_user_token",
    "escape_html",
    "looks_like_shell_injection",
    "normalise_email",
    "redact_secrets",
    "reject_control_characters",
    "strip_control_characters",
    "truncate",
    "validate_identifier",
    "verify_token",
]
