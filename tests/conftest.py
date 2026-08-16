"""Shared pytest fixtures.

The test environment is forced to ``APP_ENV=test`` *before* any ``aegis`` module
is imported. That matters because :mod:`aegis.core.config` resolves settings at
import time; setting it afterwards would have no effect and tests would silently
run against development configuration.
"""

from __future__ import annotations

import os

# Must precede every aegis import in this file.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-that-is-long-enough-for-hs256-usage")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")
os.environ.setdefault("LOG_TO_FILE", "false")
# Keep test output readable; raise to DEBUG when diagnosing a failure.
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
os.environ.setdefault("LANGFUSE_ENABLED", "false")
os.environ.setdefault("LONG_TERM_MEMORY_ENABLED", "false")

from datetime import (
    UTC,
    datetime,
)
from typing import Any
import uuid

import pytest

from aegis.analysis.parser import (
    LogEntry,
    LogLevel,
)
from aegis.retrieval.vector_store import SearchResult


@pytest.fixture
def anyio_backend() -> str:
    """Restrict anyio-based tests to asyncio."""
    return "asyncio"


# ----------------------------------------------------------------------
# Sample content
# ----------------------------------------------------------------------


@pytest.fixture
def runbook_markdown() -> str:
    """A representative runbook with front matter and nested headings."""
    return """---
title: Payments Service Runbook
service: payments
type: runbook
owner: payments-team
---

# Payments Service Runbook

The payments service authorises card transactions via the upstream gateway.

## Troubleshooting

Read this section during an active incident.

### 503 errors from checkout

Checkout returns 503 when the payments service cannot reach the gateway.

1. Check connection pool saturation.
2. If saturated, scale replicas.

```bash
kubectl exec deploy/pgbouncer -- psql -c 'SHOW POOLS'
```

### Timeout errors

The gateway enforces a 30 second timeout. Requests exceeding it are rejected
with `PaymentGatewayTimeoutException`.

## Escalation

Page the payments on-call rota via PagerDuty.
"""


@pytest.fixture
def json_logs() -> str:
    """JSON-lines logs containing a clear error burst."""
    return "\n".join(
        [
            '{"timestamp":"2026-08-15T10:20:01Z","level":"info","service":"payments","message":"deploy completed"}',
            (
                '{"timestamp":"2026-08-15T10:23:04Z","level":"error","service":"payments",'
                '"message":"upstream connect timeout to gateway-7f3a"}'
            ),
            (
                '{"timestamp":"2026-08-15T10:23:05Z","level":"error","service":"payments",'
                '"message":"upstream connect timeout to gateway-9c21"}'
            ),
            (
                '{"timestamp":"2026-08-15T10:23:06Z","level":"error","service":"payments",'
                '"message":"upstream connect timeout to gateway-2b8e"}'
            ),
            (
                '{"timestamp":"2026-08-15T10:23:07Z","level":"warning","service":"checkout",'
                '"message":"retrying payment authorisation"}'
            ),
            '{"timestamp":"2026-08-15T10:24:00Z","level":"info","service":"checkout","message":"health check ok"}',
        ]
    )


@pytest.fixture
def text_logs() -> str:
    """Plain-text logs including a multi-line stack trace."""
    return """2026-08-15 10:23:04 ERROR [payments] Connection to db-primary-7f3a timed out after 30000ms
2026-08-15 10:23:04 ERROR [payments] Connection to db-primary-9c21 timed out after 30000ms
2026-08-15 10:23:05 ERROR [checkout] PaymentGatewayTimeoutException: gateway did not respond
    at com.example.payments.Gateway.authorise(Gateway.java:142)
    at com.example.checkout.Service.process(Service.java:88)
Caused by: java.net.SocketTimeoutException: Read timed out
2026-08-15 10:23:06 INFO  [checkout] Falling back to queued authorisation
"""


@pytest.fixture
def logfmt_logs() -> str:
    """logfmt-style logs."""
    return """ts=2026-08-15T10:23:04Z level=error service=payments msg="connection pool exhausted" pool_size=20
ts=2026-08-15T10:23:05Z level=error service=payments msg="connection pool exhausted" pool_size=20
ts=2026-08-15T10:23:06Z level=info service=payments msg="scaling replicas" count=6
"""


# ----------------------------------------------------------------------
# Doubles
# ----------------------------------------------------------------------


class FakeEmbeddingService:
    """Deterministic embedding service for tests.

    Produces a stable pseudo-vector from a hash of the input, so identical text
    always embeds identically and similarity comparisons are reproducible -
    without any network call.
    """

    def __init__(self, dimensions: int = 8) -> None:
        """Create the fake with a small vector width."""
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index % len(digest)] / 255.0 for index in range(self.dimensions)]

    async def embed_texts(self, texts: Any, use_cache: bool = True) -> list[list[float]]:
        """Embed a batch."""
        items = list(texts)
        self.calls.append(items)
        return [self._vector(text) for text in items]

    async def embed_query(self, query: str) -> list[float]:
        """Embed one query."""
        self.calls.append([query])
        return self._vector(query)

    async def embed_documents(self, texts: Any) -> list[list[float]]:
        """Embed document chunks."""
        return await self.embed_texts(texts, use_cache=False)


@pytest.fixture
def fake_embeddings() -> FakeEmbeddingService:
    """A deterministic embedding service."""
    return FakeEmbeddingService()


def make_search_result(
    *,
    content: str = "sample passage",
    score: float = 0.9,
    title: str = "Runbook",
    strategy: str = "vector",
    chunk_id: uuid.UUID | None = None,
    heading_path: str = "",
) -> SearchResult:
    """Build a :class:`SearchResult` with sensible defaults."""
    from aegis.models.knowledge import SourceType

    return SearchResult(
        chunk_id=chunk_id or uuid.uuid4(),
        document_id=uuid.uuid4(),
        content=content,
        score=score,
        title=title,
        source_uri="file:///runbook.md",
        source_type=SourceType.RUNBOOK,
        heading_path=heading_path,
        service="payments",
        strategy=strategy,
    )


@pytest.fixture
def search_result_factory():
    """Expose :func:`make_search_result` as a fixture."""
    return make_search_result


def make_log_entry(
    *,
    message: str = "something happened",
    level: LogLevel = LogLevel.INFO,
    service: str | None = "payments",
    timestamp: datetime | None = None,
    line_number: int = 1,
) -> LogEntry:
    """Build a :class:`LogEntry` with sensible defaults."""
    return LogEntry(
        raw=message,
        message=message,
        level=level,
        timestamp=timestamp or datetime(2026, 8, 15, 10, 23, 4, tzinfo=UTC),
        service=service,
        line_number=line_number,
    )


@pytest.fixture
def log_entry_factory():
    """Expose :func:`make_log_entry` as a fixture."""
    return make_log_entry
