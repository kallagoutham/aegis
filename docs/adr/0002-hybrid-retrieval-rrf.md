# 0002 — Hybrid retrieval fused by Reciprocal Rank Fusion

**Status:** Accepted

## Context

Pure dense retrieval has a specific, well-documented failure mode: it misses
rare literal tokens. Embeddings average over a passage, so a unique identifier —
`NoBrokersAvailable`, `ERR_CONN_POOL_EXHAUSTED`, a pod name — contributes almost
nothing to the vector.

This matters more here than in most RAG applications. During an incident the
query is frequently *built* from those literals: an engineer pastes the error
string. Dense-only retrieval will confidently return topically-adjacent runbooks
and miss the one that names the error verbatim.

Pure lexical retrieval has the mirror failure: it misses every paraphrase. "The
service can't reach the database and everything is timing out" shares almost no
vocabulary with a runbook titled "Connection pool exhaustion".

Real queries contain both — prose describing the symptom, and pasted literals.

## Decision

Run both arms on every search and fuse the ranked lists with Reciprocal Rank
Fusion:

```
score(d) = Σ_arms  weight_arm / (k + rank_arm(d))
```

`k = 60` (`RRF_K`), vector weight 0.7 (`HYBRID_VECTOR_WEIGHT`), lexical weight
`1 - 0.7`.

## Alternatives considered

**Dense only.** Simplest. Rejected — the exact-token failure mode is precisely
the case incident response cares about.

**Weighted sum of normalised scores.** The intuitive approach, and unstable in
practice. Cosine similarity and `ts_rank_cd` are on different scales, and
`ts_rank_cd` is unbounded above. Normalising requires min/max over the result
set, which *shifts with every query* — so the same document scores differently
depending on what it was retrieved alongside, and any threshold you pick becomes
query-dependent.

**Learned fusion / a trained reranker.** Better, given training data. We have
none, and a cold-start incident tool cannot wait for it. RRF needs no training.

**Query routing** — classify each query as "lexical-ish" or "semantic-ish" and
pick one arm. Rejected because the classification is itself unreliable, and real
incident queries are genuinely both. Running both arms costs one extra query
against an already-warm index.

## Why RRF specifically

Three properties:

**Scale-free.** It uses only rank position. No calibration, no normalisation, no
query-dependent thresholds.

**Robust.** Cormack et al. (2009) showed RRF matching or beating more
sophisticated fusion methods across TREC collections, without tuning.

**It rewards agreement.** A document surfaced by *both* arms outranks one found
by either alone, even if it was never first anywhere:

```
agreed (rank 2 both arms)    = 0.5/62 + 0.5/62  = 0.01613   ← wins
vector_only (rank 1)          = 0.5/61           = 0.00820
```

That is exactly the signal we want. If both a semantic and a lexical matcher
independently surface a passage, it is very likely relevant.

`k = 60` damps the influence of the very top ranks, so one arm being confidently
wrong cannot dominate.

## Consequences

**Good**

- Recovers exact identifiers that dense retrieval drops.
- Recovers paraphrases that lexical retrieval drops.
- No calibration, no training data.
- `strategy` on each result (`vector` / `lexical` / `hybrid`) is exposed in the
  API, so you can see *why* something ranked where it did.

**Costs accepted**

- Two queries per search instead of one. They run concurrently against different
  indexes, so combined latency is the slower arm rather than the sum.
- Two indexes to maintain (HNSW and GIN), roughly doubling index storage.
- `HYBRID_VECTOR_WEIGHT` is a knob that needs corpus-specific tuning. Documented
  in [rag-pipeline.md](../rag-pipeline.md#tuning).

**Failure isolation**

If one arm errors, the search continues with the other and logs a warning.
Degraded results beat an error page during an incident.

## Implementation notes

`reciprocal_rank_fusion` returns **copies**, never mutating the input lists.
Rewriting caller-owned scores would make a second fusion over the same results
silently operate on already-fused values — a bug the test suite caught during
development.

Fused scores are normalised against the top result so `RETRIEVAL_MIN_SCORE` can
be expressed in a stable `[0, 1]` range.
