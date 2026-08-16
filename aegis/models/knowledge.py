"""Knowledge base tables: source documents and their embedded chunks.

The knowledge base is what separates Aegis from a generic chatbot. It holds the
operational corpus an on-call engineer would otherwise grep by hand - runbooks,
architecture notes, resolved postmortems, and captured log bundles - split into
retrievable chunks with both dense (vector) and sparse (full-text) indexes.

Two-table design:

``documents``
    One row per ingested source. Owns provenance (where it came from, which
    service it describes) and a content hash used to skip unchanged re-ingests.

``document_chunks``
    One row per retrievable passage. Owns the embedding, the full-text vector,
    and the heading path that lets a citation say *which section* of a runbook
    answered the question.

Chunks are deliberately denormalised with ``source_type`` and ``service``
copied down from their parent. Retrieval filters on those columns on every
query, and a copied column with its own index is far cheaper than joining back
to ``documents`` inside a vector search.
"""

from enum import Enum
from typing import Any
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    Computed,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
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

# The Postgres text-search configuration used for lexical retrieval. Must match
# between the generated column and the query side, or stemming will disagree and
# recall will quietly collapse.
TEXT_SEARCH_CONFIG = "english"


class SourceType(str, Enum):
    """What kind of operational knowledge a document represents.

    Retrieval filters on this, and the agent weights hits differently by type:
    a runbook is prescriptive ("do this"), a postmortem is precedent ("this
    happened before"), a log bundle is evidence ("here is what the system did").
    """

    RUNBOOK = "runbook"
    POSTMORTEM = "postmortem"
    ARCHITECTURE = "architecture"
    LOG_BUNDLE = "log_bundle"
    ALERT_DEFINITION = "alert_definition"
    OTHER = "other"


class IngestionStatus(str, Enum):
    """Lifecycle of a document as it moves through the ingestion pipeline."""

    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class Document(AegisTable, table=True):
    """A single ingested source document.

    ``content_hash`` makes ingestion idempotent: re-running the pipeline over an
    unchanged file is a no-op, so a nightly sync of a large runbook repository
    costs one hash comparison per file instead of a full re-embed. It is a
    SHA-256 of the normalised text, not of the file bytes, so cosmetic changes
    (line endings, trailing whitespace) do not trigger a pointless re-index.
    """

    __tablename__ = "documents"
    __table_args__ = (
        # A source URI may legitimately be re-ingested with new content, but the
        # same URI at the same content hash is a duplicate.
        UniqueConstraint("source_uri", "content_hash", name="uq_documents_uri_hash"),
        Index("ix_documents_type_service", "source_type", "service"),
        Index("ix_documents_status", "status"),
    )

    title: str = Field(sa_column=Column(String(500), nullable=False), description="Human-readable document title.")
    source_type: SourceType = Field(default=SourceType.OTHER, index=True, nullable=False)
    source_uri: str = Field(
        sa_column=Column(String(1024), nullable=False, index=True),
        description="Where this came from: file path, Confluence URL, S3 key.",
    )
    content_hash: str = Field(
        sa_column=Column(String(64), nullable=False, index=True),
        description="SHA-256 of the normalised text; drives idempotent re-ingest.",
    )
    service: str | None = Field(
        default=None,
        index=True,
        max_length=200,
        description="Service this document describes. The highest-signal retrieval filter.",
    )
    status: IngestionStatus = Field(default=IngestionStatus.PENDING, index=True, nullable=False)
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    chunk_count: int = Field(default=0, ge=0)
    token_count: int = Field(default=0, ge=0, description="Total tokens across all chunks; drives cost reporting.")
    doc_metadata: dict[str, Any] = Field(
        default_factory=empty_dict,
        sa_column=jsonb_column(),
        description="Arbitrary provenance: author, tags, severity, upstream ids.",
    )

    chunks: list["DocumentChunk"] = Relationship(
        back_populates="document",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "lazy": "selectin"},
    )


class DocumentChunk(AegisTable, table=True):
    """One retrievable passage of a document, with its embedding.

    Index strategy, and why each index exists:

    ``HNSW`` on ``embedding``
        Approximate nearest neighbour over cosine distance. Chosen over IVFFlat
        because HNSW needs no training step and keeps good recall as the corpus
        grows incrementally - which is exactly the access pattern here, where
        runbooks trickle in rather than arriving as one bulk load.

    ``GIN`` on ``content_tsv``
        Backs the lexical arm of hybrid search. Vector search alone reliably
        misses exact identifiers - error codes, ``NoBrokersAvailable``, a pod
        name - because embeddings smooth over rare tokens. Lexical search
        catches precisely those.

    ``content_tsv`` is a *generated* column: Postgres recomputes it on write, so
    it can never drift out of sync with ``content`` the way an application-
    maintained column eventually does.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_document_index"),
        Index("ix_chunks_type_service", "source_type", "service"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
    )

    document_id: uuid.UUID = Field(foreign_key="documents.id", index=True, nullable=False, ondelete="CASCADE")
    chunk_index: int = Field(ge=0, nullable=False, description="Zero-based position within the parent document.")
    content: str = Field(sa_column=Column(Text, nullable=False), description="The passage handed to the model.")
    token_count: int = Field(default=0, ge=0)

    heading_path: str = Field(
        default="",
        sa_column=Column(String(1000), nullable=False, server_default=""),
        description=(
            "Breadcrumb of enclosing markdown headings, e.g. "
            "'Payment Service > Troubleshooting > 503 errors'. Preserves the "
            "structural context that flat chunking destroys, and gives citations "
            "something precise to point at."
        ),
    )

    embedding: Any = Field(
        default=None,
        sa_column=Column(Vector(settings.EMBEDDING_DIMENSIONS), nullable=True),
        description=(
            "Dense representation. Nullable so a chunk can be persisted before "
            "its embedding is computed, letting ingestion resume after an "
            "embedding-provider outage without re-chunking."
        ),
    )

    content_tsv: Any = Field(
        default=None,
        sa_column=Column(
            TSVECTOR,
            Computed(f"to_tsvector('{TEXT_SEARCH_CONFIG}', content)", persisted=True),
            nullable=True,
        ),
        description="Generated full-text vector backing lexical retrieval.",
    )

    # Denormalised from the parent document so retrieval can filter without a join.
    source_type: SourceType = Field(default=SourceType.OTHER, index=True, nullable=False)
    service: str | None = Field(default=None, index=True, max_length=200)

    chunk_metadata: dict[str, Any] = Field(
        default_factory=empty_dict,
        sa_column=jsonb_column(),
        description="Per-chunk extras: line ranges for logs, code language, and so on.",
    )

    document: Document = Relationship(back_populates="chunks")

    def citation(self) -> str:
        """Render a short human-readable source reference.

        Used in agent answers so every claim can be traced back to the runbook
        section it came from.
        """
        title = self.document.title if self.document else "unknown source"
        return f"{title} :: {self.heading_path}" if self.heading_path else title


__all__ = [
    "TEXT_SEARCH_CONFIG",
    "Document",
    "DocumentChunk",
    "IngestionStatus",
    "SourceType",
]
