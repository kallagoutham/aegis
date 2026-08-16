"""Knowledge base endpoints: search, ingestion, and log analysis."""

from __future__ import annotations

import asyncio
from pathlib import Path
import uuid

from fastapi import (
    APIRouter,
    File,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import (
    func,
    select,
)

from aegis.api.deps import (
    AdminUser,
    CurrentUser,
    DbSession,
    PaginationParams,
)
from aegis.core.config import (
    PROJECT_ROOT,
    settings,
)
from aegis.core.exceptions import (
    AuthorizationError,
    PayloadTooLargeError,
    ResourceNotFoundError,
    ValidationError,
)
from aegis.core.limiter import limiter
from aegis.core.logging import logger
from aegis.ingestion.pipeline import IngestionPipeline
from aegis.models.knowledge import (
    Document,
    SourceType,
)
from aegis.retrieval.hybrid import (
    HybridRetriever,
    RetrievalRequest,
)
from aegis.retrieval.vector_store import VectorStore
from aegis.schemas.knowledge import (
    DocumentListResponse,
    DocumentResponse,
    IngestionSummaryResponse,
    IngestPathRequest,
    IngestTextRequest,
    KnowledgeStats,
    LogAnalysisRequest,
    SearchHit,
    SearchRequest,
    SearchResponse,
)

router = APIRouter()


@router.post("/search", response_model=SearchResponse, summary="Search the knowledge base")
@limiter.limit(settings.rate_limit_for("search"))
async def search(
    request: Request,
    payload: SearchRequest,
    user: CurrentUser,
    session: DbSession,
) -> SearchResponse:
    """Run a hybrid search and return ranked passages.

    Exposed directly - not only to the agent - because an engineer who knows
    what they are looking for is better served by search results than by waiting
    for a model to summarise them.
    """
    retriever = HybridRetriever(session)
    response = await retriever.search(
        RetrievalRequest(
            query=payload.query,
            top_k=payload.top_k,
            source_types=payload.source_types,
            service=payload.service,
            rerank=payload.rerank,
            min_score=payload.min_score,
        )
    )

    return SearchResponse(
        query=response.query,
        hits=[
            SearchHit(
                chunk_id=result.chunk_id,
                document_id=result.document_id,
                title=result.title,
                heading_path=result.heading_path,
                source_uri=result.source_uri,
                source_type=result.source_type,
                service=result.service,
                score=result.score,
                strategy=result.strategy,
                content=result.content,
            )
            for result in response.results
        ],
        total_candidates=response.total_candidates,
        reranked=response.reranked,
        timings_ms=response.timings_ms,
    )


@router.post(
    "/documents/text",
    response_model=IngestionSummaryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a document supplied as text",
)
@limiter.limit(settings.rate_limit_for("ingest"))
async def ingest_text(
    request: Request,
    payload: IngestTextRequest,
    user: AdminUser,
) -> IngestionSummaryResponse:
    """Chunk, embed, and index a document sent as text."""
    pipeline = IngestionPipeline()
    result = await pipeline.ingest_raw_text(
        payload.content,
        source_uri=payload.source_uri,
        title=payload.title,
        source_type=payload.source_type,
        service=payload.service,
        force=payload.force,
    )

    from aegis.ingestion.pipeline import IngestionSummary

    summary = IngestionSummary(results=[result], duration_ms=result.duration_ms)
    return IngestionSummaryResponse(**summary.to_dict())


@router.post(
    "/documents/upload",
    response_model=IngestionSummaryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload and ingest a document",
)
@limiter.limit(settings.rate_limit_for("ingest"))
async def upload_document(
    request: Request,
    user: AdminUser,
    file: UploadFile = File(..., description="Markdown, text, or log file."),
    service: str | None = None,
    source_type: SourceType | None = None,
) -> IngestionSummaryResponse:
    """Ingest an uploaded file.

    Size is checked against the decoded body rather than a declared
    ``Content-Length``, which a client controls and can understate.
    """
    raw = await file.read()
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise PayloadTooLargeError(
            f"Upload is {len(raw)} bytes, above the {settings.MAX_UPLOAD_BYTES} byte limit.",
            context={"filename": file.filename, "size": len(raw)},
        )

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        # errors="replace" rather than rejecting: a log capture with one bad
        # byte is still worth indexing.
        content = raw.decode("utf-8", errors="replace")
        logger.warning("upload_contained_invalid_utf8", filename=file.filename)

    pipeline = IngestionPipeline()
    result = await pipeline.ingest_raw_text(
        content,
        source_uri=f"upload://{file.filename}",
        title=None,
        source_type=source_type,
        service=service,
    )

    from aegis.ingestion.pipeline import IngestionSummary

    summary = IngestionSummary(results=[result], duration_ms=result.duration_ms)
    return IngestionSummaryResponse(**summary.to_dict())


@router.post(
    "/documents/path",
    response_model=IngestionSummaryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a file or directory from the server filesystem",
)
@limiter.limit(settings.rate_limit_for("ingest"))
async def ingest_path(
    request: Request,
    payload: IngestPathRequest,
    user: AdminUser,
) -> IngestionSummaryResponse:
    """Ingest from a server-side path.

    The path is resolved and confined to the project root. Without that check
    this endpoint is an arbitrary-file-read primitive: an admin could ingest
    ``/etc/shadow`` and then retrieve its contents through search.
    """

    # expanduser() and resolve() both touch the filesystem. Offloaded so a slow
    # or unresponsive mount cannot stall the event loop.
    def _resolve() -> Path:
        candidate = Path(payload.path).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate.resolve()

    resolved = await asyncio.to_thread(_resolve)
    root = PROJECT_ROOT.resolve()

    # Compares fully resolved paths, so symlinks pointing outside the root are
    # caught as well as literal '..' traversal.
    if not resolved.is_relative_to(root):
        logger.warning("ingest_path_traversal_blocked", requested=payload.path, resolved=str(resolved))
        raise AuthorizationError(
            "Ingestion paths must be inside the project directory.",
            context={"requested": payload.path},
        )

    if not await asyncio.to_thread(resolved.exists):
        raise ResourceNotFoundError("Path", payload.path)

    pipeline = IngestionPipeline()
    summary = await pipeline.ingest_path(resolved, force=payload.force)
    return IngestionSummaryResponse(**summary.to_dict())


@router.get("/documents", response_model=DocumentListResponse, summary="List indexed documents")
async def list_documents(
    user: CurrentUser,
    session: DbSession,
    pagination: PaginationParams,
    service: str | None = None,
    source_type: SourceType | None = None,
) -> DocumentListResponse:
    """List indexed documents with optional filters."""
    statement = select(Document)
    count_statement = select(func.count()).select_from(Document)

    if service:
        statement = statement.where(Document.service == service)
        count_statement = count_statement.where(Document.service == service)
    if source_type:
        statement = statement.where(Document.source_type == source_type)
        count_statement = count_statement.where(Document.source_type == source_type)

    statement = statement.order_by(Document.updated_at.desc()).limit(pagination.limit).offset(pagination.offset)

    rows = (await session.execute(statement)).scalars().all()
    total = (await session.execute(count_statement)).scalar_one()

    return DocumentListResponse(
        items=[DocumentResponse.model_validate(row) for row in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a document from the index",
)
async def delete_document_by_id(
    document_id: uuid.UUID,
    user: AdminUser,
    session: DbSession,
) -> None:
    """Delete a document and its chunks."""
    document = await session.get(Document, document_id)
    if document is None:
        raise ResourceNotFoundError("Document", document_id)

    await session.delete(document)  # chunks follow via ON DELETE CASCADE
    await session.commit()
    logger.info("document_deleted", document_id=str(document_id), title=document.title)


@router.get("/stats", response_model=KnowledgeStats, summary="Index size and coverage")
async def knowledge_stats(user: CurrentUser, session: DbSession) -> KnowledgeStats:
    """Report index size, coverage, and embedding backlog."""
    store = VectorStore(session)
    return KnowledgeStats(**await store.stats())


@router.post("/analyze-logs", summary="Parse and summarise a log bundle")
@limiter.limit(settings.rate_limit_for("search"))
async def analyze_logs_endpoint(
    request: Request,
    payload: LogAnalysisRequest,
    user: CurrentUser,
) -> dict:
    """Analyse logs without running the agent.

    Useful on its own, and much cheaper than an investigation when all you want
    is the error breakdown and the onset time.
    """
    from aegis.analysis import analyse_logs

    if not payload.logs.strip():
        raise ValidationError("The 'logs' field is empty.")

    analysis = analyse_logs(payload.logs, max_lines=payload.max_lines)
    return {
        "analysis": analysis.to_dict(),
        "summary": analysis.to_prompt_summary(),
    }


__all__ = ["router"]
