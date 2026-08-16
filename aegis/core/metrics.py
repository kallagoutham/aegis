"""Prometheus instrumentation for Aegis.

Metric design follows three rules that keep a time-series database healthy:

1. **Bounded label cardinality.** Labels only ever hold values from a closed set
   (model names, node names, status classes). Never a user id, session id, or
   raw URL path - those would create one series per value and eventually take
   the scrape endpoint down.
2. **Route templates, not paths.** ``/api/v1/incidents/{id}`` is one series;
   ``/api/v1/incidents/9f3c...`` would be millions. :func:`route_template`
   resolves the template from Starlette's router.
3. **Histograms sized to the operation.** Bucket boundaries are chosen per
   metric: sub-second for HTTP, tens of seconds for LLM inference, minutes for
   ingestion. Reusing one bucket set everywhere makes half the histograms
   useless.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    make_asgi_app,
    multiprocess,
)

if TYPE_CHECKING:
    from fastapi import (
        FastAPI,
        Request,
    )

# Latency buckets, in seconds, tuned per operation class.
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_LLM_BUCKETS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0, 120.0)
_RETRIEVAL_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
_INGESTION_BUCKETS = (0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0)

# ----------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------

http_requests_total = Counter(
    "aegis_http_requests_total",
    "HTTP requests handled, by method, route template and status class.",
    ["method", "route", "status"],
)

http_request_duration_seconds = Histogram(
    "aegis_http_request_duration_seconds",
    "Wall-clock time to produce an HTTP response.",
    ["method", "route"],
    buckets=_HTTP_BUCKETS,
)

http_requests_in_flight = Gauge(
    "aegis_http_requests_in_flight",
    "Requests currently being processed. Sustained growth means saturation.",
)

# ----------------------------------------------------------------------
# LLM layer
# ----------------------------------------------------------------------

llm_requests_total = Counter(
    "aegis_llm_requests_total",
    "LLM invocations by model and outcome.",
    ["model", "outcome"],  # outcome: success | error | fallback
)

llm_inference_duration_seconds = Histogram(
    "aegis_llm_inference_duration_seconds",
    "Latency of a single non-streaming LLM completion.",
    ["model"],
    buckets=_LLM_BUCKETS,
)

llm_stream_duration_seconds = Histogram(
    "aegis_llm_stream_duration_seconds",
    "Wall-clock time from stream open to final token.",
    ["model"],
    buckets=_LLM_BUCKETS,
)

llm_time_to_first_token_seconds = Histogram(
    "aegis_llm_time_to_first_token_seconds",
    "Latency until the first streamed token. The number users actually feel.",
    ["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0),
)

llm_tokens_total = Counter(
    "aegis_llm_tokens_total",
    "Tokens consumed, split by direction. Multiply by unit price for spend.",
    ["model", "direction"],  # direction: input | output
)

llm_fallback_total = Counter(
    "aegis_llm_fallback_total",
    "Times the service failed over from one model to the next in the chain.",
    ["from_model", "to_model"],
)

# ----------------------------------------------------------------------
# Retrieval layer
# ----------------------------------------------------------------------

retrieval_queries_total = Counter(
    "aegis_retrieval_queries_total",
    "Knowledge base searches by strategy and outcome.",
    ["strategy", "outcome"],  # strategy: vector | lexical | hybrid
)

retrieval_duration_seconds = Histogram(
    "aegis_retrieval_duration_seconds",
    "Latency of a retrieval stage.",
    ["stage"],  # stage: embed | vector | lexical | fuse | rerank
    buckets=_RETRIEVAL_BUCKETS,
)

retrieval_results_returned = Histogram(
    "aegis_retrieval_results_returned",
    "Chunks returned per search. A spike of zeros means a coverage gap.",
    ["strategy"],
    buckets=(0, 1, 2, 4, 8, 16, 32, 64),
)

retrieval_top_score = Histogram(
    "aegis_retrieval_top_score",
    "Fused score of the best hit. Drift downward signals index staleness.",
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

embedding_requests_total = Counter(
    "aegis_embedding_requests_total",
    "Embedding API calls by outcome.",
    ["model", "outcome"],
)

embedding_batch_size = Histogram(
    "aegis_embedding_batch_size",
    "Texts per embedding API call. Small batches waste request overhead.",
    buckets=(1, 2, 4, 8, 16, 32, 64, 128, 256),
)

# ----------------------------------------------------------------------
# Ingestion
# ----------------------------------------------------------------------

ingestion_documents_total = Counter(
    "aegis_ingestion_documents_total",
    "Documents processed by source type and outcome.",
    ["source_type", "outcome"],  # outcome: indexed | skipped_unchanged | failed
)

ingestion_chunks_total = Counter(
    "aegis_ingestion_chunks_total",
    "Chunks written to the vector store.",
    ["source_type"],
)

ingestion_duration_seconds = Histogram(
    "aegis_ingestion_duration_seconds",
    "End-to-end time to ingest one document.",
    ["source_type"],
    buckets=_INGESTION_BUCKETS,
)

# ----------------------------------------------------------------------
# Agent workflow
# ----------------------------------------------------------------------

agent_investigations_total = Counter(
    "aegis_agent_investigations_total",
    "Completed investigations by terminal outcome.",
    ["outcome"],  # outcome: completed | failed | truncated
)

agent_node_duration_seconds = Histogram(
    "aegis_agent_node_duration_seconds",
    "Time spent inside a single LangGraph node.",
    ["node"],  # triage | retrieve | analyze | investigate | synthesize
    buckets=_LLM_BUCKETS,
)

agent_tool_calls_total = Counter(
    "aegis_agent_tool_calls_total",
    "Tool invocations by tool and outcome.",
    ["tool", "outcome"],
)

agent_tool_duration_seconds = Histogram(
    "aegis_agent_tool_duration_seconds",
    "Tool execution latency.",
    ["tool"],
    buckets=_RETRIEVAL_BUCKETS,
)

agent_iterations = Histogram(
    "aegis_agent_iterations",
    "Investigate->tool loops per investigation. Values at the cap mean the agent ran out of budget before converging.",
    buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 25),
)

agent_confidence = Histogram(
    "aegis_agent_confidence",
    "Self-reported confidence of the leading root cause hypothesis.",
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# ----------------------------------------------------------------------
# Infrastructure
# ----------------------------------------------------------------------

db_pool_connections = Gauge(
    "aegis_db_pool_connections",
    "Database connections by pool and state.",
    ["pool", "state"],  # state: in_use | idle
)

db_query_duration_seconds = Histogram(
    "aegis_db_query_duration_seconds",
    "Latency of instrumented database operations.",
    ["operation"],
    buckets=_RETRIEVAL_BUCKETS,
)

app_info = Gauge(
    "aegis_app_info",
    "Build and deployment metadata. Always 1; read the labels.",
    ["version", "environment"],
)


def route_template(request: Request) -> str:
    """Resolve a request to its route *template*.

    ``/api/v1/incidents/abc-123`` becomes ``/api/v1/incidents/{incident_id}``.
    Without this, every distinct id would create its own metric series and blow
    up Prometheus' memory.

    Args:
        request: The incoming request.

    Returns:
        The matched route template, or ``"unmatched"`` for 404s. Returning a
        constant for unmatched routes is deliberate: a scanner probing random
        URLs must not be able to create unbounded series.
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
    if path_format:
        return str(path_format)
    return "unmatched"


