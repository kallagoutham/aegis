"""Shared SQLModel base classes, mixins, and column types.

Conventions applied across every table:

* **UUIDv4 primary keys.** Sequential integers leak volume ("we have 412 users")
  and let anyone enumerate resources by incrementing an id. UUIDs are generated
  client-side, which also means a row's identity is known before the INSERT -
  useful when building object graphs in one transaction.
* **Timezone-aware timestamps.** ``created_at``/``updated_at`` are stored as
  ``TIMESTAMPTZ`` and always in UTC. Naive datetimes are a recurring source of
  off-by-hours incident timelines, which is exactly the bug class this product
  exists to diagnose.
* **JSONB for open-ended metadata.** Structured enough to index and query with
  containment operators, flexible enough not to need a migration per new field.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
)
from typing import Any
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import (
    Field,
    SQLModel,
)


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Used as a Python-side default so newly constructed objects carry a timestamp
    before they are flushed. The database also has its own ``now()`` default as
    a backstop for rows inserted outside the ORM.
    """
    return datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    """Generate a random UUIDv4 primary key."""
    return uuid.uuid4()


class TimestampMixin(SQLModel):
    """Adds ``created_at`` and ``updated_at`` columns.

    ``updated_at`` uses SQLAlchemy's ``onupdate`` hook, so it refreshes on every
    UPDATE without callers having to remember. Both columns are ``TIMESTAMPTZ``;
    Postgres normalises to UTC on write and we always read back UTC-aware
    values.

    Note the use of ``sa_type`` plus ``sa_column_kwargs`` rather than a fully
    built ``sa_column``. A ``Column`` object constructed in this class body
    would be created *once* and then shared by every subclass, and a Column may
    belong to exactly one Table - the second model to inherit this mixin would
    fail at import with "Column object 'created_at' already assigned". Passing
    the type and kwargs instead lets SQLModel build a fresh Column per model.
    """

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"nullable": False, "server_default": func.now()},
        index=True,
        description="When the row was created (UTC).",
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={
            "nullable": False,
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        description="When the row was last modified (UTC).",
    )


class UUIDPrimaryKeyMixin(SQLModel):
    """Adds a client-generated UUIDv4 primary key named ``id``."""

    id: uuid.UUID = Field(
        default_factory=new_uuid,
        primary_key=True,
        index=True,
        nullable=False,
        description="Primary key (UUIDv4).",
    )


class AegisTable(UUIDPrimaryKeyMixin, TimestampMixin, SQLModel):
    """Base for every persisted entity: UUID key plus timestamps.

    Subclasses still need ``table=True``; this class deliberately is not a table
    itself so SQLModel does not try to map it.
    """


def jsonb_column(nullable: bool = False) -> Column:
    """Build a JSONB column that defaults to an empty object.

    A server-side default of ``'{}'`` avoids the ``None`` vs ``{}`` ambiguity
    that otherwise forces defensive ``or {}`` at every read site.

    Args:
        nullable: Whether SQL NULL is permitted. Defaults to False.

    Returns:
        A configured SQLAlchemy ``Column``.
    """
    return Column(JSONB, nullable=nullable, server_default="{}")


def empty_dict() -> dict[str, Any]:
    """Default factory for metadata fields.

    A function rather than a literal, so every instance gets its own dict
    instead of sharing one mutable class-level default.
    """
    return {}


__all__ = [
    "AegisTable",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "empty_dict",
    "jsonb_column",
    "new_uuid",
    "utcnow",
]
