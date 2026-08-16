# Architecture decision records

Short records of decisions that were not obvious, including the options that
were rejected and why. The point is to save a future reader from re-litigating
a choice without knowing what it was weighed against.

| # | Decision | Status |
|---|---|---|
| [0001](0001-postgres-for-vectors.md) | PostgreSQL + pgvector instead of a dedicated vector database | Accepted |
| [0002](0002-hybrid-retrieval-rrf.md) | Hybrid retrieval fused by Reciprocal Rank Fusion | Accepted |
| [0003](0003-structured-graph.md) | A structured workflow graph instead of a generic agent loop | Accepted |
| [0004](0004-structured-report-schema.md) | Force the agent into a validated report schema | Accepted |
| [0005](0005-per-call-model-selection.md) | Per-call model selection instead of shared mutable state | Accepted |
| [0006](0006-typed-tokens.md) | Typed JWTs to close a token-confusion flaw | Accepted |

## Format

Each record states the **context** (what forced a decision), the **decision**,
the **alternatives** and why they lost, and the **consequences** — including the
costs we accepted.
