"""Service layer: database access and language model access.

Nothing is instantiated at import time. The database engine and the LLM service
are both created lazily on first use, so importing this package does not open a
connection pool or build an HTTP client - which matters for tests, for the CLI,
and for Alembic, none of which need both.
"""

from aegis.services.database import (
    IncidentRepository,
    UserRepository,
    check_database_health,
    check_pgvector_available,
    dispose_engine,
    get_db_session,
    get_engine,
    session_scope,
)
from aegis.services.llm import (
    LLMService,
    get_llm_service,
)

__all__ = [
    "IncidentRepository",
    "LLMService",
    "UserRepository",
    "check_database_health",
    "check_pgvector_available",
    "dispose_engine",
    "get_db_session",
    "get_engine",
    "get_llm_service",
    "session_scope",
]
