# Aegis documentation

Start here. Each document is self-contained; read them in whatever order fits
what you are trying to do.

## By goal

**"I want to understand how this works."**
1. [architecture.md](architecture.md) — the system as a whole, and why each boundary sits where it does
2. [rag-pipeline.md](rag-pipeline.md) — how documents become searchable, and how search actually works
3. [agent.md](agent.md) — the workflow graph, the prompts, and the report contract
4. [log-analysis.md](log-analysis.md) — turning 10,000 log lines into something a model can reason about

**"I want to run it."**
1. [configuration.md](configuration.md) — every setting and why its default is what it is
2. [deployment.md](deployment.md) — Docker, Kubernetes, migrations, scaling
3. [operations.md](operations.md) — metrics, alerts, and what to do when something is wrong

**"I want to build on it."**
1. [development.md](development.md) — setup, testing strategy, adding tools and document types
2. [api.md](api.md) — full endpoint reference
3. [adr/](adr/) — why the significant decisions went the way they did

**"I want to know what I'm trusting."**
1. [security.md](security.md) — threat model, auth design, and honest limitations

## Code map

| Package | Responsibility | Entry point |
|---|---|---|
| `aegis.api` | HTTP surface, auth dependencies | [`api/v1/__init__.py`](../aegis/api/v1/__init__.py) |
| `aegis.core.config` | Validated settings | [`core/config.py`](../aegis/core/config.py) |
| `aegis.core.langgraph` | Agent workflow and tools | [`core/langgraph/graph.py`](../aegis/core/langgraph/graph.py) |
| `aegis.core.prompts` | Prompt templates (markdown) | [`core/prompts/`](../aegis/core/prompts/) |
| `aegis.ingestion` | Load → chunk → embed → index | [`ingestion/pipeline.py`](../aegis/ingestion/pipeline.py) |
| `aegis.retrieval` | Embeddings, vector store, hybrid search | [`retrieval/hybrid.py`](../aegis/retrieval/hybrid.py) |
| `aegis.analysis` | Log parsing and clustering | [`analysis/clustering.py`](../aegis/analysis/clustering.py) |
| `aegis.models` | Database tables | [`models/`](../aegis/models/) |
| `aegis.schemas` | API contracts, structured LLM output | [`schemas/incident.py`](../aegis/schemas/incident.py) |
| `aegis.services` | Async database and LLM access | [`services/`](../aegis/services/) |

## A note on the code comments

The source carries substantially more commentary than typical. The convention
throughout is that comments explain **why**, not what — a comment that
restates the code earns nothing and rots. Where you see a comment, it is
usually recording one of:

- a non-obvious trade-off (why RRF instead of score normalisation),
- a bug the current shape prevents (why model selection is per-call),
- a constraint that is invisible from the code (why the checkpointer needs its
  own connection pool).

If you are trying to understand a decision, the comment next to it is usually
more specific than the prose here.
