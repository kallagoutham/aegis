# Development

## Setup

```bash
make install          # .venv + all dependencies
make env              # .env.development from the template
make test             # 222 tests, no database or API key needed
```

Unit tests need nothing configured. Integration tests need PostgreSQL with
pgvector — the easiest route is `make up-min` to start just the database, or
Docker directly:

```bash
docker run -d --name aegis-db -p 5432:5432 \
  -e POSTGRES_DB=aegis -e POSTGRES_USER=aegis -e POSTGRES_PASSWORD=aegis \
  pgvector/pgvector:pg16
make migrate
```

---

## Layout

```
aegis/
├── api/v1/       endpoints — validate, delegate, serialise. No logic.
├── core/         config, logging, metrics, exceptions, prompts, agent graph
├── ingestion/    load → chunk → index
├── retrieval/    embeddings → vector store → hybrid fusion
├── analysis/     log parsing → clustering → anomaly detection (no deps)
├── models/       SQLModel tables
├── schemas/      Pydantic contracts (API + structured LLM output)
└── services/     async database and LLM access — all I/O lives here
```

Two rules keep the boundaries honest:

**Endpoints raise domain exceptions, never `HTTPException`.** That is what makes
business logic testable without HTTP and what lets one set of handlers guarantee
a consistent error contract.

**`analysis` imports nothing from the rest of the codebase.** It is pure
computation, which is why its tests run in milliseconds and why
`aegis analyze file.log` works with nothing configured. Keep it that way.

---

## Testing

```bash
make test              # everything
make test-unit         # no database required
make test-integration  # requires PostgreSQL
make test-cov          # coverage report
```

### What is tested and why

The suite deliberately concentrates on **pure logic with subtle correctness
properties**, because that is where bugs hide and where tests are cheap:

| Area | Tests | Pins |
|---|---|---|
| Chunking | 24 | Heading structure survives; overlap never crosses sections; code fences are not headings |
| Log analysis | 42 | Format detection; stack traces stay whole; templates collapse correctly; compression ratio |
| Retrieval fusion | 21 | Cross-arm agreement wins; fusion does not mutate inputs; scores normalise |
| Report schema | 36 | Dangling evidence dropped; steps renumbered; confidence derived |
| Auth | 33 | Token confusion rejected in both directions; issuer/audience/expiry enforced |
| Config and utils | 41 | Production hardening; JSON recovery from imperfect model output; secret redaction |
| API | 25 | Auth required; problem+json shape; security headers; request correlation |

Three of these tests were written after the code and immediately found real
bugs:

- `test_larger_k_flattens_rank_influence` found that `reciprocal_rank_fusion`
  mutated its input, so two calls over the same list corrupted each other.
- `test_empty_input_is_zero_tokens` found that `estimate_tokens` disagreed with
  itself depending on whether `tiktoken` was installed.
- `test_masks_bearer_tokens` found that `redact_secrets` masked the word
  "Bearer" and left the token beside it in plain sight.

That is the argument for writing them.

### Warnings are errors

`filterwarnings = ["error"]` in `pyproject.toml`. A `DeprecationWarning` in our
own code is a bug we want to see now, not after an upgrade breaks. Third-party
warnings we cannot act on are ignored **individually**, never by relaxing the
policy.

This has already paid off: it caught `status.HTTP_422_UNPROCESSABLE_ENTITY`
being deprecated in Starlette, which was emitting a warning *from inside the
exception handler* and turning every 422 into a 500.

### Fixtures

[`tests/conftest.py`](../tests/conftest.py) sets `APP_ENV=test` **before any
aegis import**, because `aegis.core.config` resolves settings at import time —
setting it afterwards would silently have no effect.

`FakeEmbeddingService` produces deterministic pseudo-vectors from a hash, so
identical text always embeds identically and similarity comparisons are
reproducible without a network call.

---

## Code style

```bash
make format     # apply fixes
make lint       # check
make typecheck  # mypy
make verify     # everything CI runs
```

Ruff, 119 columns, Google-style docstrings. Enabled rule sets include `ASYNC`
(async correctness) and `S` (bandit security checks).

### Comments explain *why*

The convention throughout is that a comment records something the code cannot:
a trade-off, a bug the current shape prevents, or a constraint invisible from
the code. A comment restating the code earns nothing and rots.

