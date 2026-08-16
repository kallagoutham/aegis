# 0001 — PostgreSQL + pgvector instead of a dedicated vector database

**Status:** Accepted

## Context

Aegis needs approximate nearest-neighbour search over embedded documentation.
The obvious candidates are a purpose-built vector database (Qdrant, Weaviate,
Pinecone, Milvus) or the `pgvector` extension in the PostgreSQL instance we
already run for relational data.

The corpus is operational documentation: runbooks, postmortems, architecture
notes, captured logs. Realistically thousands to low millions of chunks for a
large engineering organisation. Not billions.

## Decision

Use PostgreSQL 16 with `pgvector`, HNSW indexing, cosine distance.

## Alternatives considered

**Dedicated vector database.** Better raw ANN performance at very large scale,
richer filtering DSLs, and purpose-built tooling.

Rejected because at our scale the performance advantage is not observable, and
it costs three things that matter more:

- *An extra service to deploy, monitor, back up, and upgrade.* For a tool whose
  whole purpose is reducing operational burden during incidents, adding a
  service is a poor trade.
- *Transactional consistency.* With pgvector, a document and its chunks are
  written in one transaction. With a separate store, an ingestion failure
  halfway leaves the two out of sync, and reconciling them is a background job
  nobody writes until it has already caused a confusing bug.
- *Single-query filtering.* Retrieval filters by service, source type, and
  sometimes document id. In Postgres that is one query with the vector search.
  Split across systems it becomes either a pre-filter round trip or a
  post-filter that silently reduces recall below `top_k`.

**In-memory index (FAISS) with periodic rebuilds.** Fastest, and wrong for
incremental ingestion — the index would need rebuilding on every document
change, and it does not survive a restart.

## Consequences

**Good**

- One database. One backup. One failover story.
- Embeddings are transactionally consistent with their documents.
- Relational filters and vector search compose in a single query.
- Lexical search comes free from `tsvector`/GIN in the same table, which is what
  made hybrid retrieval cheap to build (see [0002](0002-hybrid-retrieval-rrf.md)).

**Costs accepted**

- A hard ceiling somewhere in the tens of millions of chunks, beyond which a
  dedicated store would win. Well past our target.
- HNSW index builds are memory-hungry on large bulk ingests.
- pgvector's filtering is applied *after* the ANN search, so a highly selective
  filter can return fewer than `limit` rows. Mitigated by pulling
  `RETRIEVAL_CANDIDATE_K` (40) candidates per arm rather than `top_k` (8).

**Migration path**

`VectorStore` is the only module issuing vector SQL. Swapping backends means
reimplementing that one class against the same interface. The abstraction is
deliberately thin — not a generic ORM over vector stores, just a boundary in the
right place.

## Notes on index choice

HNSW over IVFFlat. IVFFlat requires a training step over representative data and
its clusters degrade as rows are added afterwards. Our access pattern is
incremental — runbooks trickle in — so an index that needs periodic retraining
would quietly lose recall between retrainings. HNSW needs no training and
handles incremental inserts well, at the cost of a larger index and slower
builds.
