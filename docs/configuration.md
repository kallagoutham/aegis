# Configuration

Source: [`aegis/core/config.py`](../aegis/core/config.py) — the authoritative
reference. This document explains the reasoning behind the defaults.

## How settings resolve

Highest precedence first:

1. **Real environment variables** — anything exported by the shell or injected
   by the container runtime or secret manager.
2. **The first env file that exists**:
   ```
   .env.<APP_ENV>.local
   .env.<APP_ENV>
   .env.local
   .env
   ```
3. **Declared defaults.**

`APP_ENV` selects the environment (`development`, `staging`, `production`,
`test`) and accepts aliases: `prod`, `stage`, `local`, `testing`.

### Environment overlays

Each environment tightens defaults. Overlays apply **only to fields the operator
did not set explicitly**, so an explicit `LOG_LEVEL=DEBUG` in production is
still honoured.

| | development | test | staging | production |
|---|---|---|---|---|
| `DEBUG` | true | true | false | false |
| `LOG_LEVEL` | DEBUG | DEBUG | INFO | INFO |
| `LOG_FORMAT` | console | console | json | json |
| Rate limits | relaxed | effectively off | moderate | strict |
| `BCRYPT_ROUNDS` | 12 | **4** | 12 | 12 |

Tests drop bcrypt cost automatically so fixtures do not pay 250ms per user.

### Lists accept CSV

`ALLOWED_ORIGINS=https://a.com,https://b.com` works, as does JSON. pydantic-
settings normally only accepts JSON for list fields, which makes env files
unnecessarily hostile; Aegis installs lenient sources to allow both.

---

## Production hardening

Staging and production **refuse to start** if any of these hold:

| Condition | Why it is fatal |
|---|---|
| `JWT_SECRET_KEY` shorter than 32 chars | Brute-forceable HMAC key |
| `JWT_SECRET_KEY` is a known placeholder | Public value; anyone can mint tokens |
| `OPENAI_API_KEY` empty | Every model and embedding call would fail |
| `ALLOWED_ORIGINS` contains `*` | Wildcard plus credentials is a CORS bypass |
| `DEBUG=true` | Leaks internals in error responses |
| `POSTGRES_ECHO=true` | Copies query contents into logs |
| `POSTGRES_PASSWORD` is a placeholder | Trivially guessable |

The failure names every problem at once:

```
Refusing to start in production with unsafe configuration:
  - JWT_SECRET_KEY must be at least 32 characters in staging/production.
  - ALLOWED_ORIGINS must not be '*' when credentials are allowed; list real origins.
```

Development has none of these checks, so local work stays frictionless with no
secrets configured.

### Consistency checks (all environments)

- `CHUNK_OVERLAP_TOKENS` must be smaller than `CHUNK_TARGET_TOKENS`, or chunking
  cannot advance.
- `RETRIEVAL_TOP_K` cannot exceed `RETRIEVAL_CANDIDATE_K`.
- `RERANK_TOP_N` must be at least `RETRIEVAL_TOP_K`, or reranking would discard
  results the agent is meant to see.

---

## Reference

### Application

| Setting | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | Selects environment and env file |
| `PROJECT_NAME` | `Aegis` | Appears in OpenAPI, logs, and the agent's identity |
| `VERSION` | `1.0.0` | Reported by `/health` and metrics |
| `API_V1_STR` | `/api/v1` | Normalised to a leading, non-trailing slash |
| `DEBUG` | `false` | Verbose errors. Fatal in staging/production |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | CSV. `*` rejected when deployed |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Also sets Postgres `statement_timeout` |

### Language model

| Setting | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | — | Required outside development |
| `OPENAI_BASE_URL` | OpenAI | Point at LiteLLM, vLLM, or an Azure proxy |
| `DEFAULT_LLM_MODEL` | `gpt-4o-mini` | Primary reasoning model |
| `FALLBACK_LLM_MODELS` | `gpt-4o,gpt-4o-mini` | Tried in order on failure |
| `DEFAULT_LLM_TEMPERATURE` | `0.1` | Low deliberately — see below |
| `LLM_MAX_OUTPUT_TOKENS` | `4000` | Reports are large |
| `LLM_CONTEXT_TOKEN_BUDGET` | `32000` | History trimming budget |
| `MAX_LLM_CALL_RETRIES` | `3` | Per model, before failing over |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `90` | Per call |