```python
# Bad — restates the code
# Increment the counter
counter += 1

# Good — records a constraint
# Batches are sent sequentially. Firing them concurrently would be faster but
# reliably trips provider rate limits on large ingests, and the resulting 429
# backoff is slower than serial.
```

---

## Common tasks

### Adding a tool

1. Write it in `aegis/core/langgraph/tools/`:

```python
@tool
async def query_metrics(
    query: Annotated[str, "PromQL query."],
    minutes: Annotated[int, "Lookback window."] = 60,
) -> str:
    """Query Prometheus for a metric over a time window.

    Use this to confirm or rule out a hypothesis quantitatively — pool
    saturation, error rate, request volume. Prefer this over inferring from
    logs when a metric exists.
    """
    try:
        ...
        return formatted_results
    except Exception as exc:
        logger.error("query_metrics_tool_failed", error=str(exc), exc_info=True)
        return f"Metrics query failed: {exc}. Continue with the evidence you have."
```

2. Register it in `tools/__init__.py`, positioned by how much you want it
   reached for — models are biased toward tools listed earlier.

The three rules: **never raise**, **return text**, **say explicitly when nothing
was found**. The docstring is the interface — the model chooses tools by reading
it, so put the usage policy there.

### Adding a document type

1. Add a value to `SourceType` in `models/knowledge.py`.
2. Map extensions or directory names in `loaders.py` (`_EXTENSION_TYPES`,
   `_DIRECTORY_HINTS`).
3. If it needs different chunking, add a chunker and wire it into
   `get_chunker()`.
4. Generate a migration — the enum is stored as a string column.

### Adding a log format

Add a `_parse_*_line` function in `analysis/parser.py` and put it in the
`parse_line` chain **ahead of** the plain-text fallback. Order by specificity: a
parser that matches too eagerly will steal lines from the ones after it.

### Adding an endpoint

```python
@router.post("/thing", response_model=ThingResponse, summary="Create a thing")
@limiter.limit(settings.rate_limit_for("thing"))
async def create_thing(
    request: Request,          # required by the rate limiter
    payload: ThingCreate,
    user: ResponderUser,       # or CurrentUser / AdminUser
    session: DbSession,
) -> ThingResponse:
    """One-line summary for the OpenAPI docs."""
    ...
    await session.commit()     # commit explicitly, never implicitly
    return ThingResponse.model_validate(thing)
```

Raise domain exceptions from `core/exceptions.py`. Commit explicitly so the
transaction boundary is visible at the call site rather than being an invisible
side effect of returning.

### Changing a prompt

Edit the markdown in `aegis/core/prompts/`. Placeholders are `{{name}}` — never
`{name}`, which collides with the JSON examples these prompts contain.

Prompt bodies are cached; `clear_prompt_cache()` picks up edits without a
restart.

### Adding a metric

Define it in `core/metrics.py` with **bounded label cardinality**. Never label
with a user id, session id, or raw path — one series per value will eventually
take the scrape endpoint down.

### Database changes

```bash
make migration M="describe the change"
# READ the generated file before applying
make migrate
```

Alembic cannot see index operator classes, generated columns, or extensions —
the initial migration writes those by hand for that reason. If your change
involves any of them, write it manually.

New tables must be imported in `models/__init__.py` or autogenerate will not see
them.

---

## Debugging

```bash
LOG_LEVEL=DEBUG make dev            # verbose structured logs
POSTGRES_ECHO=true make dev         # every SQL statement (never in production)
```

Trace one request end to end:

```bash
curl -H "X-Request-ID: my-trace" localhost:8000/api/v1/health
grep my-trace logs/development.jsonl | jq
```

Inspect retrieval without the agent:

```bash
aegis search "your query" --json | jq '.hits[] | {title, heading_path, score, strategy}'
```

Inspect log analysis without anything else:

```bash
aegis analyze /path/to.log --json | jq '.top_error_templates'
```

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to get full LLM traces —
every prompt, response, and tool call in a timeline.

---

## Pull requests

```bash
make verify
```

Must pass: ruff check, ruff format check, and the full test suite. CI also runs
a **migration reversibility** job (`upgrade → downgrade base → upgrade`) and a
schema-drift check that fails if models changed without a matching migration.

When adding a feature, update the relevant document in `docs/`. The README's
"Project status" section lists what is deliberately not built — if you build one
of those things, move it out of that list.
