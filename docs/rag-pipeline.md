# The retrieval pipeline

How a markdown file becomes a cited passage in an incident report.

## Contents

- [Ingestion](#ingestion)
- [Chunking](#chunking)
- [Embeddings](#embeddings)
- [Storage and indexes](#storage-and-indexes)
- [Hybrid search](#hybrid-search)
- [Rank fusion](#rank-fusion)
- [Reranking](#reranking)
- [Tuning](#tuning)

---

## Ingestion

Source: [`aegis/ingestion/`](../aegis/ingestion/)

```
load ──► normalise ──► hash ──► unchanged? ──skip──► done
                                    │
                                 changed
                                    ▼
                              chunk ──► embed ──► replace chunks
```

### Loading

[`loaders.py`](../aegis/ingestion/loaders.py) answers four questions about a
blob of bytes:

| Question | Sources, in priority order |
|---|---|
| What is the text? | decode → strip front matter → normalise whitespace |
| What is it called? | front matter `title` → first `#` heading → filename |
| What service? | front matter `service` → directory convention → `service:` title prefix |
| What type? | front matter `type` → directory name → file extension |

Getting **service** right matters more than the rest combined. It is the most
selective retrieval filter available — during a payments incident, restricting
to payments documentation removes most of the corpus and most of the
opportunity for a confidently irrelevant answer.

When no signal is available, `service` is left `None` rather than guessed. A
wrong service tag is worse than none: it hides the document from exactly the
searches that should find it.

Directory convention:

```
data/runbooks/payments/gateway-timeouts.md   →  service=payments,  type=runbook
data/postmortems/2026-07-checkout.md         →  service=None,      type=postmortem
```

### Idempotence

`content_hash` is a SHA-256 of the **normalised** text. Re-ingesting an
unchanged file is a no-op decided before any embedding cost is incurred.

Normalising before hashing means line-ending changes, trailing whitespace, and
collapsed blank-line runs do not trigger a pointless re-index. Hashing file
bytes instead would re-embed a thousand runbooks because someone's editor
rewrote the newlines.

Use `--force` when the *source* is identical but the derived index is stale —
after changing `CHUNK_TARGET_TOKENS` or `EMBEDDING_MODEL`.

### Failure isolation

Each document is its own transaction. A failure marks that document `FAILED`
with the reason on the row and the run continues:

```bash
$ aegis ingest ./runbooks
Processed : 412
Indexed   : 409
Unchanged : 0
Failed    : 3

Failures:
  ./runbooks/legacy/old.md: Document produced no chunks (empty or unparseable).
```

A batch that fails halfway leaves the successful documents indexed and
queryable.

---

## Chunking

Source: [`aegis/ingestion/chunking.py`](../aegis/ingestion/chunking.py)

### Why naive chunking fails here

Split this runbook every 500 characters:

```markdown
## Payment service: 503 from checkout
1. Check the connection pool: `kubectl exec ... -- pgbouncer -s`
2. If saturated, scale replicas: `kubectl scale deploy/payments --replicas=6`
```

and you get a chunk containing step 2 with no indication of which failure it
addresses. Retrieved during an incident, that reads as authoritative advice
detached from its precondition — worse than returning nothing, because the
responder will act on it.

Retrieval can only ever return a chunk. If the boundary is in the wrong place,
no amount of embedding quality or reranking recovers the lost context.

### `MarkdownChunker`

1. Walk line by line, tracking the heading stack and whether we are inside a
   fenced code block. Headings inside fences are literal text — a `# comment`
   in a bash example must not restructure the document.
2. Emit a section per heading region, tagged with its full breadcrumb.
3. Pack sections into chunks up to `CHUNK_TARGET_TOKENS`. Oversized sections
   split on paragraph → sentence → hard boundary, in that order, each level
   reached only when the previous produced something that still does not fit.
4. Merge chunks below `CHUNK_MIN_TOKENS` into a neighbour — but only within the
   same heading path.
5. Apply overlap — again, only within a heading path.

### The breadcrumb is the point

Every chunk carries `heading_path`:

```
"Payments Service Runbook > Troubleshooting > 503 errors from checkout"
```

Two things use it:

**Embedding.** `Chunk.with_heading_context()` prepends the breadcrumb before
embedding, so the vector encodes where the passage sits, not just its body
text. This measurably improves retrieval for queries that name a service or
symptom without repeating the body's vocabulary.

**Citation.** A report can say *which section* answered the question, which is
what makes a claim checkable.

### Boundary discipline

Both merging and overlap refuse to cross a heading path. Bleeding the end of
"Rollback procedure" into the start of "Escalation contacts" would create a
chunk that misrepresents both — and it would look perfectly plausible.

### `LogChunker`

Logs need different handling:

- **Entries, not sentences.** A log entry is atomic.
- **Stack traces stay whole.** Continuation lines (indented, `at ...`,
  `Caused by:`) belong to the entry above. A trace cut in half loses the frame
  that identifies the failing code.
- **Time ranges are metadata.** Each chunk records its first and last
  timestamp, so the agent can correlate a chunk against an incident window
  instead of guessing whether it is relevant.

---

## Embeddings

Source: [`aegis/retrieval/embeddings.py`](../aegis/retrieval/embeddings.py)

Three optimisations, in order of impact:

**Batching.** `EMBEDDING_BATCH_SIZE` (default 64) inputs per request. Embedding
1,000 chunks one at a time is 1,000 round trips; batched it is 16. Batches are
further split if they would exceed the provider's per-request token ceiling.

Batches are sent **sequentially**, not concurrently. Firing them in parallel is
faster in principle and reliably trips rate limits on large ingests, where the
resulting 429 backoff is slower than serial.

**Caching.** A bounded LRU keyed on `SHA-256(model + text)`. Query embeddings
repeat constantly — the same alert text gets investigated by three engineers,
and the agent re-searches similar phrasings within one investigation. The model
is part of the key because vectors from different models live in incomparable
spaces; mixing them produces silently meaningless similarity scores.

Document embedding **bypasses** the cache: ingestion inputs are near-unique, and
caching them would only evict query vectors that actually benefit.

**Failing in the right place.** A failed embedding during *ingestion* marks that
document failed and moves on. A failed embedding during *search* raises,
because silently returning zero results is indistinguishable from "the runbook
does not exist" — the worst possible answer during an incident.

Vector width is validated on every response. A provider returning the wrong
dimension would corrupt the index if written.

---

## Storage and indexes

Source: [`aegis/models/knowledge.py`](../aegis/models/knowledge.py)

### Why Postgres rather than a dedicated vector database

The corpus here is operational documentation — thousands to low millions of
chunks, not billions. At that scale pgvector's HNSW index is comfortably fast,
and using the database we already run:

- removes an entire service from the deployment,
- keeps embeddings transactionally consistent with their parent documents,
- lets one query join vector hits against relational filters (service, type,
  recency) without a second network hop or a stale replica.

### The indexes

| Index | Type | Backs |
|---|---|---|
| `ix_chunks_embedding_hnsw` | HNSW, `vector_cosine_ops` | Dense retrieval |
| `ix_chunks_content_tsv` | GIN over `tsvector` | Lexical retrieval |
| `ix_chunks_type_service` | B-tree composite | Filtering |
| `ix_documents_service_trgm` | GIN trigram | Fuzzy service matching |

**HNSW over IVFFlat**: HNSW needs no training step and keeps good recall as the
corpus grows incrementally — exactly the access pattern here, where runbooks
trickle in rather than arriving as one bulk load. IVFFlat's clusters degrade as
data is added after training.

**`content_tsv` is a generated column.** Postgres recomputes it on write, so it
can never drift out of sync with `content` the way an application-maintained
column eventually does.

### Denormalisation

`source_type` and `service` are copied from `documents` onto `document_chunks`.
Retrieval filters on them on every query, and a copied column with its own index
is far cheaper than joining back inside a vector search.

---

## Hybrid search

Source: [`aegis/retrieval/vector_store.py`](../aegis/retrieval/vector_store.py)

### The two arms are complementary

| | Finds | Misses |
|---|---|---|
| **Dense** | Paraphrase, meaning, symptom descriptions | Rare literal tokens — error codes, `NoBrokersAvailable`, pod names |
| **Lexical** | Exact identifiers | Everything phrased differently |

During an incident a query contains *both*: prose describing the symptom, and
pasted literals from a stack trace. Running both arms and fusing recovers what
either would drop.

### Vector arm

```sql
SELECT ..., 1 - (c.embedding <=> CAST(:query_vector AS vector)) AS score
FROM document_chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.embedding IS NOT NULL
ORDER BY c.embedding <=> CAST(:query_vector AS vector)
LIMIT :limit
```

Two details matter:

**Ordering is on the bare operator, not the computed alias.** Sorting by
`1 - (...)` would force a sequential scan over every chunk. Only the raw
distance expression lets the planner use the HNSW index.

**The vector is bound as a string and cast.** pgvector's driver-native binding
requires per-connection type registration, which is fragile across pooled async
connections. Serialising to `'[0.1,0.2,...]'` and casting is driver-agnostic,
still a bound parameter, and no slower at realistic dimensions.

### Lexical arm

```sql
WITH q AS (SELECT websearch_to_tsquery('english', :query) AS query)
SELECT ..., ts_rank_cd(c.content_tsv, q.query) AS score
FROM document_chunks c CROSS JOIN q
WHERE c.content_tsv @@ q.query
ORDER BY score DESC
```

**`websearch_to_tsquery`, not `to_tsquery`.** It parses Google-style syntax
(quoted phrases, `OR`, leading `-`) and — critically — never raises on
malformed input. `to_tsquery` throws a syntax error on an unbalanced quote in a
pasted log line, turning a search into a 500.

**`ts_rank_cd`, not `ts_rank`.** Cover density rewards matches whose terms
appear near each other, which matters when searching a multi-token error string.

`ts_rank_cd` is unbounded above, so absolute values are meaningless. Scores are
scaled by the top hit into `[0, 1]`; only relative order survives, which is all
fusion consumes.

---

## Rank fusion

Source: [`aegis/retrieval/hybrid.py`](../aegis/retrieval/hybrid.py)

### Why not a weighted score sum

Cosine similarity and `ts_rank_cd` are not on a common scale, and normalising
them into one is unstable: the min and max of a result set shift with every
query, so the same document scores differently depending on what it was
retrieved alongside. Any threshold you pick becomes query-dependent.

### Reciprocal Rank Fusion

```
score(d) = Σ_arms  weight_arm / (k + rank_arm(d))
```

Scale-free, needs no calibration, and empirically hard to beat (Cormack et al.,
2009). `k` (default 60) damps the influence of the very top ranks so one arm
cannot dominate alone.

The property that matters: **a document found by both arms outranks one found by
either alone, even if it was never first anywhere.** That agreement is precisely
the signal hybrid retrieval exists to exploit.

Worked example — `agreed` is rank 2 in both arms, `vector_only` is rank 1 in one:

```
agreed       = 0.5/(60+2) + 0.5/(60+2)  = 0.01613   ← wins
vector_only  = 0.5/(60+1)               = 0.00820
lexical_only = 0.5/(60+1)               = 0.00820
```

Fused results are marked `strategy="hybrid"` when both arms found them, which
surfaces in the API so you can see *why* something ranked where it did.

Fusion returns **copies**, never mutating the input lists. Rewriting caller-owned
scores would make a second fusion over the same results silently operate on
already-fused values.

---

## Reranking

Optional, controlled by `RERANK_ENABLED`.

Fusion judges each document against the query independently. A cross-encoder
sees query and passage *together*, so it can tell that a runbook mentioning
"connection pool" is about HTTP clients rather than the database — a distinction
embeddings routinely miss.

All candidates are scored in **one** call, not one call each: N requests would
cost N× the latency for no accuracy gain, and seeing candidates side by side
makes the model's relative judgements more consistent.

Reranking is an enhancement. If it fails, retrieval falls back to fusion order
and logs a warning — losing it costs precision, not correctness.

**Cost**: one extra LLM call per search. Disable it for latency-sensitive
lookups; keep it when answer quality matters more than 300ms.

---

## Tuning

| Symptom | Knob | Direction |
|---|---|---|
| Misses exact error codes | `HYBRID_VECTOR_WEIGHT` | Lower toward `0.5` |
| Misses paraphrased queries | `HYBRID_VECTOR_WEIGHT` | Raise toward `0.85` |
| Right document, wrong section | `CHUNK_TARGET_TOKENS` | Lower to `256`, re-ingest with `--force` |
| Chunks lack context | `CHUNK_TARGET_TOKENS` / `CHUNK_OVERLAP_TOKENS` | Raise |
| Relevant results ranked low | `RERANK_ENABLED` | Enable; raise `RERANK_TOP_N` |
| Searches return nothing | — | Check coverage: `aegis stats`, then `list_documented_services` |
| Retrieval too slow | `RERANK_ENABLED` | Disable; lower `RETRIEVAL_CANDIDATE_K` |

Every search returns per-stage timings, so you can see which stage to attack:

```json
{ "timings_ms": { "embed": 41.2, "retrieve": 18.7, "fuse": 0.3, "rerank": 380.1, "total": 440.3 } }
```

Diagnose from the terminal:

```bash
aegis search "connection pool exhausted" --service platform
aegis search "connection pool exhausted" --no-rerank   # isolate rerank cost
aegis stats                                            # coverage and backlog
```

A non-zero `unembedded_chunks` in `aegis stats` means an ingestion run failed
partway through — re-run with `--force`.
