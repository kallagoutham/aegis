"""Ingestion layer: loading, chunking, and indexing operational documents."""

from aegis.ingestion.chunking import (
    Chunk,
    LogChunker,
    MarkdownChunker,
    estimate_tokens,
    get_chunker,
)
from aegis.ingestion.loaders import (
    LoadedDocument,
    load_directory,
    load_file,
    load_text,
)
from aegis.ingestion.pipeline import (
    IngestionPipeline,
    IngestionResult,
    IngestionSummary,
    delete_document,
)

__all__ = [
    "Chunk",
    "IngestionPipeline",
    "IngestionResult",
    "IngestionSummary",
    "LoadedDocument",
    "LogChunker",
    "MarkdownChunker",
    "delete_document",
    "estimate_tokens",
    "get_chunker",
    "load_directory",
    "load_file",
    "load_text",
]