**Why temperature 0.1.** Incident response wants reproducibility. Two engineers
pasting the same logs should get the same hypothesis; a creative variation is a
liability, not a feature.

**The fallback chain is walked per call**, over a local copy. A failover
triggered by one request cannot change which model another request uses. Watch
`aegis_llm_fallback_total` — sustained fallback means the primary is effectively
down and answer quality has silently changed.

**Model choice.** `gpt-4o-mini` is the default because most of the reasoning
load is grounded synthesis rather than open-ended problem solving, and retrieval
quality dominates model capability for this task. Move to `gpt-4o` if reports
are shallow *after* you have confirmed retrieval is returning the right
passages — upgrading the model to compensate for bad retrieval is expensive and
does not work.

### Embeddings and retrieval

| Setting | Default | Notes |
|---|---|---|
| `EMBEDDING_MODEL` | `text-embedding-3-small` | |
| `EMBEDDING_DIMENSIONS` | `1536` | **Must match the pgvector column** |
| `EMBEDDING_BATCH_SIZE` | `64` | Inputs per API call |
| `RETRIEVAL_TOP_K` | `8` | Chunks handed to the agent |
| `RETRIEVAL_CANDIDATE_K` | `40` | Pulled from each arm before fusion |
| `RETRIEVAL_MIN_SCORE` | `0.0` | Post-fusion threshold; 0 disables |
| `HYBRID_VECTOR_WEIGHT` | `0.7` | Lexical gets `1 - this` |
| `RRF_K` | `60` | Fusion smoothing constant |
| `RERANK_ENABLED` | `true` | One extra LLM call per search |
| `RERANK_MODEL` | `gpt-4o-mini` | |
| `RERANK_TOP_N` | `20` | Candidates handed to the reranker |

> **Changing `EMBEDDING_MODEL` or `EMBEDDING_DIMENSIONS` invalidates every
> stored vector.** Vectors from different models live in incomparable spaces, so
> mixing them produces silently meaningless similarity scores. You need a
> migration to alter the column width and a full `--force` re-ingest.

**`HYBRID_VECTOR_WEIGHT`** is the main quality dial. Toward `0.5` favours exact
tokens (good for corpora dense with error codes); toward `0.85` favours meaning
(good for prose-heavy documentation).

**`RETRIEVAL_CANDIDATE_K`** trades recall for latency. Raising it improves the
chance the right chunk is *considered*; it does not change how many are
*returned*.

### Ingestion

| Setting | Default | Notes |
|---|---|---|
| `CHUNK_TARGET_TOKENS` | `512` | Preferred chunk size |
| `CHUNK_OVERLAP_TOKENS` | `64` | Repeated between adjacent chunks |
| `CHUNK_MIN_TOKENS` | `32` | Below this, merged into a neighbour |
| `INGESTION_CONCURRENCY` | `4` | Parallel documents |
| `MAX_UPLOAD_BYTES` | `25 MB` | Per file |

**Chunk size trade-off.** Smaller chunks are more precise — retrieval returns
exactly the relevant procedure — but lose surrounding context. Larger chunks
carry context and dilute the embedding, so a chunk covering three topics matches
all three weakly. 512 tokens is roughly one runbook subsection, which is the
natural unit of an operational procedure.

Any change here requires `--force` re-ingest; existing chunks were built under
the old settings.

### Agent

| Setting | Default | Notes |
|---|---|---|
| `AGENT_MAX_TOOL_ITERATIONS` | `6` | Hard cap before synthesis is forced |
| `AGENT_MAX_LOG_LINES` | `5000` | Parsing cap; truncation is flagged |
| `LONG_TERM_MEMORY_ENABLED` | `true` | mem0 cross-session user facts |

The iteration cap bounds worst-case latency and cost. Six is enough for
search → read → refine → confirm. If
`aegis_agent_investigations_total{outcome="truncated"}` runs above ~25%, the
usual cause is poor retrieval rather than a low cap — the agent keeps searching
for something it never finds.

### Authentication