def status_class(status_code: int) -> str:
    """Bucket a status code into ``2xx``/``4xx``/``5xx``.

    Collapsing to classes keeps cardinality at five values instead of sixty
    while preserving everything an alert rule needs.
    """
    return f"{status_code // 100}xx"


def setup_metrics(app: FastAPI) -> None:
    """Mount the ``/metrics`` scrape endpoint and publish build info.

    Uses a dedicated ASGI sub-application rather than a normal route so metric
    collection bypasses our middleware stack: a scrape must not appear in the
    HTTP metrics it is reporting, and must not be rate limited.

    Under Gunicorn with multiple workers, ``PROMETHEUS_MULTIPROC_DIR`` makes the
    endpoint aggregate across processes; otherwise each worker would report only
    its own slice.

    Args:
        app: The FastAPI application to mount onto.
    """
    import os

    app_info.labels(version=app.version, environment=os.getenv("APP_ENV", "development")).set(1)

    multiproc_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR")
    if multiproc_dir:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        app.mount("/metrics", make_asgi_app(registry=registry))
    else:
        app.mount("/metrics", make_asgi_app())


__all__ = [
    "agent_confidence",
    "agent_investigations_total",
    "agent_iterations",
    "agent_node_duration_seconds",
    "agent_tool_calls_total",
    "agent_tool_duration_seconds",
    "app_info",
    "db_pool_connections",
    "db_query_duration_seconds",
    "embedding_batch_size",
    "embedding_requests_total",
    "http_request_duration_seconds",
    "http_requests_in_flight",
    "http_requests_total",
    "ingestion_chunks_total",
    "ingestion_documents_total",
    "ingestion_duration_seconds",
    "llm_fallback_total",
    "llm_inference_duration_seconds",
    "llm_requests_total",
    "llm_stream_duration_seconds",
    "llm_time_to_first_token_seconds",
    "llm_tokens_total",
    "retrieval_duration_seconds",
    "retrieval_queries_total",
    "retrieval_results_returned",
    "retrieval_top_score",
    "route_template",
    "setup_metrics",
    "status_class",
]
