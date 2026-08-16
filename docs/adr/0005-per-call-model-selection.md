# 0005 — Per-call model selection instead of shared mutable state

**Status:** Accepted

## Context

The original `LLMService` was a module-level singleton holding the active model
in a mutable instance attribute:

```python
class LLMService:
    def __init__(self):
        self._llm = LLMRegistry.get(settings.DEFAULT_LLM_MODEL)

    def _switch_to_next_model(self):
        self._llm = next_model_entry["llm"]     # mutates shared state

    def bind_tools(self, tools):
        self._llm = self._llm.bind_tools(tools) # also mutates shared state
```

Under a single request this works. Under concurrency it is a data race with two
distinct symptoms:

**Cross-request model switching.** Request A fails on `gpt-4o` and rotates
`self._llm`. Request B, midway through its own multi-turn investigation, silently
switches models — so B's second turn runs on a different model from its first,
with different tool-calling behaviour and a different token budget. B never
errored; it just quietly changed.

**Tool binding races.** `bind_tools` mutated the same attribute, so whichever
caller bound last determined the tools every other caller saw.

Both are invisible in single-request testing and reliably wrong under load —
which is exactly when an incident response tool is being used.

## Decision

Make model selection **per call**. The service holds only an immutable cache of
configured model instances, keyed by `(model_name, tool_signature)`. Every method
takes the model as an argument. The fallback chain is walked over a **local
copy**.

```python
async def invoke(self, messages, *, model=None, tools=None, **overrides):
    chain = list(settings.model_fallback_chain)   # local, per call
    for position, candidate in enumerate(chain):
        instance = self.get_model(candidate, tools, **overrides)
        try:
            return await self._invoke_once(instance, messages)
        except Exception:
            continue   # nothing shared is written
```

The service object holds no per-request data at all, so sharing it across the
process is safe.

## Alternatives considered

**A lock around the shared attribute.** Correct, and it serialises every LLM
call in the process — turning a concurrent workload into a queue behind the
slowest request. Unacceptable for a service whose calls take seconds.

**One service instance per request.** Correct, and it discards the model cache
and the underlying HTTP connection pool on every request, adding setup latency
to every call.

**Thread-local / context-local state.** Would work, and it is more machinery
than the problem needs. Passing the model as an argument is simpler and makes
the data flow visible at the call site.

## Consequences

**Good**

- No shared mutable state, so no race. A fallback in one request cannot affect
  another.
- Callers can request a specific model per call — used by the reranker, which
  runs on `RERANK_MODEL` independently of the reasoning model.
- The cache still avoids rebuilding clients, so connection pools are reused.
- Testable: a fake service needs no lifecycle management.

**Costs accepted**

- The fallback chain is recomputed per call. Negligible next to a network round
  trip.
- A model that is failing repeatedly is retried by each new request rather than
  being globally marked down. A circuit breaker would fix that; it is not built.
  `aegis_llm_fallback_total` makes the condition observable in the meantime.

## The same bug class, elsewhere

Two other instances were found and fixed by the same reasoning:

**Tool citations** used a module-level list. Two concurrent investigations would
interleave citations and each report would cite the other's sources. Now a
`ContextVar`, which is scoped per asyncio task — matching the lifetime of an
investigation exactly. Note that ContextVar *defaults* are shared, so
`_record_citations` always rebinds a new list rather than mutating in place.

**Rank fusion** mutated the `SearchResult` objects it was given, so fusing the
same candidate list twice operated on already-fused scores. Now returns copies
via `dataclasses.replace`. This one was found by a test, which is the argument
for writing them.

The general rule this codebase follows: **module-level mutable state is a bug
until proven otherwise** in an async service. Caches of immutable values are
fine. Anything that gets written during a request is not.
