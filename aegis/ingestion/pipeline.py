"""The ingestion pipeline: load, chunk, embed, index.

Orchestrates the full path from a source document to searchable chunks:

    load  ->  hash  ->  (skip if unchanged)  ->  chunk  ->  embed  ->  upsert

Three properties matter more than raw throughput:

**Idempotence.** Re-ingesting an unchanged document is a no-op decided by a
content-hash comparison, before any embedding cost is incurred. A nightly sync
over a thousand runbooks where three changed costs three embeddings, not a
thousand.

**Partial failure isolation.** Each document is its own transaction. One
malformed file marks itself ``FAILED`` with the reason recorded on the row and
the run continues. A batch that fails halfway leaves the successful documents
indexed and queryable.

**Bounded concurrency.** Documents are processed through a semaphore
(``INGESTION_CONCURRENCY``). Unbounded fan-out would open one database
connection per document and trip provider rate limits, turning a fast ingest
into a slow cascade of 429 backoffs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
import time

from sqlalchemy import select

from aegis.core.config import settings
from aegis.core.exceptions import IngestionError
from aegis.core.logging import logger
from aegis.core.metrics import (
    ingestion_chunks_total,
    ingestion_documents_total,
    ingestion_duration_seconds,
)
from aegis.ingestion.chunking import get_chunker
from aegis.ingestion.loaders import (
    LoadedDocument,
    load_directory,
    load_file,
    load_text,
)
from aegis.models.knowledge import (
    Document,
    DocumentChunk,
    IngestionStatus,
    SourceType,
)
from aegis.retrieval.embeddings import get_embedding_service
from aegis.retrieval.vector_store import VectorStore
from aegis.services.database import session_scope


@dataclass(slots=True)
class IngestionResult:
    """Outcome of ingesting one document."""

    source_uri: str
    status: IngestionStatus
    document_id: str | None = None
    title: str = ""
    chunks: int = 0
    tokens: int = 0
    duration_ms: float = 0.0
    skipped_reason: str | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Whether the document ended up indexed or was already current."""
        return self.status is IngestionStatus.INDEXED


