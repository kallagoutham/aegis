# 0003 — A structured workflow graph instead of a generic agent loop

**Status:** Accepted

## Context

The default agent architecture is a two-node loop: call the model, run whatever
tools it asked for, repeat until it stops asking. It is flexible, it is what
most frameworks demo, and it was what this codebase started with.

It has one property that is disqualifying for incident response: **the model
decides whether to gather evidence.**

In practice, when a question looks answerable from general knowledge, models
answer from general knowledge. Ask "why would checkout return 503?" and a
capable model will produce a fluent, plausible, entirely ungrounded list of
generic causes — without ever calling `search_runbooks`. The answer looks
authoritative and says nothing about *your* systems.

During an incident that is worse than silence, because the responder spends
time chasing it.

## Decision

Encode the investigation structure in the graph:

```
triage → gather → investigate ⇄ tools → synthesize
```

Retrieval happens in `gather`, **unconditionally**, before the model reasons at
all. The model can still call tools to dig further — but it cannot start from
nothing.

## Alternatives considered

**Generic ReAct loop with a strong system prompt** telling the model to always
search first. Tried in principle; prompt instructions are probabilistic. "Always
search before answering" is followed most of the time, and the failures are
silent and look exactly like successes.

**Forced first tool call** — require `search_runbooks` on turn one. Better, but
it wastes a full model round trip on a decision that is already made, and the
model still chooses the query, which is the part it is worst at (see `triage`
below).

**A fully deterministic pipeline** with no agent loop at all. More predictable,
and unable to follow up — "the logs mention a service the query didn't name,
search for that too" is exactly the behaviour worth having.

## Why each node exists

**`triage`** — the highest-value node, and the least obvious. It generates 2–4
*search query variants*. The way a user describes a problem rarely matches the
vocabulary of the runbook that answers them:

| User says | Runbook says |
|---|---|
| "checkout is broken" | "payment gateway upstream timeout" |
| "everything is slow" | "connection pool saturation" |

Searching only the raw query systematically under-retrieves, and the failure is
invisible — you get *some* results, just not the right ones.

**`gather`** — retrieval, prior incidents, and memory concurrently. Independent
operations; during an incident the difference between 1.2s and 3.5s of setup is
worth the complexity.

**`investigate`** — the reasoning loop, now starting from evidence.

**`synthesize`** — separated from investigation because the two want opposite
things. Investigation wants freedom to explore; synthesis wants rigid validated
output. A single prompt doing both produces reports that are either
under-structured or under-reasoned.

## Consequences

**Good**

- The model *always* sees retrieved context. Ungrounded answers become an
  observable failure (empty retrieval) rather than a silent one.
- Query variants materially improve recall.
- Per-node metrics (`aegis_agent_node_duration_seconds`) show exactly where
  latency goes.
- Nodes are independently testable.

**Costs accepted**

- One extra LLM call per investigation for triage.
- Retrieval runs even when it is unnecessary — a pure follow-up question pays
  for a search it may not need. Cheap relative to being wrong.
- More code than a two-node loop, and a graph that must be understood before it
  can be modified.

**Chat mode** short-circuits after `investigate`, since a conversational
follow-up does not need a formal report.

## Bounding

`_route_after_investigate` checks the iteration cap **before** honouring a tool
request. Without that ordering, a model that keeps calling tools loops until the
HTTP request times out — producing no answer at all after burning the entire
budget. Instead it is forced into synthesis and recorded as `truncated`, which
is both an answer and an observable signal.

## Degradation

Every node returns rather than raises. Triage failure falls back to the raw
query; investigate failure still reaches synthesis; synthesis failure emits a
minimal valid report. A partially failed investigation still hands the responder
the evidence that *was* gathered — which is usually the expensive part.
