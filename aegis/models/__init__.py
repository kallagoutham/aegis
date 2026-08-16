"""SQLModel table definitions for Aegis.

Importing this package registers every table on ``SQLModel.metadata``, which is
what Alembic's autogenerate reads. Any new table must be imported here or
migrations will silently omit it.
"""

from aegis.models.base import (
    AegisTable,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    utcnow,
)
from aegis.models.incident import (
    Incident,
    IncidentStatus,
    InvestigationReport,
    Severity,
)
from aegis.models.knowledge import (
    Document,
    DocumentChunk,
    IngestionStatus,
    SourceType,
)
from aegis.models.user import (
    Session,
    User,
    UserRole,
)

__all__ = [
    "AegisTable",
    "Document",
    "DocumentChunk",
    "Incident",
    "IncidentStatus",
    "IngestionStatus",
    "InvestigationReport",
    "Session",
    "Severity",
    "SourceType",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "User",
    "UserRole",
    "utcnow",
]
