# Architecture

## The problem being solved

During a production incident, an engineer's time goes to three things:

1. **Reading evidence** — logs, dashboards, traces. Mostly volume, little signal.
2. **Recalling procedure** — which runbook covers this, and where is it.
3. **Reasoning** — forming a hypothesis and deciding what to check next.

An LLM is good at (3) and useless at (1) and (2) *unless* it is given the
material. A model without retrieval will still answer — fluently, plausibly,
and from parametric memory about software in general rather than about your
deployment. During an incident that is worse than silence, because it costs the
responder time to chase.

Aegis is therefore structured so that **evidence gathering is not optional**.
The agent does not decide whether to look things up; the graph does it before
the model reasons at all.

## System shape

```
                    ┌──────────────────────────────────────┐
   HTTP ──────────► │  aegis.api        routing, auth,     │
                    │                   validation, limits │
                    └──────────────┬───────────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
 ┌─────────────────┐   ┌──────────────────────┐   ┌──────────────────┐
 │ aegis.ingestion │   │ aegis.core.langgraph │   │ aegis.analysis   │
 │ load → chunk    │   │ triage → gather →    │   │ parse → cluster  │
 │ → embed → index │   │ investigate ⇄ tools  │   │ → detect         │
 └────────┬────────┘   │ → synthesize         │   └────────┬─────────┘
          │            └──────────┬───────────┘            │
          │                       │                        │
          │            ┌──────────▼───────────┐            │
          └───────────►│ aegis.retrieval      │◄───────────┘
                       │ embeddings, vector   │
                       │ store, hybrid fusion │
                       └──────────┬───────────┘
                                  │
                       ┌──────────▼───────────┐    ┌─────────────────┐
                       │ aegis.services       │    │ OpenAI-compatible│
                       │ async DB, LLM        │───►│ provider         │
                       └──────────┬───────────┘    └─────────────────┘
                                  │
                       ┌──────────▼───────────┐
                       │ PostgreSQL + pgvector│
                       │ tables, HNSW, GIN,   │
                       │ LangGraph checkpoints│
                       └──────────────────────┘
```

## Where the boundaries are, and why

### `api` never contains logic

Endpoints validate, delegate, and serialise. They raise domain exceptions from
[`core/exceptions.py`](../aegis/core/exceptions.py), never `HTTPException`.

This is what makes the business logic testable without HTTP, and it is why a
single set of handlers in [`main.py`](../aegis/main.py) can guarantee every
error response has the same shape. Scatter `HTTPException` through the codebase
and the error contract becomes whatever each author felt like that day.

### `retrieval` does not know about agents

`HybridRetriever` takes a query and returns ranked passages. It has no idea an
LLM exists (except for the optional rerank stage, which is injected).

That separation is what lets `/api/v1/knowledge/search` exist as a first-class
endpoint. An engineer who already knows what they're looking for is better
served by search results than by waiting for a model to summarise them.

### `analysis` has no dependencies at all

Log parsing and clustering are pure functions over strings. No database, no
network, no API key. That is why `aegis analyze file.log` works with nothing
configured, and why the log tests run in milliseconds.

### `services` owns all I/O

Two things talk to the outside world: the database and the model provider.
Both live here, both are lazily constructed, and both are shared process-wide.

Lazy construction matters more than it sounds. Building the engine at import
time binds the connection pool to whichever event loop happened to import the
module — which breaks under pytest-asyncio, where each test may get a fresh
loop.

## Data flow: ingestion

```
file/upload ──► load ──► normalise ──► hash ──► unchanged? ──► skip
                                                    │
                                                    ▼ changed
                                                  chunk
                                                    │
                                        (prepend heading breadcrumb)
                                                    ▼
                                                  embed
                                                    │
                                                    ▼
                                        delete old chunks, insert new
```

Two properties are worth calling out.

**The hash is computed over normalised text, not file bytes.** A file that
differs only in line endings hashes identically and is correctly skipped.
Hashing raw bytes would re-embed the entire corpus every time someone opened a
file in a different editor.

**Chunks are replaced wholesale, never upserted by index.** Chunk boundaries
shift when a document is edited, so chunk *n* of the new version has no
meaningful relationship to chunk *n* of the old one. Matching them up would
produce chunks whose embedding describes text they no longer contain — a
corruption that is nearly impossible to notice, because search still returns
results, just subtly wrong ones.

**Each document is its own transaction.** One malformed file marks itself
`FAILED` with the reason on the row, and the run continues. See
[`ingestion/pipeline.py`](../aegis/ingestion/pipeline.py).

