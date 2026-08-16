"""Retrieval layer: embeddings, vector storage, and hybrid search."""

from aegis.retrieval.embeddings import (
    EmbeddingCache,
    EmbeddingService,
    get_embedding_service,
)
from aegis.retrieval.hybrid import (
    HybridRetriever,
    RetrievalRequest,
    RetrievalResponse,
    reciprocal_rank_fusion,
)
from aegis.retrieval.vector_store import (
    SearchResult,
    VectorStore,
)

__all__ = [
    "EmbeddingCache",
    "EmbeddingService",
    "HybridRetriever",
    "RetrievalRequest",
    "RetrievalResponse",
    "SearchResult",
    "VectorStore",
    "get_embedding_service",
    "reciprocal_rank_fusion",
]
