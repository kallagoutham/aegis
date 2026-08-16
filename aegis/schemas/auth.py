"""Authentication schemas and password policy.

Password rules follow NIST SP 800-63B rather than the older "one of each
character class" convention: length is the dominant factor, and composition
rules mostly push users toward predictable substitutions (``Password1!``). The
policy here enforces a real minimum length and screens against obviously weak
choices instead.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import re
from typing import Annotated
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
)

from aegis.core.config import settings
from aegis.models.user import UserRole

# Common passwords and product-specific guesses that must never be accepted,
# regardless of length. A real deployment should back this with a breach-corpus
# check (e.g. Have I Been Pwned's k-anonymity API); this is the floor.
_WEAK_PASSWORDS = {
    "password",
    "password123",
    "passw0rd",
    "letmein",
    "changeme",
    "qwerty",
    "12345678",
    "123456789",
    "1234567890",
    "iloveyou",
    "admin",
    "administrator",
    "welcome",
    "aegis",
    "aegis123",
    "incident",
    "oncall",
}

_REPEATED_RE = re.compile(r"^(.)\1+$")
_SEQUENTIAL_RE = re.compile(r"(?:abcdef|qwerty|asdfgh|zxcvbn|123456|654321)", re.IGNORECASE)


class TokenType(str, Enum):
    """Distinguishes what a token authorises.

    This is a real security control, not bookkeeping. Both user tokens and
    session tokens previously carried the subject in ``sub`` with nothing to
    tell them apart, so a token issued for one purpose could be presented for
    the other. The ``typ`` claim is verified on decode, making the two
    non-interchangeable.
    """

    USER = "user"
    SESSION = "session"


class Token(BaseModel):
    """An issued access token."""

    access_token: str = Field(..., description="The signed JWT.")
    token_type: str = Field(default="bearer")
    expires_at: datetime = Field(..., description="Expiry instant (UTC).")


class UserCreate(BaseModel):
    """Registration request."""

    email: EmailStr = Field(..., description="Login email.")
    password: SecretStr = Field(..., description="Plaintext password, validated then immediately hashed.")
    full_name: str = Field(default="", max_length=200)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: SecretStr) -> SecretStr:
        """Apply the password policy.

        Raises:
            ValueError: With a message describing what to fix. The message is
                deliberately specific - "must be at least 12 characters" is
                actionable in a way "password too weak" is not.
        """
        password = value.get_secret_value()

        if len(password) < settings.PASSWORD_MIN_LENGTH:
            raise ValueError(f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters long.")
        # bcrypt silently truncates beyond 72 bytes, so anything longer would
        # give a false sense of added strength.
        if len(password.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes long.")
        if password.lower() in _WEAK_PASSWORDS:
            raise ValueError("That password is too common. Choose something less predictable.")
        if _REPEATED_RE.match(password):
            raise ValueError("Password must not be a single repeated character.")
        if _SEQUENTIAL_RE.search(password):
            raise ValueError("Password must not contain a common keyboard or numeric sequence.")
        if len(set(password)) < 5:
            raise ValueError("Password must contain at least 5 distinct characters.")

        return value


class LoginRequest(BaseModel):
    """Login request body."""

    email: EmailStr
    password: SecretStr


class UserResponse(BaseModel):
    """A user as returned by the API. Never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime


class RegisterResponse(BaseModel):
    """Registration response: the created user plus an initial token."""

    user: UserResponse
    token: Token


class SessionTokenResponse(BaseModel):
    """A session identifier paired with its scoped token."""

    session_id: uuid.UUID
    title: str
    token: Token


class PasswordChange(BaseModel):
    """Request body for changing a password."""

    current_password: SecretStr
    new_password: Annotated[SecretStr, Field(description="Subject to the same policy as registration.")]

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, value: SecretStr) -> SecretStr:
        """Apply the registration password policy to the replacement."""
        return UserCreate._validate_password(value)


__all__ = [
    "LoginRequest",
    "PasswordChange",
    "RegisterResponse",
    "SessionTokenResponse",
    "Token",
    "TokenType",
    "UserCreate",
    "UserResponse",
]