## Data flow: investigation

```
POST /incidents/investigate
   │
   ├─► create a dedicated session (its own checkpoint thread)
   │
   ├─► analyse supplied logs (before the graph runs — cheap, deterministic)
   │
   └─► graph.ainvoke
         │
         ├─ triage      classify; generate 2-4 query variants
         ├─ gather      retrieve ∥ prior incidents ∥ memory   (concurrent)
         ├─ investigate reason over evidence; maybe call tools
         │    └─ tools  execute concurrently, feed results back
         └─ synthesize  force into InvestigationReportSchema
         │
   ┌─────┴─────────────────────────────────────────────┐
   │ persist InvestigationReport                        │
   │ advance incident status OPEN → INVESTIGATING       │
   │ return report + rendered markdown                  │
   └────────────────────────────────────────────────────┘
```

## The two connection pools

This surprises people, so it is worth stating plainly. Aegis holds **two**
independent PostgreSQL pools:

| Pool | Driver | Used by | Why separate |
|---|---|---|---|
| SQLAlchemy async | `asyncpg` | ORM: users, incidents, documents, retrieval | General-purpose |
| psycopg async | `psycopg` | LangGraph `AsyncPostgresSaver` | The checkpointer requires psycopg specifically |

They cannot be merged. The checkpointer needs `autocommit=True` and
`prepare_threshold=None` — settings that would be wrong for ORM traffic. Both
count toward the database's `max_connections`, which matters when sizing
`POSTGRES_POOL_SIZE` against replica count.

## Failure philosophy

Aegis degrades rather than fails, in a specific and deliberate way:

| Failure | Behaviour | Rationale |
|---|---|---|
| One retrieval arm errors | Continue with the other | Partial results beat an error page |
| Reranking fails | Fall back to fusion order | Costs precision, not correctness |
| A tool raises | Return the error as tool output | The model can read it and adapt |
| Triage fails | Search the raw query | Degraded retrieval beats no investigation |
| Synthesis fails | Emit a minimal valid report | Evidence is still worth having |
| Database unreachable | 503 with `Retry-After` | Retryable, and honestly labelled |
| Model provider down | Walk the fallback chain, then 502 | Failover is per-call, not global |

The exception is **startup**. Staging and production refuse to boot on unsafe
configuration — a weak JWT secret, wildcard CORS with credentials, `DEBUG=true`.
Those are not transient conditions to degrade through; they are mistakes that
should be loud immediately rather than exploitable quietly.

Note the asymmetry with dependency health: the app *does* start when the
database is down. A replica that refuses to start cannot report its own health,
cannot serve `/metrics`, and converts a brief outage into a crash loop. It
starts, reports itself unready, and recovers.

## Concurrency correctness

Three shared-state bugs were specifically designed out, and each has a test
pinning it.

**Model selection is per-call.** The LLM service holds only an immutable cache
of configured models. A fallback triggered by one request cannot change which
model another request uses. (The prior design rotated a shared attribute — see
[`services/llm.py`](../aegis/services/llm.py) for the full description.)

**Tool citations use a `ContextVar`, not a module global.** Two concurrent
investigations would otherwise interleave citations into one list and each
report would cite the other's sources. ContextVars are scoped per asyncio task,
which matches the lifetime of an investigation exactly.

**Rank fusion returns copies.** `reciprocal_rank_fusion` does not mutate the
result objects it is given, so fusing the same candidate list twice cannot
operate on already-fused scores.

## Request lifecycle

Middleware is registered in reverse execution order, so at request time:

```
RequestContextMiddleware   assign/propagate X-Request-ID, bind log context
   AccessLogMiddleware     one structured event per request
      MetricsMiddleware    counters and histograms, labelled by route template
         CORSMiddleware
            SecurityHeaders
               GZip
                  → router → dependencies → endpoint
```

`RequestContextMiddleware` must be first so every subsequent layer's logs carry
the request id. Because `contextvars` propagate into asyncio tasks, a background
task spawned inside a request inherits its correlation id for free.

Metrics label on the **route template** (`/incidents/{incident_id}`), never the
raw path. Labelling on raw paths would create one time series per UUID and
eventually take the scrape endpoint down — and would let an attacker probing
random URLs do it deliberately.

## Related reading

- [rag-pipeline.md](rag-pipeline.md) — retrieval internals
- [agent.md](agent.md) — the graph and prompts in depth
- [adr/](adr/) — the record of why the significant choices went this way
