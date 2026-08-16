"""User accounts and chat/investigation sessions."""

from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
)
import uuid

import bcrypt
from sqlalchemy import (
    Column,
    Index,
    String,
    UniqueConstraint,
)
from sqlmodel import (
    Field,
    Relationship,
)

from aegis.core.config import settings
from aegis.models.base import (
    AegisTable,
    empty_dict,
    jsonb_column,
)

if TYPE_CHECKING:
    from aegis.models.incident import Incident


class UserRole(str, Enum):
    """Coarse permission tiers.

    Deliberately coarse: fine-grained permissions belong in an authorization
    service, not baked into the user row where every change needs a migration.

    * ``VIEWER``    - read incidents and search the knowledge base.
    * ``RESPONDER`` - the default; run investigations, create incidents.
    * ``ADMIN``     - additionally manage the knowledge base and other users.
    """

    VIEWER = "viewer"
    RESPONDER = "responder"
    ADMIN = "admin"

    @property
    def can_ingest(self) -> bool:
        """Whether this role may write to the knowledge base."""
        return self is UserRole.ADMIN

    @property
    def can_investigate(self) -> bool:
        """Whether this role may start investigations."""
        return self in (UserRole.RESPONDER, UserRole.ADMIN)


class User(AegisTable, table=True):
    """A person who can authenticate and run investigations.

    Passwords are stored only as bcrypt hashes. The plaintext never reaches a
    field, a log, or a serialised response - :meth:`hash_password` is the sole
    entry point and it returns the digest directly.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_active_role", "is_active", "role"),
    )

    email: str = Field(
        sa_column=Column(String(320), nullable=False, unique=True, index=True),
        description="Login identifier, stored lowercased. 320 chars is the RFC 5321 maximum.",
    )
    hashed_password: str = Field(
        sa_column=Column(String(128), nullable=False),
        description="bcrypt digest. Never the plaintext.",
    )
    full_name: str = Field(default="", max_length=200, description="Display name.")
    role: UserRole = Field(default=UserRole.RESPONDER, index=True, description="Permission tier.")
    is_active: bool = Field(default=True, index=True, description="Soft-disable flag; blocks login without deleting.")
    preferences: dict[str, Any] = Field(
        default_factory=empty_dict,
        sa_column=jsonb_column(),
        description="Free-form UI/notification preferences.",
    )

    sessions: list["Session"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "lazy": "selectin"},
    )
    incidents: list["Incident"] = Relationship(
        back_populates="reporter",
        sa_relationship_kwargs={"lazy": "selectin"},
    )

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a plaintext password with bcrypt.

        Cost comes from ``BCRYPT_ROUNDS`` (12 by default, ~250ms). Tests drop it
        to 4 automatically so fixtures stay fast.

        Args:
            password: The plaintext password.

        Returns:
            The bcrypt digest, safe to persist.
        """
        # bcrypt silently truncates at 72 bytes; encoding first makes the limit
        # explicit and keeps multi-byte characters from being cut mid-sequence.
        payload = password.encode("utf-8")[:72]
        return bcrypt.hashpw(payload, bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)).decode("utf-8")

    def verify_password(self, password: str) -> bool:
        """Check a plaintext password against the stored digest.

        ``bcrypt.checkpw`` is constant-time with respect to the digest, so this
        does not leak information through timing.

        Args:
            password: Candidate plaintext.

        Returns:
            True when the password matches.
        """
        try:
            return bcrypt.checkpw(password.encode("utf-8")[:72], self.hashed_password.encode("utf-8"))
        except ValueError:
            # Raised when the stored value is not a valid bcrypt hash, e.g. a row
            # written by an older scheme. Treat as a failed login, not a 500.
            return False


class Session(AegisTable, table=True):
    """A conversation thread between one user and the agent.

    The session id doubles as the LangGraph checkpoint ``thread_id``, which is
    what gives a conversation durable memory across processes: message history
    lives in the checkpoint tables keyed by this id.
    """

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_created", "user_id", "created_at"),)

    user_id: uuid.UUID = Field(foreign_key="users.id", index=True, nullable=False, ondelete="CASCADE")
    title: str = Field(default="", max_length=200, description="Human label, auto-derived from the first message.")
    is_archived: bool = Field(default=False, index=True)
    message_count: int = Field(default=0, ge=0, description="Denormalised counter to avoid counting checkpoints.")
    session_metadata: dict[str, Any] = Field(
        default_factory=empty_dict,
        sa_column=jsonb_column(),
        description="Client-supplied context (originating alert, Slack thread, and so on).",
    )

    user: User = Relationship(back_populates="sessions")

    @property
    def thread_id(self) -> str:
        """The LangGraph checkpoint thread id for this session."""
        return str(self.id)


__all__ = ["Session", "User", "UserRole"]
