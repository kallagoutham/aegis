"""Knowledge base API schemas: search, ingestion, and index statistics."""

from __future__ import annotations

from datetime import datetime
from typing import (
    Annotated,
    Any,
)
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from aegis.models.knowledge import (
    IngestionStatus,
    SourceType,
)


class SearchRequest(BaseModel):
    """A knowledge base search."""

    query: Annotated[str, Field(min_length=1, max_length=2000)]
    top_k: int = Field(default=8, ge=1, le=50)
    source_types: list[SourceType] | None = Field(default=None, description="Restrict to these document types.")
    service: str | None = Field(default=None, max_length=200)
    rerank: bool | None = Field(
        default=None,
        description="Override the reranking default. Disable for latency-sensitive lookups.",
    )
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("query")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Reject whitespace-only queries."""
        if not value.strip():
            raise ValueError("query must contain non-whitespace characters")
        return value.strip()


class SearchHit(BaseModel):
    """One retrieved passage."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    title: str
    heading_path: str
    source_uri: str
    source_type: SourceType
    service: str | None
    score: float
    strategy: str = Field(description="Which arm found it: vector, lexical, or hybrid.")
    content: str


class SearchResponse(BaseModel):
    """Search results with retrieval diagnostics."""

    query: str
    hits: list[SearchHit]
    total_candidates: int = Field(description="Candidates considered before truncation to top_k.")
    reranked: bool
    timings_ms: dict[str, float] = Field(
        default_factory=dict,
        description="Per-stage latency. Exposed so slow retrieval can be diagnosed without server access.",
    )


class IngestTextRequest(BaseModel):
    """Ingest a document supplied as text."""

    content: Annotated[str, Field(min_length=1, max_length=5_000_000)]
    title: str | None = Field(default=None, max_length=500)
    source_uri: str = Field(
        ..., min_length=1, max_length=1024, description="Stable identifier; re-ingesting the same URI replaces it."
    )
    source_type: SourceType | None = None
    service: str | None = Field(default=None, max_length=200)
    force: bool = Field(
        default=False,
        description="Re-index even when the content hash is unchanged. Needed after "
        "changing the chunking strategy or embedding model.",
    )


class IngestPathRequest(BaseModel):
    """Ingest a file or directory from the server's filesystem."""

    path: Annotated[str, Field(min_length=1, max_length=4096)]
    force: bool = Field(default=False)


class DocumentResponse(BaseModel):
    """An indexed document."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    source_type: SourceType
    source_uri: str
    service: str | None
    status: IngestionStatus
    chunk_count: int
    token_count: int
    error: str | None
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    """A page of documents."""

    items: list[DocumentResponse]
    total: int
    limit: int
    offset: int


class IngestionResultResponse(BaseModel):
    """Outcome of ingesting one document."""

    source_uri: str
    title: str
    status: IngestionStatus
    document_id: str | None = None
    chunks: int = 0
    tokens: int = 0
    skipped_reason: str | None = None
    error: str | None = None


class IngestionSummaryResponse(BaseModel):
    """Aggregate outcome of an ingestion run."""

    documents_processed: int
    indexed: int
    skipped_unchanged: int
    failed: int
    total_chunks: int
    duration_ms: float
    documents: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeStats(BaseModel):
    """Index size and coverage."""

    documents: int
    chunks: int
    unembedded_chunks: int = Field(
        description="Chunks awaiting an embedding. Non-zero means an ingestion run failed partway."
    )
    services: int
    documents_by_type: dict[str, int] = Field(default_factory=dict)


class LogAnalysisRequest(BaseModel):
    """Request body for standalone log analysis."""

    logs: Annotated[str, Field(min_length=1, max_length=5_000_000)]
    max_lines: int | None = Field(default=None, ge=1, le=200_000)


__all__ = [
    "DocumentListResponse",
    "DocumentResponse",
    "IngestPathRequest",
    "IngestTextRequest",
    "IngestionResultResponse",
    "IngestionSummaryResponse",
    "KnowledgeStats",
    "LogAnalysisRequest",
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
]
