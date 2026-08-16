# API reference

Base URL: `http://localhost:8000/api/v1`
Interactive docs: `/docs` (development and test only)
OpenAPI schema: `/api/v1/openapi.json`

## Contents

- [Authentication](#authentication)
- [Errors](#errors)
- [Auth endpoints](#auth-endpoints)
- [Incidents](#incidents)
- [Knowledge base](#knowledge-base)
- [Chat](#chat)
- [Health](#health)
- [Rate limits](#rate-limits)

---

## Authentication

Two token types exist, and they are **not interchangeable**. Each carries a
`typ` claim that is verified on decode, so a token issued for one purpose is
rejected by endpoints expecting the other.

| Type | Obtained from | Authorises | Default lifetime |
|---|---|---|---|
| `user` | `POST /auth/register`, `POST /auth/login` | Account-level: sessions, incidents, search, ingestion | 24h |
| `session` | `POST /auth/sessions` | One conversation thread only | 7d |

The split is a real boundary: a session token cannot enumerate the owner's other
sessions, and a user token cannot be replayed against a chat endpoint. See
[security.md](security.md).

```
Authorization: Bearer <token>
```

### Roles

| Role | Can |
|---|---|
| `viewer` | Read incidents, search the knowledge base |
| `responder` | The default. Also: create incidents, run investigations |
| `admin` | Also: ingest and delete documents |

Registration creates a `responder`. Bootstrap an admin from the CLI:

```bash
aegis create-user you@example.com --admin
```

---

## Errors

Every error is an RFC 9457 problem document with content type
`application/problem+json`:

```json
{
  "type": "https://docs.aegis.dev/errors/validation_error",
  "title": "ValidationError",
  "status": 422,
  "code": "validation_error",
  "detail": "The request payload failed validation.",
  "instance": "/api/v1/auth/register",
  "request_id": "8f2c1e...",
  "errors": [
    { "field": "password", "message": "Password must be at least 12 characters long.", "type": "value_error" }
  ]
}
```

Branch on `code`, never on `detail` — codes are stable, wording is not.

| Code | Status | Meaning |
|---|---|---|
| `validation_error` | 422 | Payload failed validation; see `errors` |
| `authentication_failed` | 401 | Missing, invalid, expired, or wrong-type token |
| `permission_denied` | 403 | Authenticated but not allowed |
| `not_found` | 404 | No such resource |
| `conflict` | 409 | Duplicate; e.g. email already registered |
| `payload_too_large` | 413 | Above `MAX_UPLOAD_BYTES` |
| `unsupported_content` | 415 | No loader for that content type |
| `rate_limit_exceeded` | 429 | Includes `Retry-After` |
| `llm_unavailable` | 502 | Every model in the chain failed |
| `database_unavailable` | 503 | Includes `Retry-After` |
| `retrieval_failed` | 503 | Search could not be serviced |
| `internal_error` | 500 | Unexpected; quote `request_id` |

`request_id` is echoed in the body and in the `X-Request-ID` header. Pass your
own inbound `X-Request-ID` to correlate across services — Aegis preserves it.

---

## Auth endpoints

### `POST /auth/register`

```json
{ "email": "responder@example.com", "password": "a-good-passphrase-here", "full_name": "Alex" }
```

→ `201` with the user and an initial user token.

Passwords follow NIST SP 800-63B: length is what matters (12 minimum, 72 bytes
maximum), plus screening against common passwords, repeated characters,
keyboard sequences, and low character variety. No composition rules — they
mostly produce `Password1!`.

### `POST /auth/login`

```json
{ "email": "responder@example.com", "password": "a-good-passphrase-here" }
```

→ `200` `{ "access_token": "...", "token_type": "bearer", "expires_at": "..." }`

Returns an identical error whether the account is absent or the password wrong,
and performs a dummy hash in the missing-user case so response timing does not
reveal which emails are registered.

### `GET /auth/me`

→ `200` with the current user profile.

### `POST /auth/password`

```json
{ "current_password": "...", "new_password": "..." }
```

→ `204`. **Existing tokens remain valid** — there is no denylist. See
[security.md](security.md#known-limitations).

### `POST /auth/sessions`

```json
{ "title": "payments 503 investigation", "metadata": { "slack_thread": "C123/p456" } }
```

→ `201` `{ "session_id": "...", "title": "...", "token": { ... } }`

The returned token is scoped to this session only, so it can be handed to a
narrower component without granting account access.

### `GET /auth/sessions` · `GET /auth/sessions/{id}` · `DELETE /auth/sessions/{id}`

List, read, and delete. Deletion clears the LangGraph checkpoints first, so a
failure leaves the session visible and retryable rather than orphaning
checkpoint rows.

---

## Incidents

### `POST /incidents/investigate` — the main endpoint

Requires `responder` or `admin`.

```json
{
  "query": "Checkout is returning 503s. Started around 10:23.",
  "service": "payments",
  "severity": "sev2",
  "logs": "2026-08-15 10:23:04 ERROR [payments] upstream connect timeout...",
  "incident_id": "optional-uuid-to-attach-the-report-to"
}
```

| Field | Required | Notes |
|---|---|---|
| `query` | yes | The question or symptom. Whitespace-only is rejected |
| `service` | no | Scopes retrieval. The single highest-signal filter |
| `severity` | no | `sev1`–`sev4`. Biases toward mitigation-first guidance |
| `logs` | no | Raw logs, any supported format. Up to 2 MB |
| `incident_id` | no | Attaches the report and advances status to `investigating` |

Response:

```json
{
  "report_id": "...",
  "session_id": "...",
  "report": {
    "summary": "Payments is timing out against the card gateway...",
    "confidence": 0.65,
    "hypotheses": [
      {
        "id": "H1",
        "statement": "Connection pool exhausted after the 10:20 deploy raised concurrency.",
        "confidence": 0.65,
        "reasoning": "Onset at 10:23 aligns with the deploy; runbook [2] describes this signature.",
        "evidence_ids": ["E1", "E3"],
        "disconfirming_checks": ["Check pool saturation; below 60% rules this out."]
      }
    ],
    "evidence": [
      { "id": "E1", "type": "log_pattern", "description": "4812 timeouts from 10:23:04", "supports": true, "strength": 0.8 }
    ],
    "immediate_actions": ["Enable queued authorisation to stop customer impact."],
    "remediation_steps": [
      {
        "order": 1,
        "action": "Inspect pgbouncer pool saturation.",
        "risk": "safe",
        "reversible": true,
        "command": "kubectl exec deploy/pgbouncer -- psql -c 'SHOW POOLS'",
        "addresses_hypothesis": "H1"
      }
    ],
    "open_questions": ["No metrics available, so saturation was not directly confirmed."],
    "citations": [ { "chunk_id": "...", "title": "Payments Runbook", "heading_path": "Troubleshooting > 503 errors", "relevance": 0.91 } ]
  },
  "markdown": "## Summary\n\n...",
  "needs_human_review": false,
  "duration_ms": 8421,
  "model": "gpt-4o-mini",
  "tool_calls": [ { "tool": "search_runbooks", "args": {...}, "result_preview": "..." } ]
}
```

**Check `needs_human_review`.** It is true when there is no hypothesis,
confidence is below 0.5, or any step carries `high` risk — route those to a
person rather than automating.

`markdown` is pre-rendered so clients need not reimplement the formatting.

Rate limited to 10/minute by default: investigations are expensive.

### `POST /incidents` · `GET /incidents` · `GET /incidents/{id}` · `PATCH /incidents/{id}`

Standard CRUD. `GET /incidents` supports `incident_status`, `service`, `limit`,
`offset`, and returns a total count so clients can paginate without a second
call.

`PATCH` is where **ground truth** is recorded:

```json
{ "status": "resolved", "root_cause": "Deploy raised concurrency without raising pool size." }
```

Setting a terminal status stamps `resolved_at` automatically if absent. Recording
`root_cause` turns every report previously generated for that incident into a
labelled evaluation example, and makes the incident available to
`find_similar_incidents` as confirmed precedent.

### `GET /incidents/{id}/reports`

Every report for an incident, newest first — the audit trail of what the agent
believed and when.

### `POST /incidents/reports/{id}/feedback`

```json
{ "was_helpful": false, "feedback": "Missed that the gateway was already degraded." }
```

→ `204`. The cheapest high-quality evaluation signal available.

---

## Knowledge base

### `POST /knowledge/search`

```json
{ "query": "connection pool exhausted", "service": "platform", "top_k": 5, "rerank": true }
```

Response includes per-stage timings so slow retrieval can be diagnosed without
server access:

```json
{
  "query": "connection pool exhausted",
  "hits": [
    {
      "chunk_id": "...",
      "title": "Database Connection Pool Exhaustion",
      "heading_path": "Diagnosis > Count connections by state",
      "source_type": "runbook",
      "service": "platform",
      "score": 0.94,
      "strategy": "hybrid",
      "content": "..."
    }
  ],
  "total_candidates": 38,
  "reranked": true,
  "timings_ms": { "embed": 41.2, "retrieve": 18.7, "fuse": 0.3, "rerank": 380.1, "total": 440.3 }
}
```

`strategy` is `vector`, `lexical`, or `hybrid` — `hybrid` means both arms found
it, which is the strongest relevance signal available.

### `POST /knowledge/documents/text` · `/upload` · `/path` — admin only

Three ingestion routes: inline text, multipart upload, or a server-side path.

`/path` resolves and confines the target to the project root. Without that
check the endpoint would be an arbitrary-file-read primitive: an admin could
ingest `/etc/shadow` and retrieve it through search.

All return an ingestion summary:

```json
{ "documents_processed": 12, "indexed": 9, "skipped_unchanged": 3, "failed": 0, "total_chunks": 147, "duration_ms": 8231.4 }
```

Set `"force": true` to re-index unchanged content — needed after changing the
chunking strategy or embedding model.

### `GET /knowledge/documents` · `DELETE /knowledge/documents/{id}`

List with `service` / `source_type` filters. Deletion cascades to chunks.

### `GET /knowledge/stats`

```json
{ "documents": 412, "chunks": 5834, "unembedded_chunks": 0, "services": 14, "documents_by_type": { "runbook": 380, "postmortem": 32 } }
```

**`unembedded_chunks > 0` means an ingestion run failed partway through.**
Re-run with `--force`.

### `POST /knowledge/analyze-logs`

Log analysis with no LLM and no database. Much cheaper than an investigation
when all you want is the error breakdown and the onset time.

```json
{ "logs": "2026-08-15 10:23:04 ERROR ...", "max_lines": 5000 }
```

→ `{ "analysis": { ... }, "summary": "## Log analysis\n\n..." }`

---

## Chat

Requires a **session** token.

### `POST /chat`

```json
{ "messages": [{ "role": "user", "content": "Why would checkout return 503?" }], "service": "payments" }
```

The final message must have `role: "user"` — otherwise the agent would be asked
to respond to its own last message, which produces untethered rambling.

Retrieval still runs for chat: even a casual follow-up benefits from grounding.

### `POST /chat/stream`

Server-sent events:

```
data: {"content": "The most likely", "done": false, "error": null, "citations": []}
data: {"content": " cause is", "done": false, "error": null, "citations": []}
data: {"content": "", "done": true, "error": null, "citations": [{...}]}
```

**Errors mid-stream cannot be an HTTP status** — headers are long gone. They
arrive as a final frame with `error` populated. Clients must check that field
rather than assuming a 200 means success.

Citations arrive only on the final frame, once retrieval has settled.

### `GET /chat/messages` · `DELETE /chat/messages`

Read or clear the session transcript. Tool calls are filtered out of the public
transcript — they remain available in `tool_calls` on investigation responses.

---

## Health

| Endpoint | Checks | Use for |
|---|---|---|
| `GET /health/live` | Nothing external | Kubernetes **liveness** |
| `GET /health/ready` | Database, pgvector, API key | Kubernetes **readiness** |
| `GET /health` | Same as ready, plus uptime | Dashboards |

Liveness deliberately checks nothing external. A liveness probe that touches the
database restarts every replica during a database blip — converting a
recoverable dependency outage into a restart storm at the worst possible moment.

Readiness returns `503` when a hard dependency is down, which removes the replica
from the load balancer while leaving it alive to recover.

```json
{
  "status": "ready",
  "version": "1.0.0",
  "checks": {
    "database": { "healthy": true, "connections_in_use": 2, "connections_idle": 18 },
    "pgvector": { "healthy": true, "detail": "extension installed" },
    "llm": { "healthy": true, "detail": "api key configured" }
  }
}
```

`/metrics` is mounted as a separate ASGI app, so scrapes bypass the middleware
stack entirely — a scrape must not appear in the HTTP metrics it reports, and
must not be rate limited.

---

## Rate limits

Keyed by authenticated user where possible, falling back to client IP. Keying
purely on IP would put everyone behind one corporate NAT or Kubernetes egress
gateway into a single bucket, so the first busy user locks out the rest of the
team — precisely the wrong failure during an incident.

| Endpoint | Default |
|---|---|
| `/incidents/investigate` | 10/min |
| `/chat` | 30/min |
| `/chat/stream` | 20/min |
| `/knowledge/search` | 60/min |
| `/knowledge/documents/*` | 20/hour |
| `/auth/login` | 20/min |
| `/auth/register` | 10/hour |

Responses carry `X-RateLimit-*`; 429s carry `Retry-After`.

Limits are **in-process**, so N replicas permit roughly N× the configured rate.
For strict global limits, point slowapi at Redis via `storage_uri` in
[`core/limiter.py`](../aegis/core/limiter.py).
