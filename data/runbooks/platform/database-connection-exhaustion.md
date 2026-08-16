---
title: Platform - Database Connection Pool Exhaustion
service: platform
type: runbook
owner: platform-infra
severity_hint: sev2
---

# Database connection pool exhaustion

Applies to any service backed by the shared `pg-primary` cluster.

## Symptoms

- Application logs show `connection pool exhausted`, `QueuePool limit of size N
  overflow M reached`, or `TimeoutError: pool timeout`.
- Request latency rises sharply while CPU on both app and database stays low.
  Low CPU with high latency is the signature of queueing, not of work.
- `pg_stat_activity` shows many sessions in `idle in transaction`.

## Why this happens

Postgres allocates a backend process per connection, so `max_connections` is
finite (600 on `pg-primary`). Each application replica holds its own pool. Total
demand is `replicas x (pool_size + max_overflow)`. Scaling replicas without
lowering per-replica pool size is the usual way a service exhausts the cluster.

The second common cause is a leaked transaction: code that opens a transaction
and does slow work - an HTTP call, an LLM request - before committing. The
connection is held for the entire duration.

## Diagnosis

### 1. Count connections by state and application

```sql
SELECT application_name, state, count(*)
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY 1, 2
ORDER BY 3 DESC;
```

A large `idle in transaction` count identifies a leak. A large `active` count
with slow queries identifies genuine load.

### 2. Find long-running transactions

```sql
SELECT pid, now() - xact_start AS duration, state, left(query, 120) AS query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
ORDER BY duration DESC
LIMIT 20;
```

Anything above a few seconds in `idle in transaction` is a bug, not load.

### 3. Check headroom

```sql
SELECT
  (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_conn,
  (SELECT count(*) FROM pg_stat_activity) AS current_conn;
```

## Mitigation

1. **Terminate leaked transactions.** Frees connections immediately.
   Destructive to the affected requests, which will error.

   ```sql
   SELECT pg_terminate_backend(pid)
   FROM pg_stat_activity
   WHERE state = 'idle in transaction'
     AND now() - state_change > interval '5 minutes';
   ```

2. **Reduce per-replica pool size** so total demand fits under
   `max_connections`. Requires a restart.

   ```bash
   kubectl set env deploy/<service> POSTGRES_POOL_SIZE=10 POSTGRES_MAX_OVERFLOW=5
   ```

Do **not** raise `max_connections` as a first response. Each connection costs
roughly 10 MB of backend memory, and raising it converts a connection problem
into an out-of-memory problem on the database host.

## Resolution

Long term, put PgBouncer in transaction mode in front of the cluster so
hundreds of application connections multiplex onto a few dozen backends. Note
that transaction-mode pooling breaks prepared statements - set
`prepare_threshold=None` on psycopg clients.

## Related

- Aegis holds two separate pools by design: SQLAlchemy for ORM traffic and
  psycopg for LangGraph checkpointing. Both count toward the cluster total.