| Setting | Default | Notes |
|---|---|---|
| `JWT_SECRET_KEY` | — | Generate with `make secret` |
| `JWT_ALGORITHM` | `HS256` | |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` (24h) | User tokens |
| `JWT_SESSION_TOKEN_EXPIRE_MINUTES` | `10080` (7d) | Session tokens |
| `JWT_ISSUER` / `JWT_AUDIENCE` | `aegis` / `aegis-api` | Verified on decode |
| `PASSWORD_MIN_LENGTH` | `12` | NIST SP 800-63B |
| `BCRYPT_ROUNDS` | `12` | ~250ms per hash |

Session tokens live longer because an investigation may span a shift. They are
also narrower — one thread, nothing else.

### Database

| Setting | Default | Notes |
|---|---|---|
| `POSTGRES_HOST` / `PORT` / `DB` / `USER` / `PASSWORD` | `localhost` / `5432` / `aegis` / `aegis` / — | |
| `POSTGRES_POOL_SIZE` | `20` | Per replica, ORM pool |
| `POSTGRES_MAX_OVERFLOW` | `10` | Burst above pool size |
| `POSTGRES_POOL_TIMEOUT` | `30` | Wait for a connection |
| `POSTGRES_POOL_RECYCLE` | `1800` | Recycle before proxy idle timeouts |
| `POSTGRES_ECHO` | `false` | Fatal in staging/production |

> **Sizing.** Total demand is
> `replicas × (POSTGRES_POOL_SIZE + POSTGRES_MAX_OVERFLOW)` for the ORM pool,
> **plus** a second psycopg pool for LangGraph checkpointing. Both count toward
> the database's `max_connections`. At the defaults, 8 replicas is roughly
> 8 × 30 + 8 × 20 = 400 connections.

### Observability

| Setting | Default | Notes |
|---|---|---|
| `LOG_LEVEL` | per environment | |
| `LOG_FORMAT` | `console` dev / `json` deployed | |
| `LOG_DIR` | `logs` | Resolved against the project root |
| `LOG_TO_FILE` | `true` | Set false when the platform captures stdout |
| `METRICS_ENABLED` | `true` | Mounts `/metrics` |
| `LANGFUSE_*` | — | Auto-enables when both keys are present |

File logs rotate at 50 MB, 5 generations. Unbounded log files are a common way
for a container to fill its disk.

### Rate limiting

See [api.md](api.md#rate-limits). Limits are per-replica.

---

## Recipes

### Minimal local development

```bash
APP_ENV=development
OPENAI_API_KEY=sk-...
JWT_SECRET_KEY=anything-works-locally
POSTGRES_HOST=localhost
```

### Production

```bash
APP_ENV=production
OPENAI_API_KEY=${FROM_SECRET_MANAGER}
JWT_SECRET_KEY=${FROM_SECRET_MANAGER}      # openssl rand -hex 32
POSTGRES_PASSWORD=${FROM_SECRET_MANAGER}
ALLOWED_ORIGINS=https://ops.example.com
LOG_TO_FILE=false                          # platform captures stdout
POSTGRES_POOL_SIZE=10                      # lower per-replica, more replicas
```

### Latency-optimised

```bash
RERANK_ENABLED=false          # removes one LLM call per search
RETRIEVAL_CANDIDATE_K=20      # less work per arm
AGENT_MAX_TOOL_ITERATIONS=3   # shorter worst case
```

### Quality-optimised

```bash
DEFAULT_LLM_MODEL=gpt-4o
RERANK_MODEL=gpt-4o
RETRIEVAL_CANDIDATE_K=60
RETRIEVAL_TOP_K=12
AGENT_MAX_TOOL_ITERATIONS=10
```

### Self-hosted / air-gapped model

```bash
OPENAI_BASE_URL=http://litellm.internal:4000/v1
DEFAULT_LLM_MODEL=llama-3.3-70b
EMBEDDING_MODEL=bge-large-en-v1.5
EMBEDDING_DIMENSIONS=1024     # must match the model AND the column
```

Changing `EMBEDDING_DIMENSIONS` needs a migration altering the `vector(n)`
column and a full re-ingest.

---

## Verifying

```bash
make check
```

```
Environment   : production
Database      : db.internal:5432/aegis
LLM model     : gpt-4o-mini
Embedding     : text-embedding-3-small (1536d)

[ok]   database reachable
[ok]   pgvector extension installed
[ok]   OPENAI_API_KEY configured
[ok]   JWT_SECRET_KEY configured

All checks passed.
```

Exits non-zero on failure, so it works as a deployment gate.
