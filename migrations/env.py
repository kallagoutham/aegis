"""Alembic environment configuration.

Two details differ from Alembic's default template and both matter:

* The database URL comes from :mod:`aegis.core.config`, not ``alembic.ini``.
  Duplicating credentials into a checked-in ini file is how they end up in
  version control.
* Migrations run through the async engine, because the application uses
  ``asyncpg`` and running migrations on a second, synchronous driver means the
  two can disagree about type handling.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

# Importing the models package registers every table on SQLModel.metadata,
# which is what autogenerate diffs against. Without this import, autogenerate
# would confidently produce a migration that drops every table.
import aegis.models  # noqa: F401
from aegis.core.config import settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.async_postgres_dsn)

target_metadata = SQLModel.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Filter objects out of autogenerate.

    LangGraph creates and manages its own checkpoint tables at runtime. Alembic
    would otherwise see them as unexpected and generate a migration dropping
    them, which would destroy every conversation on the next deploy.
    """
    langgraph_tables = {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    }
    if type_ == "table" and name in langgraph_tables:
        return False
    # mem0 manages its own pgvector collection table.
    if type_ == "table" and name == settings.LONG_TERM_MEMORY_COLLECTION_NAME:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Used to hand a reviewable script to a DBA in environments where the
    application has no DDL privileges.
    """
    context.configure(
        url=settings.async_postgres_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an established connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        # Detect column type and server-default changes, which Alembic ignores
        # by default and which then silently drift between code and database.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
