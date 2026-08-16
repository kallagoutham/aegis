"""Authentication and session management endpoints."""

from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Request,
    status,
)

from aegis.api.deps import (
    CurrentUser,
    DbSession,
    PaginationParams,
    verify_session_owner,
)
from aegis.core.config import settings
from aegis.core.exceptions import (
    AuthenticationError,
    ResourceNotFoundError,
)
from aegis.core.limiter import limiter
from aegis.core.logging import logger
from aegis.schemas.auth import (
    LoginRequest,
    PasswordChange,
    RegisterResponse,
    SessionTokenResponse,
    Token,
    UserCreate,
    UserResponse,
)
from aegis.schemas.chat import (
    SessionCreate,
    SessionResponse,
)
from aegis.services.database import UserRepository
from aegis.utils.auth import (
    create_session_token,
    create_user_token,
)
from aegis.utils.sanitization import normalise_email

router = APIRouter()


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
)
@limiter.limit(settings.rate_limit_for("register"))
async def register(request: Request, payload: UserCreate, session: DbSession) -> RegisterResponse:
    """Create an account and return an initial user token.

    The email uniqueness check is enforced by a database constraint rather than
    a pre-read, so two simultaneous registrations of the same address cannot
    both succeed.
    """
    repository = UserRepository(session)
    user = await repository.create_user(
        email=normalise_email(payload.email),
        password=payload.password.get_secret_value(),
        full_name=payload.full_name,
    )
    await session.commit()

    logger.info("user_registered", user_id=str(user.id))
    return RegisterResponse(
        user=UserResponse.model_validate(user),
        token=create_user_token(user.id),
    )


@router.post("/login", response_model=Token, summary="Exchange credentials for a user token")
@limiter.limit(settings.rate_limit_for("login"))
async def login(request: Request, payload: LoginRequest, session: DbSession) -> Token:
    """Authenticate and return a user token.

    Returns an identical error whether the account does not exist or the
    password is wrong, and performs a dummy hash comparison in the missing-user
    case. Both matter: distinct messages let an attacker enumerate registered
    addresses, and returning early without hashing would leak the same
    information through response timing.
    """
    repository = UserRepository(session)
    user = await repository.get_user_by_email(payload.email)

    if user is None:
        from aegis.models.user import User

        # Constant-ish work so a missing account takes about as long as a wrong
        # password. Without this, timing alone reveals which emails are registered.
        User.hash_password(payload.password.get_secret_value())
        raise AuthenticationError("Incorrect email or password.")

    if not user.verify_password(payload.password.get_secret_value()):
        logger.info("login_failed", user_id=str(user.id))
        raise AuthenticationError("Incorrect email or password.")

    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.")

    logger.info("login_succeeded", user_id=str(user.id))
    return create_user_token(user.id)


@router.get("/me", response_model=UserResponse, summary="Return the authenticated account")
async def read_me(user: CurrentUser) -> UserResponse:
    """Return the current user's profile."""
    return UserResponse.model_validate(user)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT, summary="Change password")
@limiter.limit(settings.rate_limit_for("login"))
async def change_password(
    request: Request,
    payload: PasswordChange,
    user: CurrentUser,
    session: DbSession,
) -> None:
    """Replace the caller's password after verifying the current one."""
    if not user.verify_password(payload.current_password.get_secret_value()):
        raise AuthenticationError("Current password is incorrect.")

    from aegis.models.user import User

    user.hashed_password = User.hash_password(payload.new_password.get_secret_value())
    session.add(user)
    await session.commit()

    # Existing tokens deliberately remain valid: this deployment has no token
    # denylist, so claiming otherwise would be misleading. Adding one is the
    # documented follow-up in docs/security.md.
    logger.info("password_changed", user_id=str(user.id))


@router.post(
    "/sessions",
    response_model=SessionTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a conversation session",
)
async def create_session(
    payload: SessionCreate,
    user: CurrentUser,
    session: DbSession,
) -> SessionTokenResponse:
    """Create a session and return a token scoped to it.

    The returned token authorises only this session, so a client can hand it to
    a narrower component without granting account-level access.
    """
    repository = UserRepository(session)
    chat_session = await repository.create_session(user.id, title=payload.title)
    if payload.metadata:
        chat_session.session_metadata = payload.metadata
        session.add(chat_session)
    await session.commit()

    return SessionTokenResponse(
        session_id=chat_session.id,
        title=chat_session.title,
        token=create_session_token(chat_session.id),
    )


@router.get("/sessions", response_model=list[SessionResponse], summary="List your sessions")
async def list_sessions(
    user: CurrentUser,
    session: DbSession,
    pagination: PaginationParams,
    include_archived: bool = False,
) -> list[SessionResponse]:
    """List the caller's conversation sessions, newest first."""
    repository = UserRepository(session)
    sessions = await repository.list_sessions(user.id, include_archived=include_archived)
    window = sessions[pagination.offset : pagination.offset + pagination.limit]
    return [SessionResponse.model_validate(entry) for entry in window]


@router.get("/sessions/{session_id}", response_model=SessionResponse, summary="Read one session")
async def read_session(session_id: uuid.UUID, user: CurrentUser, session: DbSession) -> SessionResponse:
    """Return a session the caller owns."""
    repository = UserRepository(session)
    chat_session = await repository.get_session(session_id)
    if chat_session is None:
        raise ResourceNotFoundError("Session", session_id)

    await verify_session_owner(chat_session, user.id)
    return SessionResponse.model_validate(chat_session)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a session and its history",
)
async def delete_session(session_id: uuid.UUID, user: CurrentUser, session: DbSession) -> None:
    """Delete a session, its checkpoints, and its stored conversation."""
    repository = UserRepository(session)
    chat_session = await repository.get_session(session_id)
    if chat_session is None:
        raise ResourceNotFoundError("Session", session_id)

    await verify_session_owner(chat_session, user.id)

    # Clear graph checkpoints before deleting the row. Doing it in this order
    # means a failure here leaves the session visible and retryable, rather than
    # orphaning checkpoint rows that nothing references.
    from aegis.core.langgraph.graph import get_agent

    try:
        await get_agent().clear_history(session_id)
    except Exception as exc:
        logger.warning("checkpoint_clear_failed", session_id=str(session_id), error=str(exc))

    await repository.delete_session(session_id)
    await session.commit()


__all__ = ["router"]
