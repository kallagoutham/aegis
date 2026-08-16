"""Initial Aegis schema.

Creates the pgvector extension, all application tables, and the retrieval
indexes.

Two things are done by hand rather than left to autogenerate:

* ``CREATE EXTENSION vector`` must run before any ``vector`` column is created.
* The HNSW and GIN indexes carry operator classes and build parameters that
  Alembic's autogenerate does not emit.

Revision ID: 0001
Revises:
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Must match settings.EMBEDDING_DIMENSIONS. Hard-coded rather than imported:
# a migration records what was actually applied at the time, and must not
# silently change meaning when configuration changes later.
EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    """Create the schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Used by the trigram index below for fuzzy service-name matching.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(128), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("role", sa.String(20), nullable=False, server_default="responder"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("preferences", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_active_role", "users", ["is_active", "role"])
    op.create_index("ix_users_created_at", "users", ["created_at"])

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("session_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_user_created", "sessions", ["user_id", "created_at"])
    op.create_index("ix_sessions_is_archived", "sessions", ["is_archived"])

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False, server_default="other"),
        sa.Column("source_uri", sa.String(1024), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("service", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("doc_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_uri", "content_hash", name="uq_documents_uri_hash"),
    )
    op.create_index("ix_documents_source_uri", "documents", ["source_uri"])
    op.create_index("ix_documents_content_hash", "documents", ["content_hash"])
    op.create_index("ix_documents_type_service", "documents", ["source_type", "service"])
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_service", "documents", ["service"])
    # Trigram index so a service filter tolerates near-miss spellings
    # ("payments-api" vs "payments_api") rather than silently returning nothing.
    op.execute("CREATE INDEX ix_documents_service_trgm ON documents USING gin (service gin_trgm_ops)")

    # ------------------------------------------------------------------
    # document_chunks
    # ------------------------------------------------------------------
    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heading_path", sa.String(1000), nullable=False, server_default=""),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("source_type", sa.String(30), nullable=False, server_default="other"),
        sa.Column("service", sa.String(200), nullable=True),
        sa.Column("chunk_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunk_document_index"),
    )
    op.create_index("ix_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index("ix_chunks_type_service", "document_chunks", ["source_type", "service"])

    # HNSW over cosine distance. m=16 and ef_construction=64 are pgvector's
    # defaults: good recall at a reasonable build cost. Raise ef_construction
    # for better recall at the price of a slower build.
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
        ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )

    # GIN over the generated tsvector, backing the lexical retrieval arm.
    op.execute("CREATE INDEX ix_chunks_content_tsv ON document_chunks USING gin (content_tsv)")

    # ------------------------------------------------------------------
    # incidents
    # ------------------------------------------------------------------
    op.create_table(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("severity", sa.String(10), nullable=False, server_default="sev3"),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("service", sa.String(200), nullable=True),
        sa.Column("affected_services", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reporter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("incident_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        # SET NULL rather than CASCADE: deleting an employee's account must not
        # delete the incident history they happened to file.
        sa.ForeignKeyConstraint(["reporter_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_incidents_status_severity", "incidents", ["status", "severity"])
    op.create_index("ix_incidents_service_detected", "incidents", ["service", "detected_at"])
    op.create_index("ix_incidents_service", "incidents", ["service"])
    op.create_index("ix_incidents_severity", "incidents", ["severity"])
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index("ix_incidents_detected_at", "incidents", ["detected_at"])
    op.create_index("ix_incidents_reporter_id", "incidents", ["reporter_id"])
    op.create_index("ix_incidents_created_at", "incidents", ["created_at"])

    # ------------------------------------------------------------------
    # investigation_reports
    # ------------------------------------------------------------------
    op.create_table(
        "investigation_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("hypotheses", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("remediation_steps", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("citations", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("was_helpful", sa.Boolean(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_reports_incident_created", "investigation_reports", ["incident_id", "created_at"])
    op.create_index("ix_reports_incident_id", "investigation_reports", ["incident_id"])
    op.create_index("ix_reports_session_id", "investigation_reports", ["session_id"])
    op.create_index("ix_reports_confidence", "investigation_reports", ["confidence"])
    op.create_index("ix_reports_was_helpful", "investigation_reports", ["was_helpful"])
    op.create_index("ix_reports_created_at", "investigation_reports", ["created_at"])


def downgrade() -> None:
    """Drop the schema.

    Tables are dropped in reverse dependency order. The extensions are left in
    place: another database object may depend on them, and dropping a shared
    extension during a rollback is a much worse outcome than leaving it.
    """
    op.drop_table("investigation_reports")
    op.drop_table("incidents")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("sessions")
    op.drop_table("users")
