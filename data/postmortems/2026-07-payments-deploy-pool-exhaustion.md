---
title: Postmortem - Checkout 503s from payments pool exhaustion
service: payments
type: postmortem
severity: sev2
date: 2026-07-14
duration_minutes: 47
---

# Postmortem: checkout 503s, 2026-07-14

**Severity:** SEV2
**Duration:** 47 minutes (10:23 - 11:10 UTC)
**Impact:** 12% of checkout attempts failed. Roughly 3,400 customers affected.

## Summary

A routine `payments` deploy raised the async worker count from 4 to 12 per
replica without a corresponding increase in the gateway connection pool size,
which remained at 20. Under normal traffic the additional workers saturated the
pool within three minutes. Requests queued for a connection until the gateway's
30 second timeout elapsed, surfacing to `checkout` as 503s.

## Timeline (UTC)

- **10:20** - `payments` v2.14.0 deployed. Change raised `WORKER_CONCURRENCY`
  from 4 to 12.
- **10:23** - First `upstream connect timeout` in the payments logs. Error rate
  begins climbing.
- **10:26** - `CheckoutErrorRate` alert fires. On-call paged.
- **10:31** - On-call confirms pool saturation via `SHOW POOLS`: `cl_waiting`
  at 180 and rising.
- **10:34** - Queued authorisation enabled, which stopped customer-visible
  failures while the cause was still unconfirmed.
- **10:41** - Deploy correlated with onset. Rollback started.
- **10:52** - Rollback complete. Error rate returns to baseline.
- **11:10** - Queued authorisation disabled. Incident closed.

## Root cause

`WORKER_CONCURRENCY` and `GATEWAY_POOL_SIZE` are set in different config files
and there is no validation that the second is at least as large as the first.
The deploy tripled concurrency while leaving the pool unchanged, so each replica
could issue three times as many concurrent gateway requests as it had
connections for.

## What went well

- The queued-authorisation mitigation stopped customer impact 11 minutes before
  the root cause was identified. Mitigating before diagnosing was the right
  call.
- Onset time was unambiguous in the logs, which made deploy correlation fast.

## What went badly

- Seven minutes were spent checking the gateway vendor's status page first. The
  gateway was healthy the whole time. The deploy was three minutes before onset
  and should have been the first hypothesis.
- No alert fires on pool saturation itself, only on the downstream error rate,
  so the first signal was already customer-visible.

## Action items

- Add a startup assertion that `GATEWAY_POOL_SIZE >= WORKER_CONCURRENCY`.
- Alert on `cl_waiting > 0` sustained for two minutes.
- Add a "check recent deploys first" step to the top of the payments runbook.

## Lessons

When latency rises but CPU stays flat on both sides, the system is queueing, not
working. Look for a bounded resource - connections, threads, file descriptors -
before looking for slow code.
