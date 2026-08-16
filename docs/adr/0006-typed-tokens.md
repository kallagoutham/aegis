# 0006 — Typed JWTs to close a token-confusion flaw

**Status:** Accepted

## Context

Aegis issues two kinds of bearer token:

- **User tokens** — account-level operations: create sessions, run
  investigations, ingest documents.
- **Session tokens** — scoped to one conversation thread, so a narrower client
  component can be handed chat access without account access.

The original implementation minted both with the same function:

```python
def create_access_token(thread_id: str, ...) -> Token:
    to_encode = {"sub": thread_id, "exp": expire}
    return jwt.encode(to_encode, secret, algorithm=...)
```

`create_access_token(str(user.id))` and `create_access_token(session_id)`
produced **structurally identical tokens**. Nothing in the payload said which
was which.

Verification then did:

```python
async def get_current_session(credentials):
    session_id = verify_token(token)          # returns whatever is in 'sub'
    session = await db.get_session(session_id) # looks it up
```

So a token was accepted by whichever endpoint could resolve its `sub` to an
existing row. This is classic **token confusion**: a credential issued for one
purpose is valid for another.

In this codebase the practical exploitability was limited — user ids and session
ids were different types, so most cross-use produced a lookup miss rather than
access. But the property being relied on was *"these id spaces happen not to
overlap"*, which is not a security control. The moment both became UUIDs — which
they did during this rewrite — a session token would have been a valid user
token for the session's own id.

## Decision

Every token carries a `typ` claim, and verification **requires** the caller to
declare which type it expects:

```python
class TokenType(str, Enum):
    USER = "user"
    SESSION = "session"

def verify_token(token: str, expected_type: TokenType) -> TokenClaims:
    ...
    if payload.get("typ") != expected_type.value:
        raise AuthenticationError("This token is not valid for the requested operation.")
```

The type check runs **before any database lookup**, so a mismatched token is
rejected without touching the database.

## Additional hardening in the same change

| Claim | Purpose |
|---|---|
| `iss` | Verified on decode. A token minted by another service that happens to share the signing secret is rejected |
| `aud` | Same, for a different deployment of Aegis itself |
| `iat`, `nbf` | Issued-at and not-before; `iat` required |
| `jti` | Unique per token, so a future denylist can revoke one token rather than forcing a global secret rotation |

The signing key is validated at startup in staging and production: at least 32
characters, and not a known placeholder. A service that silently issued tokens
under a weak or public key would be worse than one that refuses to start.

## Alternatives considered

**Separate signing keys per token type.** Cryptographically stronger — a session
token would not even verify at a user endpoint. Rejected because it doubles key
management and rotation complexity for a threat the `typ` claim already closes,
given the key itself is protected.

**Different `sub` prefixes** (`user:abc`, `session:xyz`). Works, and encodes
type information in a field whose semantics are "the subject". A dedicated claim
is clearer and does not require parsing.

**Opaque session tokens in a database table.** Revocable by construction, and it
adds a database read to every request and gives up statelessness. A reasonable
future direction if revocation becomes a hard requirement — see the limitation
below.

## Consequences

**Good**

- A session token presented to a user endpoint is rejected structurally, not
  incidentally.
- Issuer and audience verification prevents token reuse across services and
  environments sharing a secret.
- Verification failures return a generic message; the specific reason goes to
  the log, so an attacker cannot use error text to iterate.
- `aegis` logs `token_type_mismatch` at WARNING, making replay attempts visible.

**Costs accepted**

- Marginally larger tokens.
- Existing tokens issued by the previous scheme are rejected — they lack `typ`.
  There is a test pinning exactly that. For a deployed system this would require
  a migration window; for a system that was not yet deployed, it is free.

**Known limitation, unchanged by this ADR**

There is still **no revocation**. Changing a password does not invalidate
existing tokens. The `jti` claim exists specifically so a Redis denylist can be
added without another token format change — but that denylist is not built. See
[security.md](../security.md#known-limitations).
