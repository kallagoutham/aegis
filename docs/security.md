# Security

What Aegis defends against, how, and — importantly — what it does not.

## Threat model

Aegis is an **internal tool handling sensitive operational data**. Logs and
runbooks routinely contain hostnames, internal architecture, connection strings,
and occasionally credentials. The realistic threats are:

| Threat | Mitigation |
|---|---|
| Credential theft via token replay | Typed tokens, short lifetimes, issuer/audience verification |
| Privilege escalation | Role checks on ingestion and investigation |
| Arbitrary file read via ingestion | Path confinement to the project root |
| SQL injection | Bound parameters everywhere, no string interpolation of user input |
| Secret leakage into logs | `redact_secrets` before logging user-supplied text |
| Prompt injection via ingested documents | Partially mitigated — see [limitations](#known-limitations) |
| Resource exhaustion | Rate limits, upload caps, log line caps, iteration caps |
| Misconfiguration | Startup validation that refuses to boot when unsafe |

Not in scope: a malicious administrator (an admin can already read the whole
corpus by design), or physical/database-level access.

---

## Authentication

### Token confusion — a real vulnerability, now fixed

The original code minted user tokens and session tokens with the same function,
put the subject in `sub`, and had **nothing distinguishing them**.
`get_current_session` accepted any token whose `sub` matched a session id;
`get_current_user` accepted any token whose `sub` matched a user id. A token
issued for one purpose was structurally valid for the other.

Every token now carries a `typ` claim, and verification *requires* the caller to
state which type it expects:

```python
claims = verify_token(token, TokenType.USER)   # a session token is rejected here
```

The check happens before any database lookup. Tests
([`tests/unit/test_auth.py`](../tests/unit/test_auth.py)) pin both directions,
and an API-level test exercises it through the real dependency stack.

### Other token hardening

| Claim | Purpose |
|---|---|
| `iss`, `aud` | Verified on decode — a token minted by another service sharing the secret is rejected |
| `iat`, `nbf`, `exp` | Required; expiry enforced |
| `jti` | Unique per token, so a denylist could target one token rather than forcing a global secret rotation |

Verification failures return a deliberately generic message. The specific reason
goes to the log — telling an attacker *which* check failed helps them iterate.

### Passwords

NIST SP 800-63B rather than the older composition rules:

- 12 character minimum, 72 byte maximum (bcrypt truncates silently past 72, so a
  longer password gives a false sense of added strength).
- Screened against common passwords, repeated characters, keyboard sequences,
  and low character variety.
- **No** "one uppercase, one symbol" requirement — those mostly produce
  `Password1!`.

bcrypt at cost 12 (~250ms). Tests drop to 4 automatically.

`verify_password` returns `False` rather than raising on a malformed stored
hash, so a legacy row is a failed login rather than a 500.

### Timing

Login returns an identical error whether the account is absent or the password
is wrong, and performs a **dummy hash** in the missing-user case. Without that,
response timing alone reveals which emails are registered.

---

## Authorization

| Role | Permissions |
|---|---|
| `viewer` | Read incidents, search |
| `responder` | Also create incidents, run investigations |
| `admin` | Also ingest and delete documents |

Deliberately coarse. Fine-grained permissions belong in an authorization
service, not baked into the user row where every change needs a migration.

Session ownership is verified explicitly (`verify_session_owner`) — a valid
token for session A must not read session B.

Deactivated accounts (`is_active = false`) are rejected at the dependency, so
disabling an account takes effect immediately for new requests without deleting
history.

---

## Input handling

### Escaping is a rendering concern, not an input concern

The original code ran `html.escape()` over every incoming value, including auth
tokens and log text. That is escaping at the wrong boundary, and it breaks this
product specifically: a stack trace containing `List<String>` was silently
rewritten to `List&lt;String&gt;` before reaching the analyser, corrupting
template extraction and every downstream citation.

The input boundary now:

- **rejects** what can never be legitimate — null bytes (Postgres cannot store
  them, so allowing them turns a clear 422 into a confusing 500), control
  characters, script tags, inline event handlers, oversized payloads;
- **normalises** what has a canonical form — emails are lowercased and
  NFKC-normalised so a Unicode homoglyph cannot register as a second account;
- **leaves everything else exactly as sent**, so the analyser sees what actually
  happened on the wire.

`escape_html` exists and is called at render time.

### SQL

Every query uses bound parameters, including the vector literal in
[`vector_store.py`](../aegis/retrieval/vector_store.py) — it is serialised to a
string and bound, then cast with `::vector`, never interpolated.

The one place a value is interpolated into SQL is the checkpoint table name in
`clear_history`, and those names come from settings, never from user input.

### Path traversal

`POST /knowledge/documents/path` resolves the target and confines it to the
project root:

```python
resolved = candidate.resolve()
if not resolved.is_relative_to(PROJECT_ROOT.resolve()):
    raise AuthorizationError(...)
```

Comparing **fully resolved** paths catches symlinks pointing outside the root as
well as literal `..` traversal. Without this the endpoint is an arbitrary-file-
read primitive: an admin could ingest `/etc/shadow` and then retrieve its
contents through search.

### Uploads

Size is checked against the **decoded body**, not a declared `Content-Length` —
which a client controls and can understate.

---

## Transport and headers

Set by `SecurityHeadersMiddleware`:

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` |
| `Permissions-Policy` | `geolocation=(), microphone=(), camera=()` |
| `Strict-Transport-Security` | production only |

HSTS is production-only deliberately: sending it from a local HTTP dev server
pins the browser to HTTPS for localhost, which is genuinely painful to undo.

CORS lists explicit origins. `*` combined with credentials is rejected at
startup in deployed environments.

---

## Error handling

Errors never leak internals. `AegisError.context` holds diagnostic data — user
ids, upstream error text, connection details — and is **logged but never
serialised** into a response.

Unhandled exceptions return a generic message plus the request id. The full
traceback goes to the log, correlated by that id, so a user can report a failure
without receiving a stack trace.

In development only, the exception type and message are included.

---

## Container hardening

- Runs as unprivileged uid 1001, never root.
- Multi-stage build: compilers stay in the builder, so the runtime carries
  neither `build-essential` nor `libpq-dev`.
- Compatible with `readOnlyRootFilesystem: true` given `LOG_TO_FILE=false` and a
  writable `/tmp`.
- `HEALTHCHECK` hits `/health/live` only.

---

## Known limitations

Stated plainly, because a security document that only lists strengths is not
useful.

### No token revocation

Changing a password does **not** invalidate existing tokens. There is no
denylist. A stolen token is valid until it expires (24h for user tokens, 7d for
session tokens).

*Mitigation today*: shorten `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`. *Proper fix*: a
Redis denylist keyed on `jti` — the claim is already issued for this purpose,
checked on decode, with entries expiring alongside the token.

### Prompt injection via ingested documents

Anyone who can write to the knowledge base can plant text designed to steer the
agent. Since ingestion is admin-only and the corpus is internal documentation,
this is a lower-severity concern than for a public-facing RAG system — but it is
not zero.

Partially mitigated: retrieved content is delimited into numbered blocks, the
system prompt instructs the model never to invent commands, and `command` fields
are only populated from retrieved sources. None of that is a hard boundary. Do
not let untrusted parties write to the corpus.

### Rate limits are per-replica

In-process. N replicas permit roughly N× the configured rate. For strict global
limits, point slowapi at Redis via `storage_uri`.

### `X-Forwarded-For` is trusted

The rate limiter reads it when no authenticated subject is present. That is
correct behind a proxy that overwrites the header, and wrong if the service is
exposed directly to the internet — a client could forge it and evade limits
entirely. Always run behind a proxy you control.

### Logs may contain sensitive data

`redact_secrets` catches common credential shapes, but a novel format will pass
through. Treat Aegis logs with the same care as the logs it analyses, and apply
the same retention policy.

### No audit log

Investigations and reports are persisted, so there is a record of agent
activity. There is no separate tamper-evident audit trail of who read what.

### No encryption at rest

Handled at the database or volume layer, not by the application. Embeddings are
lossy but not opaque — content can be partially reconstructed from them, so
treat the vector column as sensitive as the source text.

---

## Reporting a vulnerability

Open a private security advisory rather than a public issue.

## Checklist before exposing this to real incident data

- [ ] `JWT_SECRET_KEY` from a secret manager, 32+ random bytes (`make secret`)
- [ ] `POSTGRES_PASSWORD` not a default
- [ ] `ALLOWED_ORIGINS` lists real origins, not `*`
- [ ] `APP_ENV=production` (enables the startup hardening checks)
- [ ] TLS terminated at the proxy; HSTS reaching clients
- [ ] Running behind a proxy that overwrites `X-Forwarded-For`
- [ ] Database not reachable from outside the cluster
- [ ] Log retention policy matches the sensitivity of ingested logs
- [ ] Admin accounts limited to people who should read the whole corpus
- [ ] Backups encrypted
