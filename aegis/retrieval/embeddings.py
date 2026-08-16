"""Embedding generation with batching, retries, and an in-process cache.

Embeddings are the single largest cost and latency contributor in ingestion, so
this module optimises three things:

**Batching.** The provider accepts many inputs per request. Embedding 1,000
chunks one at a time means 1,000 round trips; at 64 per request it is 16. Batch
size is capped by ``EMBEDDING_BATCH_SIZE`` and further split when a batch would
exceed the provider's per-request token ceiling.

**Caching.** Query embeddings repeat constantly - the same alert text gets
investigated by three engineers, and the agent re-searches similar phrasings
within one investigation. An LRU keyed on the SHA-256 of the input turns those
into free lookups. The cache is per-process and bounded; it is a latency
optimisation, not a source of truth.

**Failing loudly, in the right place.** A failed embedding during *ingestion*
should mark that document failed and move on. A failed embedding during *search*
must raise, because silently returning zero results looks identical to "the
runbook does not exist" - the worst possible answer during an incident.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable, Sequence
import hashlib
import time

from openai import (
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from aegis.core.config import settings
from aegis.core.exceptions import EmbeddingError
from aegis.core.logging import logger
from aegis.core.metrics import (
    embedding_batch_size,
    embedding_requests_total,
    retrieval_duration_seconds,
)

# Conservative ceiling on tokens per embedding request. The documented limit is
# higher, but staying well under it avoids 400s from batches of unusually long
# chunks, and the cost of an extra request is negligible next to a failed batch.
_MAX_TOKENS_PER_REQUEST = 250_000

# Rough bytes-per-token ratio for English prose. Only used to decide when to
# split a batch, so an approximation is fine and avoids importing a tokenizer
# on the hot path.
_CHARS_PER_TOKEN = 4


class EmbeddingCache:
    """Bounded LRU cache mapping input text to its embedding.

    Not thread-safe by design: it is only touched from the event loop, and
    adding a lock would cost more than the occasional duplicate computation
    would in a multi-threaded setting.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        """Create a cache holding at most ``max_entries`` vectors."""
        self._entries: OrderedDict[str, list[float]] = OrderedDict()
        self._max_entries = max_entries
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(text: str, model: str) -> str:
        """Derive a cache key from the text and model.

        The model is part of the key because vectors from different models live
        in incomparable spaces - mixing them would produce silently meaningless
        similarity scores.
        """
        digest = hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()
        return digest

    def get(self, text: str, model: str) -> list[float] | None:
        """Look up a cached vector, promoting it to most-recently-used."""
        cache_key = self.key(text, model)
        vector = self._entries.get(cache_key)
        if vector is None:
            self.misses += 1
            return None
        self._entries.move_to_end(cache_key)
        self.hits += 1
        return vector

    def put(self, text: str, model: str, vector: list[float]) -> None:
        """Store a vector, evicting the least-recently-used entry if full."""
        cache_key = self.key(text, model)
        self._entries[cache_key] = vector
        self._entries.move_to_end(cache_key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        """Fraction of lookups served from cache."""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def clear(self) -> None:
        """Drop all cached vectors and reset counters."""
        self._entries.clear()
        self.hits = 0
        self.misses = 0


class EmbeddingService:
    """Generates embeddings for documents and queries."""

    def __init__(self, client: AsyncOpenAI | None = None, cache: EmbeddingCache | None = None) -> None:
        """Build the service.

        Args:
            client: Injected OpenAI client. Tests pass a fake; production leaves
                it unset so the client is built from settings.
            cache: Injected cache, mainly for tests that need to assert on hits.
        """
        self._client = client
        self._cache = cache if cache is not None else EmbeddingCache()
        self.model = settings.EMBEDDING_MODEL
        self.dimensions = settings.EMBEDDING_DIMENSIONS

    @property
    def client(self) -> AsyncOpenAI:
        """Lazily construct the provider client."""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY.get_secret_value(),
                base_url=settings.OPENAI_BASE_URL,
                timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS,
                max_retries=0,  # tenacity below owns retry policy; don't stack them
            )
        return self._client

    @property
    def cache(self) -> EmbeddingCache:
        """The embedding cache, exposed for metrics and tests."""
        return self._cache

    @staticmethod
    def _normalise(text: str) -> str:
        """Collapse whitespace so trivially different inputs share a cache entry.

        Also guards the provider against empty strings, which it rejects.
        """
        cleaned = " ".join(text.split())
        return cleaned if cleaned else " "

    def _split_batches(self, texts: Sequence[str]) -> list[list[str]]:
        """Split inputs into batches respecting both count and token ceilings.

        Args:
            texts: Normalised input strings.

        Returns:
            Batches, each safe to send as a single request.
        """
        batches: list[list[str]] = []
        current: list[str] = []
        current_chars = 0
        char_budget = _MAX_TOKENS_PER_REQUEST * _CHARS_PER_TOKEN

        for text in texts:
            text_chars = len(text)
            exceeds_count = len(current) >= settings.EMBEDDING_BATCH_SIZE
            exceeds_tokens = current and (current_chars + text_chars) > char_budget
            if exceeds_count or exceeds_tokens:
                batches.append(current)
                current, current_chars = [], 0
            current.append(text)
            current_chars += text_chars

        if current:
            batches.append(current)
        return batches

    @retry(
        stop=stop_after_attempt(settings.MAX_LLM_CALL_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIError)),
        reraise=True,
    )
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """Embed one batch, retrying transient provider failures.

        Raises:
            EmbeddingError: If the provider returns a vector of unexpected width,
                which would corrupt the index if written.
        """
        embedding_batch_size.observe(len(batch))
        response = await self.client.embeddings.create(
            model=self.model,
            input=batch,
            dimensions=self.dimensions,
        )

        # The API guarantees ordering by index, but sorting explicitly means a
        # future provider change cannot silently misalign vectors with chunks -
        # a corruption that would be nearly impossible to notice downstream.
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [item.embedding for item in ordered]

        for vector in vectors:
            if len(vector) != self.dimensions:
                raise EmbeddingError(
                    f"Provider returned a {len(vector)}-dimensional vector, expected {self.dimensions}. "
                    "EMBEDDING_DIMENSIONS and EMBEDDING_MODEL are inconsistent.",
                    context={"model": self.model, "expected": self.dimensions, "received": len(vector)},
                )
        return vectors

    async def embed_texts(self, texts: Iterable[str], *, use_cache: bool = True) -> list[list[float]]:
        """Embed many texts, preserving input order.

        Cached entries are served without a request; only the misses are sent,
        then results are stitched back into the original positions.

        Args:
            texts: Input strings.
            use_cache: Disable for bulk ingestion, where inputs are unique and
                caching them would only evict useful query vectors.

        Returns:
            One vector per input, in the same order.

        Raises:
            EmbeddingError: If the provider fails after retries.
        """
        normalised = [self._normalise(text) for text in texts]
        if not normalised:
            return []

        results: list[list[float] | None] = [None] * len(normalised)
        pending_indices: list[int] = []
        pending_texts: list[str] = []

        for index, text in enumerate(normalised):
            cached = self._cache.get(text, self.model) if use_cache else None
            if cached is not None:
                results[index] = cached
            else:
                pending_indices.append(index)
                pending_texts.append(text)

        if pending_texts:
            started = time.perf_counter()
            try:
                batches = self._split_batches(pending_texts)
                # Batches are sent sequentially. Firing them concurrently would
                # be faster but reliably trips provider rate limits on large
                # ingests, and the resulting 429 backoff is slower than serial.
                vectors: list[list[float]] = []
                for batch in batches:
                    vectors.extend(await self._embed_batch(batch))

                embedding_requests_total.labels(model=self.model, outcome="success").inc(len(batches))
            except Exception as exc:
                embedding_requests_total.labels(model=self.model, outcome="error").inc()
                logger.error(
                    "embedding_generation_failed",
                    model=self.model,
                    text_count=len(pending_texts),
                    error=str(exc),
                )
                raise EmbeddingError(
                    "Failed to generate embeddings.",
                    context={"model": self.model, "count": len(pending_texts), "error": str(exc)},
                ) from exc
            finally:
                retrieval_duration_seconds.labels(stage="embed").observe(time.perf_counter() - started)

            for index, text, vector in zip(pending_indices, pending_texts, vectors, strict=True):
                results[index] = vector
                if use_cache:
                    self._cache.put(text, self.model, vector)

        logger.debug(
            "embeddings_generated",
            total=len(normalised),
            from_cache=len(normalised) - len(pending_texts),
            computed=len(pending_texts),
            cache_hit_rate=round(self._cache.hit_rate, 3),
        )
        # Every slot is filled by construction; the cast documents that for type
        # checkers without a runtime cost.
        return [vector for vector in results if vector is not None]

    async def embed_query(self, query: str) -> list[float]:
        """Embed a single search query.

        Always cached: repeated queries are the common case during an incident.
        """
        vectors = await self.embed_texts([query], use_cache=True)
        if not vectors:
            raise EmbeddingError("Embedding a query returned no vector.", context={"query_length": len(query)})
        return vectors[0]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed document chunks for indexing.

        Cache is bypassed: ingestion inputs are near-unique, so caching them
        would evict query vectors that actually benefit from it.
        """
        return await self.embed_texts(texts, use_cache=False)

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        if self._client is not None:
            await self._client.close()
            self._client = None


_embedding_service: EmbeddingService | None = None
_service_lock = asyncio.Lock()


async def get_embedding_service() -> EmbeddingService:
    """Return the shared embedding service, constructing it once.

    The lock prevents two concurrent first-callers from each building a client
    and leaking one of them.
    """
    global _embedding_service
    if _embedding_service is None:
        async with _service_lock:
            if _embedding_service is None:
                _embedding_service = EmbeddingService()
    return _embedding_service


__all__ = ["EmbeddingCache", "EmbeddingService", "get_embedding_service"]
