# Operations

Running Aegis: what to watch, what the alerts mean, and what to do when it is
wrong.

## Contents

- [Metrics](#metrics)
- [Alerts](#alerts)
- [Logs](#logs)
- [Troubleshooting](#troubleshooting)
- [Tuning answer quality](#tuning-answer-quality)
- [Cost](#cost)
- [Routine maintenance](#routine-maintenance)

---

## Metrics

Exposed at `/metrics`. Source:
[`aegis/core/metrics.py`](../aegis/core/metrics.py).

### The four that matter most

| Metric | Watch for |
|---|---|
| `aegis_http_requests_total{status="5xx"}` | Above 5% of total — something is broken |
| `aegis_retrieval_results_returned` | Mass at `le="0"` — the agent is answering ungrounded |
| `aegis_agent_investigations_total{outcome}` | `failed` or sustained `truncated` |
| `aegis_llm_fallback_total` | Sustained non-zero — the primary model is effectively down |

The second is the one people forget. Empty retrieval does not produce an error —
the agent still answers, just without grounding. It is the most dangerous
failure mode this system has, and it is invisible unless you watch for it.

### Full inventory

**HTTP** — `aegis_http_requests_total{method,route,status}`,
`aegis_http_request_duration_seconds{method,route}`,
`aegis_http_requests_in_flight`

**LLM** — `aegis_llm_requests_total{model,outcome}`,
`aegis_llm_inference_duration_seconds{model}`,
`aegis_llm_stream_duration_seconds{model}`,
`aegis_llm_time_to_first_token_seconds{model}`,
`aegis_llm_tokens_total{model,direction}`,
`aegis_llm_fallback_total{from_model,to_model}`

**Retrieval** — `aegis_retrieval_queries_total{strategy,outcome}`,
`aegis_retrieval_duration_seconds{stage}`,
`aegis_retrieval_results_returned{strategy}`, `aegis_retrieval_top_score`,
`aegis_embedding_requests_total{model,outcome}`, `aegis_embedding_batch_size`

**Ingestion** — `aegis_ingestion_documents_total{source_type,outcome}`,
`aegis_ingestion_chunks_total{source_type}`,
`aegis_ingestion_duration_seconds{source_type}`

**Agent** — `aegis_agent_investigations_total{outcome}`,
`aegis_agent_node_duration_seconds{node}`,
`aegis_agent_tool_calls_total{tool,outcome}`,
`aegis_agent_tool_duration_seconds{tool}`, `aegis_agent_iterations`,
`aegis_agent_confidence`

**Infrastructure** — `aegis_db_pool_connections{pool,state}`,
`aegis_db_query_duration_seconds{operation}`,
`aegis_app_info{version,environment}`

### Label cardinality

Every label holds values from a closed set — model names, node names, status
*classes* (`2xx`/`4xx`/`5xx`), route *templates*. Never a user id, session id, or
raw path. Labelling on raw paths would create one series per UUID and eventually
take the scrape endpoint down; it would also let anyone probing random URLs do
it deliberately.

---

## Alerts

Defined in [`prometheus/alerts.yml`](../prometheus/alerts.yml).

Every rule alerts on a **symptom a user would notice**, not on a cause.
Alerting on causes ("CPU is high") produces pages nobody can act on.

| Alert | Severity | Means |
|---|---|---|
| `AegisApiDown` | critical | No successful scrape for 2 minutes |
| `AegisHighErrorRate` | critical | Over 5% of requests are 5xx |
| `AegisLLMFailures` | critical | Over 20% of model calls failing |
| `AegisInvestigationsFailing` | critical | Over 10% of investigations failing outright |
| `AegisHighLatency` | warning | p95 above 5s for 10 minutes |
| `AegisModelFallbackActive` | warning | Sustained failover from the primary |
| `AegisRetrievalReturningNothing` | warning | Over 30% of searches return nothing |
| `AegisEmbeddingFailures` | warning | Any sustained embedding failures |
| `AegisSlowRetrieval` | warning | A retrieval stage p95 above 2s |
| `AegisInvestigationsTruncated` | warning | Over 25% hit the iteration cap |
| `AegisDatabasePoolSaturated` | warning | Pool over 90% utilised |

Each carries a `runbook_url`. An alert without a runbook is an alert whose
recipient has to reinvent the diagnosis at 03:00 — which would be a poor look
for this project in particular.

---

## Logs

Structured throughout. Each line is an event name plus typed fields, so logs are
queryable rather than greppable:

```json
{
  "event": "retrieval_completed",
  "level": "info",
  "timestamp": "2026-08-15T10:23:04.123Z",
  "request_id": "8f2c1e...",
  "user_id": "...",
  "vector_hits": 12,
  "lexical_hits": 8,
  "returned": 8,
  "latency_ms": 41.2,
  "service": "aegis",
  "environment": "production"
}
```

### Useful queries

```
event="retrieval_completed" AND returned=0        # coverage gaps
event="llm_call_failed"                           # provider trouble
event="agent_iteration_cap_reached"               # agent not converging
event="document_ingestion_failed"                 # ingestion problems
event="token_type_mismatch"                       # someone replaying tokens
request_id="8f2c1e..."                            # one full request
```

`request_id` is on every line and returned in the `X-Request-ID` header, so a
user reporting a failure can hand over an id that finds the exact lines. Inbound
`X-Request-ID` is preserved, so a trace started upstream stays connected.

### Secret redaction

`aegis.utils.sanitization.redact_secrets` masks connection-string passwords,
bearer tokens, `key=value` secrets, and provider key prefixes. Use it before
logging anything a user pasted — configuration and log excerpts routinely
contain credentials, and logging them copies the secret into a second system
with different access controls and retention.

---

## Troubleshooting

### Searches return nothing

The single most consequential failure, because it is silent.

```bash
aegis stats                                    # is anything indexed?
aegis search "known phrase" --no-rerank        # is it a rerank problem?
aegis search "known phrase"                    # without a service filter
```

| Finding | Cause | Fix |
|---|---|---|
| `documents: 0` | Nothing ingested | `make ingest-samples`, or ingest your corpus |
| `unembedded_chunks > 0` | Ingestion failed partway | Re-run with `--force` |
| Works without `--service` | Wrong or missing service tag | Check front matter and directory layout |
| Works with `--no-rerank` | Reranker discarding good hits | Raise `RERANK_TOP_N`, or disable |
| Nothing works | pgvector missing | `CREATE EXTENSION vector;` then re-ingest |

### Reports are vague or generic

Almost always retrieval, not the model.

1. Check what the agent actually saw: the `citations` array on the response.
   Empty citations means it was reasoning without grounding.
2. Run the same query through `/knowledge/search`. If search returns nothing
   useful, no model will help.
3. Only after retrieval is confirmed good, consider `DEFAULT_LLM_MODEL=gpt-4o`.

Upgrading the model to compensate for bad retrieval is expensive and does not
work.

### Investigations are slow

Read the `timings_ms` on a search response and
`aegis_agent_node_duration_seconds` by node.

| Slow stage | Fix |
|---|---|
| `embed` | Provider latency. Check `aegis_embedding_requests_total` |
| `vector` | Tune HNSW `ef_search`; check pool saturation |
| `rerank` | `RERANK_ENABLED=false`, or lower `RERANK_TOP_N` |
| `investigate` | Lower `AGENT_MAX_TOOL_ITERATIONS`; use a faster model |
| `synthesize` | Lower `LLM_MAX_OUTPUT_TOKENS` |

### `database_unavailable` (503)

```bash
make check                    # is it reachable at all?
```

```sql
SELECT application_name, state, count(*)
FROM pg_stat_activity GROUP BY 1,2 ORDER BY 3 DESC;
```

A large `idle in transaction` count means a leaked transaction, not load. A
large `active` count with slow queries means genuine load.

Remember both pools count toward `max_connections` — see
[deployment.md](deployment.md#the-connection-budget).

### `llm_unavailable` (502)

Every model in the chain failed. Check the key, quota, and provider status.
`aegis_llm_fallback_total` shows whether failover was attempted.

### Sustained truncated investigations

`AGENT_MAX_TOOL_ITERATIONS` reached before converging. Usually poor retrieval —
the agent keeps searching for something it never finds. Fix retrieval first;
raise the cap only if the searches are genuinely productive.

---

## Tuning answer quality

Work in this order. Each step is cheaper and higher-leverage than the next.

**1. Coverage.** Does the corpus contain the answer at all?

```bash
aegis stats
```

No amount of tuning finds a runbook that was never written.

**2. Service tagging.** Is documentation reaching the right filter? Untagged
documents are globally searchable but never benefit from the strongest filter
available.

**3. Chunk size.** If retrieval finds the right *document* but the wrong
*section*, lower `CHUNK_TARGET_TOKENS` to ~256 and re-ingest with `--force`.

**4. Hybrid weight.** Missing exact error codes → lower
`HYBRID_VECTOR_WEIGHT` toward 0.5. Missing paraphrases → raise toward 0.85.

**5. Reranking.** Enable it, and raise `RERANK_TOP_N` so more candidates are
considered.

**6. Model.** Last resort, and only after confirming retrieval is good.

### Closing the loop

The feedback endpoint is the cheapest high-quality signal available:

```bash
curl -sX POST localhost:8000/api/v1/incidents/reports/$ID/feedback \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"was_helpful": false, "feedback": "Missed the gateway was already degraded."}'
```

And recording the true root cause turns every historical report for that
incident into a labelled example:

```bash
curl -sX PATCH localhost:8000/api/v1/incidents/$ID \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"status": "resolved", "root_cause": "Deploy raised concurrency without raising pool size."}'
```

It also makes the incident available to `find_similar_incidents` as confirmed
precedent — the strongest evidence class the agent has.

---

## Cost

Token consumption is exported directly:

```promql
sum by (model, direction) (increase(aegis_llm_tokens_total[24h]))
```

Multiply by your provider's unit prices.

Per investigation, roughly:

| Stage | Calls | Notes |
|---|---|---|
| Triage | 1 | Small prompt |
| Rerank | 1 per search | Skipped if `RERANK_ENABLED=false` |
| Investigate | 1–6 | Bounded by `AGENT_MAX_TOOL_ITERATIONS` |
| Synthesize | 1 | Largest output |
| Embedding | 2–5 | One per query variant |

The biggest levers, in order: disable reranking, lower the iteration cap, use a
smaller model, lower `RETRIEVAL_TOP_K` (less context per call).

Ingestion cost is one-time per document version, and content hashing means
re-ingesting an unchanged corpus is free.

---

## Routine maintenance

**Ingestion sync** — run nightly. Unchanged files cost one hash comparison each.

```bash
aegis ingest /path/to/runbooks
```

Exits non-zero if any document failed, so it works as a cron failure signal.

**Index health** — watch `unembedded_chunks` in `aegis stats`. Non-zero means a
partial ingestion failure.

**Checkpoint growth** — conversation history accumulates. Prune old threads:

```sql
DELETE FROM checkpoints
WHERE thread_id IN (
  SELECT id::text FROM sessions WHERE created_at < now() - interval '90 days'
);
```

**Vacuum** — `document_chunks` sees heavy delete/insert churn from re-ingestion.
Autovacuum usually handles it; after a large re-ingest, `VACUUM ANALYZE
document_chunks` is worth running explicitly so the planner has fresh
statistics.
