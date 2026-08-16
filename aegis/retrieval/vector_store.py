"""pgvector-backed storage and the two primitive search strategies.

This module owns the raw retrieval arms. Fusion, reranking, and policy live one
layer up in :mod:`aegis.retrieval.hybrid`; keeping them separate means each arm
can be tested and benchmarked on its own.

**Why Postgres rather than a dedicated vector database.** The corpus here is
operational documentation - thousands to low millions of chunks, not billions.
At that scale pgvector's HNSW index is comfortably fast, and using the database
we already run removes an entire service from the deployment, keeps embeddings
transactionally consistent with their parent documents, and lets a single query
join vector hits against relational filters (service, source type, recency)
without a second network hop or a stale replica.

**Why the vector arrives as a string.** pgvector's driver-native binding
requires per-connection type registration, which is fragile across pooled
async connections. Serialising to ``'[0.1,0.2,...]'`` and casting with
``::vector`` in SQL is driver-agnostic, still a bound parameter (so it is not
an injection vector), and measurably no slower for realistic dimensions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import (
    dataclass,
    field,
)
import time
from typing import (
    Any,
)
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.core.config import settings
from aegis.core.exceptions import RetrievalError
from aegis.core.logging import logger
from aegis.core.metrics import (
    retrieval_duration_seconds,
    retrieval_queries_total,
)
from aegis.models.knowledge import (
    TEXT_SEARCH_CONFIG,
    Document,
    DocumentChunk,
    SourceType,
)


@dataclass(slots=True)
class SearchResult:
    """A single retrieved chunk with provenance and scoring.

    ``score`` is normalised to ``[0, 1]`` and comparable *within* one strategy,
    but not across strategies - cosine similarity and ``ts_rank_cd`` measure
    different things. Cross-strategy comparison is exactly what rank fusion in
    :mod:`aegis.retrieval.hybrid` exists to handle.
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    score: float
    title: str
    source_uri: str
    source_type: SourceType
    heading_path: str = ""
    service: str | None = None
    chunk_index: int = 0
    strategy: str = "vector"
    metadata: dict[str, Any] = field(default_factory=dict)

    def citation(self) -> str:
        """Render a compact source reference for inclusion in an answer."""
        return f"{self.title} :: {self.heading_path}" if self.heading_path else self.title

    def to_context_block(self, index: int) -> str:
        """Format the result as a numbered context block for the LLM prompt.

        The explicit ``[n]`` marker is what lets the model cite sources by
        number, and what lets us verify afterwards that a claimed citation
        actually corresponds to retrieved text.
        """
        header = f"[{index}] {self.citation()}"
        meta = f"(source: {self.source_type.value}"
        if self.service:
            meta += f", service: {self.service}"
        meta += f", relevance: {self.score:.2f})"
        return f"{header}\n{meta}\n{self.content.strip()}"

    def to_dict(self) -> dict[str, Any]:
        """Serialise for API responses and report citations."""
        return {
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "title": self.title,
            "heading_path": self.heading_path,
            "source_uri": self.source_uri,
            "source_type": self.source_type.value,
            "service": self.service,
            "score": round(self.score, 4),
            "strategy": self.strategy,
            "content": self.content,
        }


# Shared projection so the vector and lexical arms return identically shaped
# rows and can be fused without per-strategy special-casing.
_PROJECTION = """
    c.id            AS chunk_id,
    c.document_id   AS document_id,
    c.content       AS content,
    c.heading_path  AS heading_path,
    c.chunk_index   AS chunk_index,
    c.source_type   AS source_type,
    c.service       AS service,
    c.chunk_metadata AS chunk_metadata,
    d.title         AS title,
    d.source_uri    AS source_uri
"""