@dataclass(slots=True)
class IngestionSummary:
    """Aggregate outcome of an ingestion run."""

    results: list[IngestionResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def indexed(self) -> int:
        """Documents newly indexed or re-indexed."""
        return sum(
            1 for result in self.results if result.status is IngestionStatus.INDEXED and not result.skipped_reason
        )

    @property
    def skipped(self) -> int:
        """Documents skipped because their content was unchanged."""
        return sum(1 for result in self.results if result.skipped_reason)

    @property
    def failed(self) -> int:
        """Documents that could not be ingested."""
        return sum(1 for result in self.results if result.status is IngestionStatus.FAILED)

    @property
    def total_chunks(self) -> int:
        """Chunks written across the run."""
        return sum(result.chunks for result in self.results)

    def to_dict(self) -> dict[str, object]:
        """Serialise for API responses and CLI output."""
        return {
            "documents_processed": len(self.results),
            "indexed": self.indexed,
            "skipped_unchanged": self.skipped,
            "failed": self.failed,
            "total_chunks": self.total_chunks,
            "duration_ms": round(self.duration_ms, 1),
            "documents": [
                {
                    "source_uri": result.source_uri,
                    "title": result.title,
                    "status": result.status.value,
                    "chunks": result.chunks,
                    "skipped_reason": result.skipped_reason,
                    "error": result.error,
                }
                for result in self.results
            ],
        }


class IngestionPipeline:
    """Coordinates loading, chunking, embedding, and indexing."""

    def __init__(self, concurrency: int | None = None) -> None:
        """Configure the pipeline.

        Args:
            concurrency: Maximum documents processed simultaneously.
        """
        self.concurrency = concurrency or settings.INGESTION_CONCURRENCY

    async def ingest_document(self, loaded: LoadedDocument, *, force: bool = False) -> IngestionResult:
        """Ingest a single loaded document.

        Runs in its own transaction so a failure here cannot roll back documents
        that already succeeded in the same batch.

        Args:
            loaded: The normalised source document.
            force: Re-index even when the content hash is unchanged. Use after
                changing the chunking strategy or embedding model, where the
                source is identical but the derived index is stale.

        Returns:
            The per-document outcome. Failures are returned, not raised, so a
            batch can report on every document.
        """
        started = time.perf_counter()
        content_hash = loaded.content_hash

        try:
            async with session_scope() as session:
                existing = (
                    await session.execute(select(Document).where(Document.source_uri == loaded.source_uri))
                ).scalar_one_or_none()

                # Fast path: identical content already fully indexed.
                if (
                    existing is not None
                    and existing.content_hash == content_hash
                    and existing.status is IngestionStatus.INDEXED
                    and not force
                ):
                    ingestion_documents_total.labels(
                        source_type=loaded.source_type.value, outcome="skipped_unchanged"
                    ).inc()
                    return IngestionResult(
                        source_uri=loaded.source_uri,
                        status=IngestionStatus.INDEXED,
                        document_id=str(existing.id),
                        title=existing.title,
                        chunks=existing.chunk_count,
                        tokens=existing.token_count,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        skipped_reason="content unchanged",
                    )

                document = existing or Document(
                    source_uri=loaded.source_uri, content_hash=content_hash, title=loaded.title
                )
                document.title = loaded.title
                document.source_type = loaded.source_type
                document.service = loaded.service
                document.content_hash = content_hash
                document.doc_metadata = loaded.metadata
                document.status = IngestionStatus.PROCESSING
                document.error = None
                session.add(document)
                await session.flush()

                # --- chunk -------------------------------------------------
                chunker = get_chunker(loaded.source_type.value)
                chunks = chunker.chunk(loaded.content, base_metadata={"source_uri": loaded.source_uri})

                if not chunks:
                    document.status = IngestionStatus.FAILED
                    document.error = "Document produced no chunks (empty or unparseable)."
                    ingestion_documents_total.labels(source_type=loaded.source_type.value, outcome="failed").inc()
                    return IngestionResult(
                        source_uri=loaded.source_uri,
                        status=IngestionStatus.FAILED,
                        document_id=str(document.id),
                        title=document.title,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        error=document.error,
                    )

                # --- embed -------------------------------------------------
                # Heading context is prepended before embedding so the vector
                # encodes where the passage sits in the document, not just its
                # body text.
                embedding_service = await get_embedding_service()
                embeddings = await embedding_service.embed_documents(
                    [chunk.with_heading_context() for chunk in chunks]
                )

                # --- index -------------------------------------------------
                rows = [
                    DocumentChunk(
                        chunk_index=chunk.index,
                        content=chunk.content,
                        heading_path=chunk.heading_path,
                        token_count=chunk.token_count,
                        chunk_metadata=chunk.metadata,
                    )
                    for chunk in chunks
                ]

                store = VectorStore(session)
                written = await store.upsert_chunks(document, rows, embeddings)

                document.chunk_count = written
                document.token_count = sum(chunk.token_count for chunk in chunks)
                document.status = IngestionStatus.INDEXED
                session.add(document)

                duration = time.perf_counter() - started
                ingestion_documents_total.labels(source_type=loaded.source_type.value, outcome="indexed").inc()
                ingestion_chunks_total.labels(source_type=loaded.source_type.value).inc(written)
                ingestion_duration_seconds.labels(source_type=loaded.source_type.value).observe(duration)

                logger.info(
                    "document_ingested",
                    document_id=str(document.id),
                    title=document.title,
                    source_type=document.source_type.value,
                    service=document.service,
                    chunks=written,
                    tokens=document.token_count,
                    duration_ms=round(duration * 1000, 1),
                )

                return IngestionResult(
                    source_uri=loaded.source_uri,
                    status=IngestionStatus.INDEXED,
                    document_id=str(document.id),
                    title=document.title,
                    chunks=written,
                    tokens=document.token_count,
                    duration_ms=duration * 1000,
                )

        except Exception as exc:
            ingestion_documents_total.labels(source_type=loaded.source_type.value, outcome="failed").inc()
            logger.error(
                "document_ingestion_failed",
                source_uri=loaded.source_uri,
                error=str(exc),
                exc_info=True,
            )
            # Record the failure on the document row in a *separate* transaction:
            # the original one was rolled back, so writing the error there would
            # be discarded along with it.
            await self._record_failure(loaded, str(exc))
            return IngestionResult(
                source_uri=loaded.source_uri,
                status=IngestionStatus.FAILED,
                title=loaded.title,
                duration_ms=(time.perf_counter() - started) * 1000,
                error=str(exc),
            )

    async def _record_failure(self, loaded: LoadedDocument, error: str) -> None:
        """Persist a failure reason so operators can see what went wrong."""
        try:
            async with session_scope() as session:
                document = (
                    await session.execute(select(Document).where(Document.source_uri == loaded.source_uri))
                ).scalar_one_or_none()
                if document is None:
                    document = Document(
                        source_uri=loaded.source_uri,
                        content_hash=loaded.content_hash,
                        title=loaded.title,
                        source_type=loaded.source_type,
                        service=loaded.service,
                    )
                document.status = IngestionStatus.FAILED
                document.error = error[:2000]
                session.add(document)
        except Exception as exc:
            # Best effort. The failure is already logged above; losing the
            # database record of it must not mask the original error.
            logger.warning("failure_record_write_failed", source_uri=loaded.source_uri, error=str(exc))

    async def ingest_documents(self, documents: Sequence[LoadedDocument], *, force: bool = False) -> IngestionSummary:
        """Ingest many documents with bounded concurrency.

        Args:
            documents: Loaded documents to process.
            force: Re-index even when unchanged.

        Returns:
            Aggregate summary including per-document outcomes.
        """
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded(document: LoadedDocument) -> IngestionResult:
            async with semaphore:
                return await self.ingest_document(document, force=force)

        results = await asyncio.gather(*(guarded(document) for document in documents))

        summary = IngestionSummary(results=list(results), duration_ms=(time.perf_counter() - started) * 1000)
        logger.info(
            "ingestion_run_completed",
            documents=len(summary.results),
            indexed=summary.indexed,
            skipped=summary.skipped,
            failed=summary.failed,
            chunks=summary.total_chunks,
            duration_ms=round(summary.duration_ms, 1),
        )
        return summary

    async def ingest_path(self, path: Path, *, force: bool = False) -> IngestionSummary:
        """Ingest a file or every ingestible file beneath a directory.

        Args:
            path: File or directory.
            force: Re-index even when unchanged.

        Returns:
            Aggregate summary.

        Raises:
            IngestionError: If the path does not exist.
        """

        def _load() -> list[LoadedDocument]:
            """Walk and read the filesystem. Synchronous by nature."""
            if not path.exists():
                raise IngestionError(f"Path does not exist: {path}", context={"path": str(path)})
            if path.is_file():
                return [load_file(path, max_bytes=settings.MAX_UPLOAD_BYTES)]
            return load_directory(path, max_bytes=settings.MAX_UPLOAD_BYTES)

        # Offloaded to a worker thread. Walking a large runbook repository and
        # reading every file is seconds of blocking syscalls; doing that on the
        # event loop would stall every other in-flight request for the duration
        # - including the health checks that decide whether this replica is
        # still serving.
        documents = await asyncio.to_thread(_load)

        if not documents:
            logger.warning("no_ingestible_documents_found", path=str(path))
            return IngestionSummary()

        return await self.ingest_documents(documents, force=force)

    async def ingest_raw_text(
        self,
        content: str,
        *,
        source_uri: str,
        title: str | None = None,
        source_type: SourceType | None = None,
        service: str | None = None,
        force: bool = False,
    ) -> IngestionResult:
        """Ingest text supplied directly, as from an upload endpoint."""
        loaded = load_text(
            content,
            source_uri=source_uri,
            title=title,
            source_type=source_type,
            service=service,
        )
        return await self.ingest_document(loaded, force=force)


async def delete_document(source_uri: str) -> bool:
    """Remove a document and its chunks from the index.

    Args:
        source_uri: Provenance identifier of the document to remove.

    Returns:
        True if a document was deleted.
    """
    async with session_scope() as session:
        document = (
            await session.execute(select(Document).where(Document.source_uri == source_uri))
        ).scalar_one_or_none()
        if document is None:
            return False
        # Chunks go with it via ON DELETE CASCADE.
        await session.delete(document)
        logger.info("document_deleted", source_uri=source_uri, document_id=str(document.id))
        return True


__all__ = [
    "IngestionPipeline",
    "IngestionResult",
    "IngestionSummary",
    "delete_document",
]
