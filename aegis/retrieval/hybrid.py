"""Hybrid retrieval: dense + lexical search fused by Reciprocal Rank Fusion.

Neither retrieval strategy is sufficient alone, and their failure modes are
close to complementary:

* **Dense (vector) search** captures meaning. It finds the "database connection
  pool exhausted" runbook when the query says "service can't reach Postgres,
  lots of timeouts". It reliably *misses* rare literal tokens - error codes,
  ``NoBrokersAvailable``, a specific pod name - because embeddings average rare
  terms away.
* **Lexical (full-text) search** is exact. It nails those identifiers and misses
  every paraphrase.

During an incident, queries contain *both*: prose describing the symptom and
pasted literals from a stack trace. Running both arms and fusing them recovers
what either would drop.

**Why Reciprocal Rank Fusion rather than a weighted score sum.** Cosine
similarity and ``ts_rank_cd`` are not on a common scale, and normalising them
into one is unstable - the min/max of a result set shifts with every query, so
the same document scores differently depending on what it was retrieved
alongside. RRF ignores scores entirely and fuses on *rank*:

    score(d) = Σ_arms  weight_arm / (k + rank_arm(d))

That is scale-free, needs no calibration, and is empirically hard to beat
(Cormack et al., 2009). ``k`` (default 60) damps the influence of the very top
ranks so a single arm cannot dominate on its own.

An optional LLM reranking pass then re-scores the fused shortlist. Fusion is
cheap and recall-oriented; reranking is expensive and precision-oriented, so
running fusion wide and reranking narrow gets both.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from aegis.core.config import settings
from aegis.core.logging import logger
from aegis.core.metrics import (
    retrieval_duration_seconds,
    retrieval_queries_total,
    retrieval_results_returned,
    retrieval_top_score,
)
from aegis.models.knowledge import SourceType
from aegis.retrieval.embeddings import get_embedding_service
from aegis.retrieval.vector_store import (
    SearchResult,
    VectorStore,
)


@dataclass(slots=True)
class RetrievalRequest:
    """Everything needed to run one hybrid search.

    Bundled into an object rather than passed as loose keyword arguments so the
    agent tools, the HTTP API, and the eval harness all construct searches the
    same way, and so adding a knob does not change five signatures.
    """

    query: str
    top_k: int | None = None
    source_types: Sequence[SourceType] | None = None
    service: str | None = None
    document_ids: Sequence[uuid.UUID] | None = None
    rerank: bool | None = None
    min_score: float | None = None

    def resolved_top_k(self) -> int:
        """Number of results to return after all stages."""
        return self.top_k or settings.RETRIEVAL_TOP_K

    def should_rerank(self) -> bool:
        """Whether the reranking stage is enabled for this request."""
        return settings.RERANK_ENABLED if self.rerank is None else self.rerank


@dataclass(slots=True)
class RetrievalResponse:
    """Search results plus the timing breakdown that produced them.

    The per-stage timings are not decoration: when retrieval is slow during an
    incident, knowing whether the cost is embedding, the index, or reranking is
    the difference between a one-line config change and an afternoon of guessing.
    """

    results: list[SearchResult]
    query: str
    strategy: str
    total_candidates: int
    timings_ms: dict[str, float]
    reranked: bool = False

    @property
    def is_empty(self) -> bool:
        """Whether nothing was found."""
        return not self.results

    def to_context(self, max_chars: int | None = None) -> str:
        """Render results as a numbered context block for a prompt.

        Args:
            max_chars: Optional budget. Blocks are added in rank order until the
                budget is exhausted, so truncation drops the least relevant
                material rather than cutting mid-sentence at an arbitrary point.

        Returns:
            The formatted context, or an explicit "no results" marker. Saying so
            plainly matters - a silently empty context invites the model to
            invent a runbook that does not exist.
        """
        if not self.results:
            return "No relevant knowledge base entries were found for this query."

        blocks: list[str] = []
        used = 0
        for index, result in enumerate(self.results, start=1):
            block = result.to_context_block(index)
            if max_chars is not None and used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        return "\n\n---\n\n".join(blocks)

    def citations(self) -> list[dict[str, object]]:
        """Serialise results as citation records for an investigation report."""
        return [result.to_dict() for result in self.results]


def reciprocal_rank_fusion(
    ranked_lists: Sequence[tuple[Sequence[SearchResult], float]],
    k: int = 60,
) -> list[SearchResult]:
    """Fuse several ranked lists into one by Reciprocal Rank Fusion.

    Args:
        ranked_lists: ``(results, weight)`` pairs. Each list must already be
            sorted best-first; only position is used, never the raw score.
        k: Smoothing constant. Larger values flatten the contribution of top
            ranks, making fusion more democratic across arms.

    Returns:
        A newly allocated list ordered by fused score. Each element is a *copy*
        of the input result with ``score`` replaced by its fused value and
        ``strategy`` marking which arms found it. A document surfaced by *both*
        arms outranks one found by either alone even if it was never first
        anywhere - that agreement is precisely the signal fusion exists to
        exploit.

        Copying rather than mutating in place is deliberate: the input lists
        belong to the caller, and rewriting their scores would make a second
        fusion over the same results silently operate on already-fused values.
    """
    from dataclasses import replace

    fused_scores: dict[uuid.UUID, float] = {}
    best_instance: dict[uuid.UUID, SearchResult] = {}
    best_rank: dict[uuid.UUID, int] = {}
    found_by: dict[uuid.UUID, set[str]] = {}

    for results, weight in ranked_lists:
        for rank, result in enumerate(results, start=1):
            key = result.chunk_id
            fused_scores[key] = fused_scores.get(key, 0.0) + weight / (k + rank)
            found_by.setdefault(key, set()).add(result.strategy)
            # Keep the instance from whichever arm ranked it highest, so the
            # retained content and metadata come from the strongest match.
            if key not in best_rank or rank < best_rank[key]:
                best_rank[key] = rank
                best_instance[key] = result

    ordered_keys = sorted(fused_scores, key=lambda key: fused_scores[key], reverse=True)

    # Normalise fused scores against the best one so downstream thresholds
    # (RETRIEVAL_MIN_SCORE) can be expressed in a stable [0, 1] range.
    top_score = fused_scores[ordered_keys[0]] if ordered_keys else 0.0

    fused: list[SearchResult] = []
    for key in ordered_keys:
        strategies = found_by[key]
        fused.append(
            replace(
                best_instance[key],
                score=fused_scores[key] / top_score if top_score > 0 else 0.0,
                strategy="hybrid" if len(strategies) > 1 else next(iter(strategies)),
            )
        )
    return fused


class HybridRetriever:
    """Runs both retrieval arms, fuses them, and optionally reranks."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the retriever to a database session."""
        self.session = session
        self.store = VectorStore(session)

    async def search(self, request: RetrievalRequest) -> RetrievalResponse:
        """Execute a hybrid search end to end.

        Pipeline: embed the query, run both arms concurrently, fuse by RRF,
        optionally rerank, threshold, truncate.

        Args:
            request: The search specification.

        Returns:
            Ranked results with per-stage timings.
        """
        started = time.perf_counter()
        timings: dict[str, float] = {}
        top_k = request.resolved_top_k()

        # --- Stage 1: embed the query -------------------------------------
        stage_start = time.perf_counter()
        embedding_service = await get_embedding_service()
        query_embedding = await embedding_service.embed_query(request.query)
        timings["embed"] = (time.perf_counter() - stage_start) * 1000

        # --- Stage 2: run both arms concurrently --------------------------
        # These hit different indexes and neither depends on the other, so the
        # combined latency is the slower arm rather than their sum.
        stage_start = time.perf_counter()
        vector_task = self.store.vector_search(
            query_embedding,
            limit=settings.RETRIEVAL_CANDIDATE_K,
            source_types=request.source_types,
            service=request.service,
            document_ids=request.document_ids,
        )
        lexical_task = self.store.lexical_search(
            request.query,
            limit=settings.RETRIEVAL_CANDIDATE_K,
            source_types=request.source_types,
            service=request.service,
            document_ids=request.document_ids,
        )
        vector_results, lexical_results = await asyncio.gather(vector_task, lexical_task, return_exceptions=True)

        # One arm failing degrades quality but must not fail the search - during
        # an incident, partial results beat an error page.
        if isinstance(vector_results, BaseException):
            logger.warning("vector_arm_failed", error=str(vector_results))
            vector_results = []
        if isinstance(lexical_results, BaseException):
            logger.warning("lexical_arm_failed", error=str(lexical_results))
            lexical_results = []
        timings["retrieve"] = (time.perf_counter() - stage_start) * 1000

        # --- Stage 3: fuse -------------------------------------------------
        stage_start = time.perf_counter()
        vector_weight = settings.HYBRID_VECTOR_WEIGHT
        fused = reciprocal_rank_fusion(
            [
                (vector_results, vector_weight),
                (lexical_results, 1.0 - vector_weight),
            ],
            k=settings.RRF_K,
        )
        timings["fuse"] = (time.perf_counter() - stage_start) * 1000
        retrieval_duration_seconds.labels(stage="fuse").observe(timings["fuse"] / 1000)

        total_candidates = len(fused)

        # --- Stage 4: rerank -----------------------------------------------
        reranked = False
        if request.should_rerank() and len(fused) > 1:
            stage_start = time.perf_counter()
            try:
                fused = await self._rerank(request.query, fused[: settings.RERANK_TOP_N])
                reranked = True
            except Exception as exc:
                # Reranking is an enhancement. Losing it costs precision, not
                # correctness, so fall back to fusion order rather than failing.
                logger.warning("rerank_failed_using_fusion_order", error=str(exc))
            timings["rerank"] = (time.perf_counter() - stage_start) * 1000
            retrieval_duration_seconds.labels(stage="rerank").observe(timings["rerank"] / 1000)

        # --- Stage 5: threshold and truncate --------------------------------
        min_score = request.min_score if request.min_score is not None else settings.RETRIEVAL_MIN_SCORE
        if min_score > 0:
            fused = [result for result in fused if result.score >= min_score]
        results = fused[:top_k]

        timings["total"] = (time.perf_counter() - started) * 1000

        retrieval_queries_total.labels(strategy="hybrid", outcome="success").inc()
        retrieval_results_returned.labels(strategy="hybrid").observe(len(results))
        if results:
            retrieval_top_score.observe(results[0].score)

        logger.info(
            "retrieval_completed",
            query_length=len(request.query),
            vector_hits=len(vector_results),
            lexical_hits=len(lexical_results),
            fused_candidates=total_candidates,
            returned=len(results),
            reranked=reranked,
            service_filter=request.service,
            latency_ms=round(timings["total"], 1),
        )

        return RetrievalResponse(
            results=results,
            query=request.query,
            strategy="hybrid",
            total_candidates=total_candidates,
            timings_ms={key: round(value, 2) for key, value in timings.items()},
            reranked=reranked,
        )

    async def _rerank(self, query: str, candidates: list[SearchResult]) -> list[SearchResult]:
        """Re-score candidates with an LLM acting as a cross-encoder.

        Fusion judges each document against the query independently. A
        cross-encoder sees query and passage *together*, so it can tell that a
        runbook mentioning "connection pool" is about HTTP clients rather than
        the database - a distinction embeddings routinely miss.

        All candidates are scored in one call rather than one call each: N
        requests would cost N times the latency for no accuracy gain, and seeing
        the candidates side by side makes the model's relative judgements more
        consistent.

        Args:
            query: The original search query.
            candidates: Fusion-ordered shortlist.

        Returns:
            Candidates re-sorted by relevance. Any candidate the model omits
            keeps its fusion score, so nothing is silently dropped.
        """
        from aegis.services.llm import get_llm_service

        # Truncate passages: the reranker only needs enough text to judge topical
        # relevance, and full chunks would multiply prompt cost for no benefit.
        listing = "\n\n".join(
            f"[{index}] {result.citation()}\n{result.content[:600]}" for index, result in enumerate(candidates)
        )
        prompt = (
            "You are scoring how well each passage answers an engineer's question "
            "during a production incident.\n\n"
            f"QUESTION: {query}\n\n"
            f"PASSAGES:\n{listing}\n\n"
            "Score every passage from 0.0 (irrelevant) to 1.0 (directly answers it). "
            "Reward passages containing concrete diagnostic steps, error signatures, or "
            "configuration details. Penalise passages that merely share vocabulary with "
            "the question.\n"
            'Respond with JSON only: {"scores": [{"index": 0, "score": 0.9}, ...]}'
        )

        llm_service = await get_llm_service()
        raw = await llm_service.complete_json(
            prompt,
            model=settings.RERANK_MODEL,
            max_tokens=1000,
        )

        scores: dict[int, float] = {}
        for entry in raw.get("scores", []):
            try:
                index = int(entry["index"])
                if 0 <= index < len(candidates):
                    scores[index] = max(0.0, min(1.0, float(entry["score"])))
            except (KeyError, TypeError, ValueError):
                # One malformed entry should not discard an otherwise good
                # reranking pass; that candidate simply keeps its fusion score.
                continue

        if not scores:
            raise ValueError("Reranker returned no usable scores")

        for index, result in enumerate(candidates):
            if index in scores:
                result.score = scores[index]

        candidates.sort(key=lambda result: result.score, reverse=True)
        logger.debug("rerank_applied", candidates=len(candidates), scored=len(scores))
        return candidates


__all__ = [
    "HybridRetriever",
    "RetrievalRequest",
    "RetrievalResponse",
    "reciprocal_rank_fusion",
]