def _build_filters(
    source_types: Sequence[SourceType] | None,
    service: str | None,
    document_ids: Sequence[uuid.UUID] | None,
) -> tuple[str, dict[str, Any]]:
    """Compose the optional WHERE clauses shared by both search arms.

    Returns:
        A ``(sql_fragment, params)`` pair. The fragment is always safe to
        concatenate because every value is bound, never interpolated.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if source_types:
        clauses.append("c.source_type = ANY(:source_types)")
        params["source_types"] = [source_type.value for source_type in source_types]
    if service:
        clauses.append("c.service = :service")
        params["service"] = service
    if document_ids:
        clauses.append("c.document_id = ANY(:document_ids)")
        params["document_ids"] = list(document_ids)

    return ("".join(f" AND {clause}" for clause in clauses), params)


def _row_to_result(row: Any, score: float, strategy: str) -> SearchResult:
    """Map a database row onto a :class:`SearchResult`."""
    return SearchResult(
        chunk_id=row.chunk_id,
        document_id=row.document_id,
        content=row.content,
        score=score,
        title=row.title,
        source_uri=row.source_uri,
        source_type=SourceType(row.source_type),
        heading_path=row.heading_path or "",
        service=row.service,
        chunk_index=row.chunk_index,
        strategy=strategy,
        metadata=row.chunk_metadata or {},
    )


def serialise_vector(vector: Sequence[float]) -> str:
    """Render an embedding in pgvector's literal syntax.

    Args:
        vector: The embedding.

    Returns:
        A string such as ``"[0.013,-0.221,...]"`` suitable for ``::vector``.
    """
    return "[" + ",".join(f"{component:.7g}" for component in vector) + "]"


class VectorStore:
    """Persistence and primitive search over ``document_chunks``."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the store to an active session."""
        self.session = session

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert_chunks(
        self,
        document: Document,
        chunks: Sequence[DocumentChunk],
        embeddings: Sequence[Sequence[float]],
    ) -> int:
        """Replace a document's chunks with a new set.

        Delete-then-insert rather than a true upsert: chunk boundaries shift
        when a document is edited, so chunk *n* of the new version has no
        meaningful relationship to chunk *n* of the old one. Matching them up
        would produce chunks whose embedding describes text they no longer
        contain.

        Args:
            document: The parent document, already persisted.
            chunks: Chunks to write, in order.
            embeddings: One vector per chunk, positionally aligned.

        Returns:
            The number of chunks written.

        Raises:
            RetrievalError: If chunk and embedding counts disagree, which would
                otherwise silently attach the wrong vector to a chunk.
        """
        if len(chunks) != len(embeddings):
            raise RetrievalError(
                "Chunk/embedding count mismatch; refusing to write a corrupt index.",
                context={"chunks": len(chunks), "embeddings": len(embeddings)},
            )

        await self.delete_document_chunks(document.id)

        for chunk, embedding in zip(chunks, embeddings, strict=True):
            chunk.document_id = document.id
            chunk.embedding = list(embedding)
            chunk.source_type = document.source_type
            chunk.service = document.service
            self.session.add(chunk)

        await self.session.flush()
        logger.info(
            "chunks_upserted",
            document_id=str(document.id),
            chunk_count=len(chunks),
            source_type=document.source_type.value,
        )
        return len(chunks)

    async def delete_document_chunks(self, document_id: uuid.UUID) -> int:
        """Delete every chunk belonging to a document.

        Returns:
            Number of rows removed.
        """
        result = await self.session.execute(
            text("DELETE FROM document_chunks WHERE document_id = :document_id"),
            {"document_id": document_id},
        )
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def vector_search(
        self,
        query_embedding: Sequence[float],
        *,
        limit: int | None = None,
        source_types: Sequence[SourceType] | None = None,
        service: str | None = None,
        document_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[SearchResult]:
        """Find the nearest chunks by cosine similarity.

        ``<=>`` is pgvector's cosine *distance* operator (0 = identical). We
        return ``1 - distance`` so callers always deal in similarity, where
        higher is better, consistent with the lexical arm.

        Ordering by the bare operator - rather than by the computed alias - is
        what lets the planner use the HNSW index. Sorting on ``1 - (...)``
        would force a sequential scan over every chunk.

        Args:
            query_embedding: The embedded query.
            limit: Rows to return; defaults to ``RETRIEVAL_CANDIDATE_K``.
            source_types: Restrict to these document types.
            service: Restrict to one service.
            document_ids: Restrict to specific documents.

        Returns:
            Results ordered by descending similarity.
        """
        limit = limit or settings.RETRIEVAL_CANDIDATE_K
        filter_sql, params = _build_filters(source_types, service, document_ids)

        # S608 is suppressed for this file in pyproject.toml: the only
        # interpolated fragments are _PROJECTION (a module constant) and
        # filter_sql (assembled from fixed clause strings). Every user-
        # supplied value is a bound parameter.
        statement = text(
            f"""
            SELECT {_PROJECTION},
                   1 - (c.embedding <=> CAST(:query_vector AS vector)) AS score
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL{filter_sql}
            ORDER BY c.embedding <=> CAST(:query_vector AS vector)
            LIMIT :limit
            """
        )
        params.update({"query_vector": serialise_vector(query_embedding), "limit": limit})

        started = time.perf_counter()
        try:
            rows = (await self.session.execute(statement, params)).all()
        except Exception as exc:
            retrieval_queries_total.labels(strategy="vector", outcome="error").inc()
            logger.error("vector_search_failed", error=str(exc), exc_info=True)
            raise RetrievalError("Vector search failed.", context={"error": str(exc)}) from exc
        finally:
            retrieval_duration_seconds.labels(stage="vector").observe(time.perf_counter() - started)

        retrieval_queries_total.labels(strategy="vector", outcome="success").inc()
        # Cosine similarity is in [-1, 1]; clamp to [0, 1] because negative
        # similarity carries no useful ranking signal for text embeddings.
        return [_row_to_result(row, max(0.0, min(1.0, float(row.score))), "vector") for row in rows]

    async def lexical_search(
        self,
        query: str,
        *,
        limit: int | None = None,
        source_types: Sequence[SourceType] | None = None,
        service: str | None = None,
        document_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[SearchResult]:
        """Find chunks by full-text relevance.

        Uses ``websearch_to_tsquery``, which parses Google-style syntax (quoted
        phrases, ``OR``, leading ``-`` for negation) and - critically - never
        raises on malformed input. ``to_tsquery`` would throw a syntax error on
        an unbalanced quote in a pasted log line, turning a search into a 500.

        ``ts_rank_cd`` (cover density) is preferred over plain ``ts_rank``
        because it rewards matches whose terms appear near each other, which
        matters when searching for a multi-token error string.

        Args:
            query: Raw user query.
            limit: Rows to return; defaults to ``RETRIEVAL_CANDIDATE_K``.
            source_types: Restrict to these document types.
            service: Restrict to one service.
            document_ids: Restrict to specific documents.

        Returns:
            Results ordered by descending lexical relevance.
        """
        limit = limit or settings.RETRIEVAL_CANDIDATE_K
        filter_sql, params = _build_filters(source_types, service, document_ids)

        # See the note in vector_search: only module constants and fixed
        # clause strings are interpolated; the query text is bound.
        statement = text(
            f"""
            WITH q AS (SELECT websearch_to_tsquery(:ts_config, :query) AS query)
            SELECT {_PROJECTION},
                   ts_rank_cd(c.content_tsv, q.query) AS score
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            CROSS JOIN q
            WHERE c.content_tsv @@ q.query{filter_sql}
            ORDER BY score DESC
            LIMIT :limit
            """
        )
        params.update({"query": query, "ts_config": TEXT_SEARCH_CONFIG, "limit": limit})

        started = time.perf_counter()
        try:
            rows = (await self.session.execute(statement, params)).all()
        except Exception as exc:
            retrieval_queries_total.labels(strategy="lexical", outcome="error").inc()
            logger.error("lexical_search_failed", error=str(exc), exc_info=True)
            raise RetrievalError("Lexical search failed.", context={"error": str(exc)}) from exc
        finally:
            retrieval_duration_seconds.labels(stage="lexical").observe(time.perf_counter() - started)

        retrieval_queries_total.labels(strategy="lexical", outcome="success").inc()

        # ts_rank_cd is unbounded above, so absolute values mean nothing. Scale
        # by the top hit to land in [0, 1]; only relative order survives, which
        # is all rank fusion consumes anyway.
        top = max((float(row.score) for row in rows), default=0.0)
        return [_row_to_result(row, (float(row.score) / top) if top > 0 else 0.0, "lexical") for row in rows]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """Summarise index size and coverage.

        Surfaces the number of chunks still missing an embedding, which is the
        clearest signal that an ingestion run failed partway through.
        """
        row = (
            await self.session.execute(
                text(
                    """
                    SELECT
                        (SELECT count(*) FROM documents)                             AS documents,
                        (SELECT count(*) FROM document_chunks)                       AS chunks,
                        (SELECT count(*) FROM document_chunks
                          WHERE embedding IS NULL)                                   AS unembedded_chunks,
                        (SELECT count(DISTINCT service) FROM documents
                          WHERE service IS NOT NULL)                                 AS services
                    """
                )
            )
        ).one()

        by_type = (
            await self.session.execute(
                text("SELECT source_type, count(*) AS count FROM documents GROUP BY source_type")
            )
        ).all()

        return {
            "documents": row.documents,
            "chunks": row.chunks,
            "unembedded_chunks": row.unembedded_chunks,
            "services": row.services,
            "documents_by_type": {entry.source_type: entry.count for entry in by_type},
        }


__all__ = ["SearchResult", "VectorStore", "serialise_vector"]
