"""Shared FastAPI dependencies: authentication, authorization, and pagination.

Two distinct authenticated principals exist, and keeping them apart is a
security boundary rather than a convenience:

* :func:`current_user` - resolved from a ``user`` token. Account-level
  operations: managing sessions, reading incidents, changing a password.
* :func:`current_session` - resolved from a ``session`` token. Conversation
  operations, scoped to exactly one thread.

A session token cannot be used to enumerate the owner's other sessions, and a
user token cannot be replayed against a conversation endpoint. Both are rejected
on the ``typ`` claim before any database work happens.
"""

from __future__ import annotations

from typing import (
    Annotated,
)
import uuid

from fastapi import (
    Depends,
    Query,
    Request,
)
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
)
from aegis.core.logging import bind_context
from aegis.models.user import (
    Session,
    User,
    UserRole,
)
from aegis.schemas.auth import TokenType
from aegis.services.database import (
    UserRepository,
    get_db_session,
)
from aegis.utils.auth import verify_token

# auto_error=False so a missing header produces our own 401 problem document
# rather than FastAPI's default, keeping every error response shape consistent.
_bearer = HTTPBearer(auto_error=False, description="JWT bearer token")

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def _require_credentials(credentials: HTTPAuthorizationCredentials | None) -> str:
    """Extract the bearer token or raise."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token.")
    return credentials.credentials


async def current_user(
    request: Request,
    credentials: Credentials,
    session: DbSession,
) -> User:
    """Resolve the authenticated user from a user-scoped token.

    Raises:
        AuthenticationError: If the token is invalid, is not a user token, or
            the account no longer exists.
        AuthorizationError: If the account has been deactivated.
    """
    claims = verify_token(_require_credentials(credentials), TokenType.USER)

    repository = UserRepository(session)
    user = await repository.get_user(claims.subject)
    if user is None:
        # Valid signature, absent account: the user was deleted after the token
        # was issued. Reported as an auth failure rather than 404 so the endpoint
        # does not confirm which user ids have existed.
        raise AuthenticationError("Invalid authentication token.")

    if not user.is_active:
        raise AuthorizationError("This account has been deactivated.")

    request.state.subject_id = str(user.id)
    bind_context(user_id=str(user.id))
    return user


async def current_session(
    request: Request,
    credentials: Credentials,
    session: DbSession,
) -> Session:
    """Resolve the conversation session from a session-scoped token.

    Raises:
        AuthenticationError: If the token is invalid or not a session token.
        AuthorizationError: If the session's owner has been deactivated.
    """
    claims = verify_token(_require_credentials(credentials), TokenType.SESSION)

    repository = UserRepository(session)
    chat_session = await repository.get_session(claims.subject)
    if chat_session is None:
        raise AuthenticationError("Invalid authentication token.")

    owner = await repository.get_user(chat_session.user_id)
    if owner is None or not owner.is_active:
        raise AuthorizationError("This account has been deactivated.")

    request.state.subject_id = str(chat_session.user_id)
    bind_context(user_id=str(chat_session.user_id), session_id=str(chat_session.id))
    return chat_session


def require_role(*allowed: UserRole):
    """Build a dependency enforcing role membership.

    Args:
        *allowed: Roles permitted to proceed.

    Returns:
        A dependency that returns the user or raises
        :class:`~aegis.core.exceptions.AuthorizationError`.
    """

    async def guard(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in allowed:
            raise AuthorizationError(
                f"This operation requires one of: {', '.join(role.value for role in allowed)}.",
                context={"user_role": user.role.value},
            )
        return user

    return guard


require_admin = require_role(UserRole.ADMIN)
require_responder = require_role(UserRole.RESPONDER, UserRole.ADMIN)

CurrentUser = Annotated[User, Depends(current_user)]
CurrentSession = Annotated[Session, Depends(current_session)]
AdminUser = Annotated[User, Depends(require_admin)]
ResponderUser = Annotated[User, Depends(require_responder)]


class Pagination:
    """Offset pagination parameters with sane bounds."""

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=200, description="Rows per page.")] = 50,
        offset: Annotated[int, Query(ge=0, description="Rows to skip.")] = 0,
    ) -> None:
        """Capture pagination parameters.

        The upper bound on ``limit`` is a denial-of-service control: without it
        a single request could ask for every incident ever recorded.
        """
        self.limit = limit
        self.offset = offset


PaginationParams = Annotated[Pagination, Depends(Pagination)]


async def verify_session_owner(chat_session: Session, user_id: uuid.UUID) -> None:
    """Assert that ``user_id`` owns ``chat_session``.

    Raises:
        AuthorizationError: If ownership does not hold.
    """
    if chat_session.user_id != user_id:
        raise AuthorizationError("You do not have access to this session.")


__all__ = [
    "AdminUser",
    "Credentials",
    "CurrentSession",
    "CurrentUser",
    "DbSession",
    "Pagination",
    "PaginationParams",
    "ResponderUser",
    "current_session",
    "current_user",
    "require_admin",
    "require_responder",
    "require_role",
    "verify_session_owner",
]
