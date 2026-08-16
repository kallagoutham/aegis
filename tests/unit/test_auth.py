"""Tests for token issuing and verification.

The token-confusion tests are the important ones here: they pin the fix for a
real vulnerability in which a session token and a user token were structurally
interchangeable.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
    timedelta,
)
import uuid

from jose import jwt
import pytest

from aegis.core.config import settings
from aegis.core.exceptions import AuthenticationError
from aegis.schemas.auth import (
    TokenType,
    UserCreate,
)
from aegis.utils.auth import (
    create_session_token,
    create_user_token,
    verify_token,
)


class TestTokenIssuing:
    """Minting tokens."""

    def test_user_token_round_trips(self):
        user_id = uuid.uuid4()
        token = create_user_token(user_id)
        claims = verify_token(token.access_token, TokenType.USER)
        assert claims.subject == user_id
        assert claims.token_type is TokenType.USER

    def test_session_token_round_trips(self):
        session_id = uuid.uuid4()
        token = create_session_token(session_id)
        claims = verify_token(token.access_token, TokenType.SESSION)
        assert claims.subject == session_id

    def test_expiry_is_in_the_future(self):
        token = create_user_token(uuid.uuid4())
        assert token.expires_at > datetime.now(UTC)

    def test_session_tokens_live_longer_than_user_tokens(self):
        user_token = create_user_token(uuid.uuid4())
        session_token = create_session_token(uuid.uuid4())
        assert session_token.expires_at > user_token.expires_at

    def test_each_token_has_a_unique_id(self):
        first = create_user_token(uuid.uuid4())
        second = create_user_token(uuid.uuid4())
        assert (
            verify_token(first.access_token, TokenType.USER).jti
            != verify_token(second.access_token, TokenType.USER).jti
        )


class TestTokenConfusion:
    """A token issued for one purpose must not work for another."""

    def test_session_token_rejected_by_user_verification(self):
        token = create_session_token(uuid.uuid4())
        with pytest.raises(AuthenticationError):
            verify_token(token.access_token, TokenType.USER)

    def test_user_token_rejected_by_session_verification(self):
        token = create_user_token(uuid.uuid4())
        with pytest.raises(AuthenticationError):
            verify_token(token.access_token, TokenType.SESSION)


class TestTokenVerificationFailures:
    """Rejection paths."""

    def test_garbage_is_rejected(self):
        with pytest.raises(AuthenticationError):
            verify_token("not-a-jwt", TokenType.USER)

    def test_empty_string_is_rejected(self):
        with pytest.raises(AuthenticationError):
            verify_token("", TokenType.USER)

    def test_token_signed_with_another_key_is_rejected(self):
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "user",
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            "a-completely-different-signing-key",
            algorithm="HS256",
        )
        with pytest.raises(AuthenticationError):
            verify_token(forged, TokenType.USER)

    def test_expired_token_is_rejected(self):
        past = datetime.now(UTC) - timedelta(hours=2)
        expired = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "user",
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "iat": int(past.timestamp()),
                "exp": int((past + timedelta(minutes=1)).timestamp()),
            },
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AuthenticationError):
            verify_token(expired, TokenType.USER)

    def test_wrong_issuer_is_rejected(self):
        # A token minted by a different service that happens to share the secret.
        foreign = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "user",
                "iss": "some-other-service",
                "aud": settings.JWT_AUDIENCE,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AuthenticationError):
            verify_token(foreign, TokenType.USER)

    def test_wrong_audience_is_rejected(self):
        foreign = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "typ": "user",
                "iss": settings.JWT_ISSUER,
                "aud": "a-different-api",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AuthenticationError):
            verify_token(foreign, TokenType.USER)

    def test_non_uuid_subject_is_rejected(self):
        malformed = jwt.encode(
            {
                "sub": "not-a-uuid",
                "typ": "user",
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AuthenticationError):
            verify_token(malformed, TokenType.USER)

    def test_missing_type_claim_is_rejected(self):
        # Exactly the shape the old implementation produced.
        legacy = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(AuthenticationError):
            verify_token(legacy, TokenType.USER)


class TestPasswordHashing:
    """bcrypt handling."""

    def test_hash_verifies(self):
        from aegis.models.user import User

        digest = User.hash_password("correct horse battery staple")
        user = User(email="a@b.com", hashed_password=digest)
        assert user.verify_password("correct horse battery staple")

    def test_wrong_password_fails(self):
        from aegis.models.user import User

        user = User(email="a@b.com", hashed_password=User.hash_password("right password here"))
        assert not user.verify_password("wrong password here")

    def test_hash_is_salted(self):
        from aegis.models.user import User

        assert User.hash_password("same input") != User.hash_password("same input")

    def test_plaintext_never_appears_in_the_digest(self):
        from aegis.models.user import User

        secret = "SuperSecretValue123!"
        assert secret not in User.hash_password(secret)

    def test_corrupt_stored_hash_fails_closed(self):
        from aegis.models.user import User

        user = User(email="a@b.com", hashed_password="not-a-bcrypt-hash")
        # Must return False, not raise: a legacy row should be a failed login,
        # not a 500.
        assert user.verify_password("anything") is False

    def test_passwords_over_72_bytes_do_not_raise(self):
        from aegis.models.user import User

        long_password = "a" * 200
        user = User(email="a@b.com", hashed_password=User.hash_password(long_password))
        assert user.verify_password(long_password)


class TestPasswordPolicy:
    """Registration password rules."""

    def test_accepts_a_strong_password(self):
        UserCreate(email="a@b.com", password="a-perfectly-fine-passphrase")

    def test_rejects_short_password(self):
        with pytest.raises(ValueError, match="at least"):
            UserCreate(email="a@b.com", password="short1!")

    def test_rejects_common_password(self):
        # Long enough to clear the length check, so the weak-password rule is
        # what actually fires.
        with pytest.raises(ValueError, match="too common"):
            UserCreate(email="a@b.com", password="administrator")

    def test_short_password_is_rejected_on_length_first(self):
        with pytest.raises(ValueError, match="at least"):
            UserCreate(email="a@b.com", password="password123")

    def test_rejects_repeated_character(self):
        with pytest.raises(ValueError, match="repeated character"):
            UserCreate(email="a@b.com", password="aaaaaaaaaaaaaaaa")

    def test_rejects_keyboard_sequence(self):
        with pytest.raises(ValueError, match="sequence"):
            UserCreate(email="a@b.com", password="myqwertypassword")

    def test_rejects_low_character_variety(self):
        with pytest.raises(ValueError, match="distinct characters"):
            UserCreate(email="a@b.com", password="ababababababab")
