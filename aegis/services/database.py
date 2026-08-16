"""Async database engine, session management, and repository helpers.

The previous implementation opened synchronous SQLAlchemy sessions from inside
``async def`` handlers. Every query blocked the event loop for its full
duration, so one slow query stalled *every* concurrent request on that worker -
a failure mode that only appears under load, which is exactly when an incident
response tool must not fall over.

This module uses ``asyncpg`` end to end. Two connection pools exist by design:

* **SQLAlchemy async pool** (here) - ORM work: users, sessions, incidents.
* **psycopg async pool** (:mod:`aegis.core.langgraph.graph`) - LangGraph's
  checkpointer, which requires psycopg specifically.

They are kept separate rather than shared because the checkpointer needs
``autocommit`` and no prepared-statement caching, settings that would be wrong
for ORM traffic.
"""

from __future__ import annotations

from collections.abc import (
    AsyncGenerator,
    Sequence,
)
from contextlib import asynccontextmanager
from typing import Any
import uuid

from sqlalchemy import (
    func,
    select,
    text,
)
from sqlalchemy.exc import (
    IntegrityError,
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aegis.core.config import settings
from aegis.core.exceptions import (
    ConflictError,
    DatabaseError,
    ResourceNotFoundError,
)
from aegis.core.logging import logger
from aegis.core.metrics import db_pool_connections
from aegis.models.incident import (
    Incident,
    IncidentStatus,
    InvestigationReport,
)
from aegis.models.user import (
    Session,
    User,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use.

    Lazy creation matters: building the engine at import time would bind the
    pool to whichever event loop happened to import the module, which breaks
    under pytest-asyncio where each test may get a fresh loop.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.async_postgres_dsn,
            echo=settings.POSTGRES_ECHO,
            pool_size=settings.POSTGRES_POOL_SIZE,
            max_overflow=settings.POSTGRES_MAX_OVERFLOW,
            pool_timeout=settings.POSTGRES_POOL_TIMEOUT,
            # Recycle before typical proxy/firewall idle timeouts so we never
            # hand out a connection the network has already dropped.
            pool_recycle=settings.POSTGRES_POOL_RECYCLE,
            # Cheap liveness check on checkout; turns a hard failure into a
            # transparent reconnect after a database restart or failover.
            pool_pre_ping=True,
            connect_args={
                "server_settings": {
                    "application_name": f"aegis-{settings.ENVIRONMENT.value}",
                    # Bound how long a single statement may hold a connection.
                    "statement_timeout": str(int(settings.REQUEST_TIMEOUT_SECONDS * 1000)),
                }
            },
        )
        logger.info(
            "database_engine_created",
            pool_size=settings.POSTGRES_POOL_SIZE,
            max_overflow=settings.POSTGRES_MAX_OVERFLOW,
            host=settings.POSTGRES_HOST,
            database=settings.POSTGRES_DB,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the configured async session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            # Objects stay usable after commit. Without this, touching any
            # attribute post-commit triggers a lazy refresh - which raises in
            # async code because the implicit IO has nowhere to await.
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


# Exceptions that mean "the database is unreachable or misbehaving" rather than
# "this request was wrong". OSError is included because asyncpg raises a bare
# ConnectionRefusedError/OSError when it cannot open a socket, and SQLAlchemy
# does not wrap connect-time OS errors into its own hierarchy.
_DATABASE_FAILURES = (SQLAlchemyError, OSError)


async def _safe_rollback(session: AsyncSession) -> None:
    """Roll back, tolerating a connection that is already gone.

    When the failure *was* the connection dying, the rollback will fail too.
    Letting that surface would replace the original, informative error with a
    confusing secondary one.
    """
    try:
        await session.rollback()
    except Exception as exc:
        logger.debug("rollback_failed", error=str(exc))


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional session scope.

    Commits on clean exit, rolls back on any exception, and always closes.
    Infrastructure failures are translated into :class:`DatabaseError` so
    callers deal in domain errors rather than driver exceptions.

    Use this for background tasks and scripts; request handlers should depend on
    :func:`get_db_session` instead so FastAPI manages the lifecycle.

    Yields:
        An open :class:`AsyncSession`.

    Raises:
        DatabaseError: If the database is unreachable or a statement fails.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except _DATABASE_FAILURES as exc:
            await _safe_rollback(session)
            logger.error("database_operation_failed", error_type=type(exc).__name__, error=str(exc))
            raise DatabaseError(
                "The database is currently unavailable.",
                context={"error_type": type(exc).__name__, "error": str(exc)},
            ) from exc
        except Exception:
            await _safe_rollback(session)
            raise


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session.

    Deliberately does **not** commit automatically. Endpoints commit explicitly
    so the transaction boundary is visible at the call site rather than being an
    invisible side effect of returning.

    Exceptions raised by the endpoint propagate back into this generator at the
    ``yield``, which is where driver failures are translated into
    :class:`DatabaseError` - and from there into a 503 problem document.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except _DATABASE_FAILURES as exc:
            await _safe_rollback(session)
            logger.error("database_operation_failed", error_type=type(exc).__name__, error=str(exc))
            raise DatabaseError(
                "The database is currently unavailable.",
                context={"error_type": type(exc).__name__, "error": str(exc)},
            ) from exc
        except Exception:
            await _safe_rollback(session)
            raise


async def dispose_engine() -> None:
    """Close all pooled connections. Called during application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("database_engine_disposed")
    _engine = None
    _session_factory = None


async def check_database_health() -> dict[str, Any]:
    """Probe database reachability and report pool saturation.

    Returns:
        A dict with ``healthy``, plus pool statistics when available. Never
        raises - health endpoints must report failure, not become failure.
    """
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))

        pool = get_engine().pool
        stats: dict[str, Any] = {"healthy": True}
        # Not every pool implementation exposes these (NullPool under tests).
        if hasattr(pool, "checkedout"):
            in_use = pool.checkedout()
            idle = pool.checkedin() if hasattr(pool, "checkedin") else 0
            db_pool_connections.labels(pool="orm", state="in_use").set(in_use)
            db_pool_connections.labels(pool="orm", state="idle").set(idle)
            stats["connections_in_use"] = in_use
            stats["connections_idle"] = idle
        return stats
    except Exception as exc:
        logger.error("database_health_check_failed", error=str(exc))
        return {"healthy": False, "error": str(exc)}


async def check_pgvector_available() -> bool:
    """Verify the ``vector`` extension is installed.

    Retrieval is dead without it, and the failure otherwise surfaces as a
    confusing syntax error on the first search rather than at startup.
    """
    try:
        async with session_scope() as session:
            result = await session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            return result.first() is not None
    except Exception as exc:
        logger.error("pgvector_check_failed", error=str(exc))
        return False


class UserRepository:
    """Data access for users and their sessions.

    Repositories take an ``AsyncSession`` rather than holding one. That keeps
    the transaction boundary owned by the caller, so several repositories can
    participate in a single atomic unit of work.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to an active session."""
        self.session = session

    async def create_user(self, email: str, password: str, full_name: str = "") -> User:
        """Create a user with a bcrypt-hashed password.

        Args:
            email: Login email; stored lowercased.
            password: Plaintext password, hashed before persisting.
            full_name: Optional display name.

        Returns:
            The persisted user.

        Raises:
            ConflictError: If the email is already registered.
        """
        user = User(
            email=email.lower().strip(),
            hashed_password=User.hash_password(password),
            full_name=full_name,
        )
        self.session.add(user)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            # Relying on the unique constraint rather than a pre-check avoids the
            # race where two concurrent registrations both see "not taken".
            raise ConflictError(
                "An account with this email already exists.",
                context={"email": email},
            ) from exc
        logger.info("user_created", user_id=str(user.id), email=user.email)
        return user

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        """Fetch a user by primary key."""
        return await self.session.get(User, user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        """Fetch a user by email, case-insensitively."""
        result = await self.session.execute(select(User).where(User.email == email.lower().strip()))
        return result.scalar_one_or_none()

    async def require_user(self, user_id: uuid.UUID) -> User:
        """Fetch a user or raise :class:`ResourceNotFoundError`."""
        user = await self.get_user(user_id)
        if user is None:
            raise ResourceNotFoundError("User", user_id)
        return user

    async def create_session(self, user_id: uuid.UUID, title: str = "") -> Session:
        """Open a new conversation session for a user."""
        chat_session = Session(user_id=user_id, title=title)
        self.session.add(chat_session)
        await self.session.flush()
        logger.info("session_created", session_id=str(chat_session.id), user_id=str(user_id))
        return chat_session

    async def get_session(self, session_id: uuid.UUID) -> Session | None:
        """Fetch a session by id."""
        return await self.session.get(Session, session_id)

    async def require_session(self, session_id: uuid.UUID) -> Session:
        """Fetch a session or raise :class:`ResourceNotFoundError`."""
        chat_session = await self.get_session(session_id)
        if chat_session is None:
            raise ResourceNotFoundError("Session", session_id)
        return chat_session

    async def list_sessions(self, user_id: uuid.UUID, include_archived: bool = False) -> Sequence[Session]:
        """List a user's sessions, newest first."""
        statement = select(Session).where(Session.user_id == user_id)
        if not include_archived:
            statement = statement.where(Session.is_archived == False)  # noqa: E712
        statement = statement.order_by(Session.created_at.desc())
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def delete_session(self, session_id: uuid.UUID) -> bool:
        """Delete a session. Returns False when it did not exist."""
        chat_session = await self.get_session(session_id)
        if chat_session is None:
            return False
        await self.session.delete(chat_session)
        logger.info("session_deleted", session_id=str(session_id))
        return True

    async def touch_session(self, session_id: uuid.UUID, message_delta: int = 1) -> None:
        """Bump a session's message counter.

        Uses an in-place SQL increment rather than read-modify-write so two
        concurrent messages cannot lose an update.
        """
        chat_session = await self.get_session(session_id)
        if chat_session is not None:
            chat_session.message_count = Session.message_count + message_delta  # type: ignore[assignment]
            self.session.add(chat_session)


class IncidentRepository:
    """Data access for incidents and their investigation reports."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to an active session."""
        self.session = session

    async def create_incident(self, **fields: Any) -> Incident:
        """Create an incident from keyword fields."""
        incident = Incident(**fields)
        self.session.add(incident)
        await self.session.flush()
        logger.info(
            "incident_created",
            incident_id=str(incident.id),
            severity=incident.severity.value,
            service=incident.service,
        )
        return incident

    async def get_incident(self, incident_id: uuid.UUID) -> Incident | None:
        """Fetch an incident by id."""
        return await self.session.get(Incident, incident_id)

    async def require_incident(self, incident_id: uuid.UUID) -> Incident:
        """Fetch an incident or raise :class:`ResourceNotFoundError`."""
        incident = await self.get_incident(incident_id)
        if incident is None:
            raise ResourceNotFoundError("Incident", incident_id)
        return incident

    async def list_incidents(
        self,
        *,
        status: IncidentStatus | None = None,
        service: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Incident], int]:
        """List incidents with optional filters.

        Returns:
            A ``(rows, total)`` tuple. ``total`` ignores pagination so clients
            can render "showing 1-50 of 231" without a second round trip.
        """
        statement = select(Incident)
        count_statement = select(func.count()).select_from(Incident)

        if status is not None:
            statement = statement.where(Incident.status == status)
            count_statement = count_statement.where(Incident.status == status)
        if service:
            statement = statement.where(Incident.service == service)
            count_statement = count_statement.where(Incident.service == service)

        statement = statement.order_by(Incident.created_at.desc()).limit(limit).offset(offset)

        rows = (await self.session.execute(statement)).scalars().all()
        total = (await self.session.execute(count_statement)).scalar_one()
        return rows, total

    async def save_report(self, report: InvestigationReport) -> InvestigationReport:
        """Persist an investigation report."""
        self.session.add(report)
        await self.session.flush()
        logger.info(
            "investigation_report_saved",
            report_id=str(report.id),
            incident_id=str(report.incident_id) if report.incident_id else None,
            confidence=report.confidence,
        )
        return report

    async def list_reports(self, incident_id: uuid.UUID) -> Sequence[InvestigationReport]:
        """List an incident's reports, newest first."""
        result = await self.session.execute(
            select(InvestigationReport)
            .where(InvestigationReport.incident_id == incident_id)
            .order_by(InvestigationReport.created_at.desc())
        )
        return result.scalars().all()

    async def resolved_incidents_for_context(self, service: str | None, limit: int = 5) -> Sequence[Incident]:
        """Fetch recent resolved incidents with a recorded root cause.

        These are the highest-value precedents to show the agent: a past
        incident on the same service whose true cause a human confirmed.
        """
        statement = (
            select(Incident)
            .where(Incident.status.in_([IncidentStatus.RESOLVED, IncidentStatus.CLOSED]))
            .where(Incident.root_cause.is_not(None))
        )
        if service:
            statement = statement.where(Incident.service == service)
        statement = statement.order_by(Incident.resolved_at.desc()).limit(limit)
        result = await self.session.execute(statement)
        return result.scalars().all()


async def execute_with_error_handling(session: AsyncSession, statement: Any) -> Any:
    """Run a statement, converting driver errors into :class:`DatabaseError`.

    Keeps SQLAlchemy exception types from leaking into API layers that should
    only know about domain errors.
    """
    try:
        return await session.execute(statement)
    except SQLAlchemyError as exc:
        logger.error("database_query_failed", error=str(exc), exc_info=True)
        raise DatabaseError("A database operation failed.", context={"error": str(exc)}) from exc


__all__ = [
    "IncidentRepository",
    "UserRepository",
    "check_database_health",
    "check_pgvector_available",
    "dispose_engine",
    "execute_with_error_handling",
    "get_db_session",
    "get_engine",
    "get_session_factory",
    "session_scope",
]
