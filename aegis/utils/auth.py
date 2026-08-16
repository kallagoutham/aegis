"""JWT issuing and verification.

**The vulnerability this replaces.** Previously both user tokens and session
tokens were minted by the same function, carried their subject in ``sub``, and
had nothing distinguishing them. ``get_current_session`` accepted any token
whose ``sub`` matched a session id, and ``get_current_user`` accepted any token
whose ``sub`` matched a user id. A token issued for one purpose was structurally
valid for the other - a classic token-confusion flaw.

Every token now carries a ``typ`` claim, and verification requires the caller to
state which type it expects. A session token presented to a user-scoped endpoint
is rejected on the claim, before any database lookup.

Also added: ``iss``/``aud`` verification (so tokens from another service using
the same secret are rejected), ``iat`` and ``nbf``, and a ``jti`` so individual
tokens can be revoked if a denylist is introduced later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
    timedelta,
)
import uuid

from jose import (
    JWTError,
    jwt,
)

from aegis.core.config import settings
from aegis.core.exceptions import AuthenticationError
from aegis.core.logging import logger
from aegis.schemas.auth import (
    Token,
    TokenType,
)


@dataclass(slots=True)
class TokenClaims:
    """Verified claims extracted from a token."""

    subject: uuid.UUID
    token_type: TokenType
    issued_at: datetime
    expires_at: datetime
    jti: str


def _expiry_for(token_type: TokenType) -> timedelta:
    """Return the configured lifetime for a token type."""
    minutes = (
        settings.JWT_SESSION_TOKEN_EXPIRE_MINUTES
        if token_type is TokenType.SESSION
        else settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return timedelta(minutes=minutes)


def create_token(subject: uuid.UUID | str, token_type: TokenType) -> Token:
    """Mint a signed JWT.

    Args:
        subject: The user id or session id the token authorises.
        token_type: What the token is for. Enforced at verification.

    Returns:
        The token with its expiry.

    Raises:
        AuthenticationError: If no signing key is configured. Failing here is
            deliberate - a service that silently issued unsigned tokens would be
            far worse than one that refuses to start.
    """
    secret = settings.JWT_SECRET_KEY.get_secret_value()
    if not secret:
        raise AuthenticationError(
            "JWT_SECRET_KEY is not configured; tokens cannot be issued.",
            code="configuration_error",
        )

    now = datetime.now(UTC)
    expires_at = now + _expiry_for(token_type)

    payload = {
        "sub": str(subject),
        "typ": token_type.value,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        # Unique token id, so a revocation list can target one token rather than
        # forcing a global secret rotation.
        "jti": uuid.uuid4().hex,
    }

    encoded = jwt.encode(payload, secret, algorithm=settings.JWT_ALGORITHM)
    return Token(access_token=encoded, expires_at=expires_at)


def create_user_token(user_id: uuid.UUID) -> Token:
    """Mint a token authorising account-level operations."""
    return create_token(user_id, TokenType.USER)


def create_session_token(session_id: uuid.UUID) -> Token:
    """Mint a token scoped to one conversation session."""
    return create_token(session_id, TokenType.SESSION)


def verify_token(token: str, expected_type: TokenType) -> TokenClaims:
    """Verify a token and return its claims.

    Args:
        token: The encoded JWT.
        expected_type: The token type this endpoint requires.

    Returns:
        The verified claims.

    Raises:
        AuthenticationError: If the signature, issuer, audience, expiry, type,
            or subject format is invalid. The message is intentionally generic
            for the caller while the specific reason goes to the log - telling
            an attacker *which* check failed helps them iterate.
    """
    secret = settings.JWT_SECRET_KEY.get_secret_value()
    if not secret:
        raise AuthenticationError("Authentication is not configured.", code="configuration_error")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except JWTError as exc:
        logger.info("token_verification_failed", reason=str(exc))
        raise AuthenticationError("Invalid or expired authentication token.") from exc

    declared_type = payload.get("typ")
    if declared_type != expected_type.value:
        # The security-relevant check. Reject before any database lookup.
        logger.warning(
            "token_type_mismatch",
            expected=expected_type.value,
            received=declared_type,
        )
        raise AuthenticationError("This token is not valid for the requested operation.")

    try:
        subject = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        logger.info("token_subject_malformed", subject=str(payload.get("sub"))[:64])
        raise AuthenticationError("Invalid authentication token.") from exc

    return TokenClaims(
        subject=subject,
        token_type=expected_type,
        issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        jti=str(payload.get("jti", "")),
    )


__all__ = [
    "TokenClaims",
    "create_session_token",
    "create_token",
    "create_user_token",
    "verify_token",
]
