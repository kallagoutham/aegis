"""Aegis - AI incident response platform.

Retrieval-grounded root cause analysis for on-call engineers: hybrid search over
runbooks and postmortems, structured log analysis, and a LangGraph workflow that
produces cited, falsifiable incident reports.

Layout:

``aegis.api``        HTTP endpoints and dependencies
``aegis.core``       config, logging, metrics, errors, prompts, agent graph
``aegis.ingestion``  loading, chunking, and indexing documents
``aegis.retrieval``  embeddings, vector store, hybrid search
``aegis.analysis``   log parsing, template clustering, anomaly detection
``aegis.models``     database tables
``aegis.schemas``    API contracts and structured LLM output
``aegis.services``   database and LLM access
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
