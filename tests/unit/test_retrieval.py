"""Tests for rank fusion and retrieval result handling.

Fusion is pure and deterministic, so it can be tested exhaustively without a
database. These tests pin the property that actually matters: a document found
by *both* arms should outrank one found by only one, because cross-arm agreement
is the signal hybrid retrieval exists to exploit.
"""

from __future__ import annotations

import uuid

from aegis.retrieval.hybrid import (
    RetrievalRequest,
    RetrievalResponse,
    reciprocal_rank_fusion,
)
from aegis.retrieval.vector_store import serialise_vector
from tests.conftest import make_search_result


class TestSerialiseVector:
    """pgvector literal formatting."""

    def test_formats_as_bracketed_csv(self):
        assert serialise_vector([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"

    def test_handles_negative_components(self):
        assert serialise_vector([-0.5, 0.5]).startswith("[-0.5")

    def test_empty_vector(self):
        assert serialise_vector([]) == "[]"


class TestReciprocalRankFusion:
    """RRF behaviour."""

    def test_empty_input_yields_empty_output(self):
        assert reciprocal_rank_fusion([]) == []
        assert reciprocal_rank_fusion([([], 1.0)]) == []

    def test_single_list_preserves_order(self):
        results = [make_search_result(content=f"doc {i}", score=1.0 - i * 0.1) for i in range(3)]
        fused = reciprocal_rank_fusion([(results, 1.0)])
        assert [r.content for r in fused] == ["doc 0", "doc 1", "doc 2"]

    def test_agreement_across_arms_beats_a_single_top_hit(self):
        shared = uuid.uuid4()
        # 'agreed' is rank 2 in both arms; 'vector_only' is rank 1 in one arm.
        vector_arm = [
            make_search_result(content="vector_only", strategy="vector"),
            make_search_result(content="agreed", chunk_id=shared, strategy="vector"),
        ]
        lexical_arm = [
            make_search_result(content="lexical_only", strategy="lexical"),
            make_search_result(content="agreed", chunk_id=shared, strategy="lexical"),
        ]
        fused = reciprocal_rank_fusion([(vector_arm, 0.5), (lexical_arm, 0.5)])
        assert fused[0].content == "agreed"

    def test_documents_found_in_both_arms_are_marked_hybrid(self):
        shared = uuid.uuid4()
        fused = reciprocal_rank_fusion(
            [
                ([make_search_result(chunk_id=shared, strategy="vector")], 0.5),
                ([make_search_result(chunk_id=shared, strategy="lexical")], 0.5),
            ]
        )
        assert len(fused) == 1
        assert fused[0].strategy == "hybrid"

    def test_single_arm_documents_keep_their_strategy(self):
        fused = reciprocal_rank_fusion(
            [
                ([make_search_result(content="a", strategy="vector")], 0.5),
                ([make_search_result(content="b", strategy="lexical")], 0.5),
            ]
        )
        assert {r.strategy for r in fused} == {"vector", "lexical"}

    def test_deduplicates_by_chunk_id(self):
        shared = uuid.uuid4()
        fused = reciprocal_rank_fusion(
            [
                ([make_search_result(chunk_id=shared)], 1.0),
                ([make_search_result(chunk_id=shared)], 1.0),
            ]
        )
        assert len(fused) == 1

    def test_scores_are_normalised_to_unit_interval(self):
        results = [make_search_result(content=f"doc {i}") for i in range(5)]
        fused = reciprocal_rank_fusion([(results, 1.0)])
        assert fused[0].score == 1.0
        assert all(0.0 <= r.score <= 1.0 for r in fused)

    def test_weights_shift_ranking(self):
        vector_arm = [make_search_result(content="from_vector", strategy="vector")]
        lexical_arm = [make_search_result(content="from_lexical", strategy="lexical")]

        vector_heavy = reciprocal_rank_fusion([(vector_arm, 0.9), (lexical_arm, 0.1)])
        lexical_heavy = reciprocal_rank_fusion([(vector_arm, 0.1), (lexical_arm, 0.9)])

        assert vector_heavy[0].content == "from_vector"
        assert lexical_heavy[0].content == "from_lexical"

    def test_larger_k_flattens_rank_influence(self):
        results = [make_search_result(content=f"doc {i}") for i in range(10)]
        tight = reciprocal_rank_fusion([(results, 1.0)], k=1)
        flat = reciprocal_rank_fusion([(results, 1.0)], k=1000)
        # With a large k the gap between rank 1 and rank 10 narrows.
        assert (tight[0].score - tight[-1].score) > (flat[0].score - flat[-1].score)

    def test_retains_instance_from_the_best_ranking_arm(self):
        shared = uuid.uuid4()
        vector_arm = [
            make_search_result(content="filler"),
            make_search_result(chunk_id=shared, content="from_vector_rank2", strategy="vector"),
        ]
        lexical_arm = [
            make_search_result(chunk_id=shared, content="from_lexical_rank1", strategy="lexical"),
        ]
        fused = reciprocal_rank_fusion([(vector_arm, 0.5), (lexical_arm, 0.5)])
        target = next(r for r in fused if r.chunk_id == shared)
        assert target.content == "from_lexical_rank1"


class TestRetrievalRequest:
    """Request defaults."""

    def test_top_k_defaults_to_configured_value(self):
        from aegis.core.config import settings

        assert RetrievalRequest(query="x").resolved_top_k() == settings.RETRIEVAL_TOP_K

    def test_explicit_top_k_wins(self):
        assert RetrievalRequest(query="x", top_k=3).resolved_top_k() == 3

    def test_rerank_override(self):
        assert RetrievalRequest(query="x", rerank=False).should_rerank() is False
        assert RetrievalRequest(query="x", rerank=True).should_rerank() is True


class TestRetrievalResponse:
    """Context rendering."""

    def _response(self, count: int = 3) -> RetrievalResponse:
        return RetrievalResponse(
            results=[make_search_result(content=f"passage {i}", heading_path=f"Doc > S{i}") for i in range(count)],
            query="why is checkout failing",
            strategy="hybrid",
            total_candidates=count,
            timings_ms={"total": 12.3},
        )

    def test_context_numbers_each_passage(self):
        context = self._response().to_context()
        assert "[1]" in context and "[2]" in context and "[3]" in context

    def test_context_includes_citations(self):
        assert "Doc > S0" in self._response().to_context()

    def test_empty_results_say_so_explicitly(self):
        empty = RetrievalResponse(results=[], query="q", strategy="hybrid", total_candidates=0, timings_ms={})
        assert empty.is_empty
        # Silence would invite the model to invent a runbook.
        assert "No relevant" in empty.to_context()

    def test_char_budget_truncates_from_the_bottom(self):
        response = self._response(count=10)
        full = response.to_context()
        limited = response.to_context(max_chars=200)
        assert len(limited) < len(full)
        # The most relevant passage must survive truncation.
        assert "[1]" in limited

    def test_citations_serialise(self):
        citations = self._response().citations()
        assert len(citations) == 3
        assert all("chunk_id" in citation for citation in citations)
        assert all("score" in citation for citation in citations)
