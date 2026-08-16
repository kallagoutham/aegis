---
title: Payments - Gateway Timeouts and 503s
service: payments
type: runbook
owner: payments-platform
severity_hint: sev2
---

# Payments: gateway timeouts and 503s

Covers the case where checkout returns HTTP 503 and the payments service logs
`PaymentGatewayTimeoutException` or `upstream connect timeout`.

## Symptoms

- Checkout returns 503 to clients, usually within 30 seconds of the request.
- `payments` logs show repeated `upstream connect timeout to gateway-*`.
- The `payments_gateway_request_duration_seconds` p95 climbs above 25s.
- Authorisation success rate drops while traffic volume stays flat.

If traffic volume also dropped, this is probably a *downstream* symptom of an
edge or DNS problem rather than a payments fault. Check the ingress first.

## Architecture context

`checkout` calls `payments`, which calls the third-party card gateway through a
connection pool managed by PgBouncer-style pooling in `payments-gateway-proxy`.
The pool defaults to 20 connections per replica. The gateway enforces a hard
30 second timeout and returns no partial response.

## Diagnosis

### 1. Confirm the pool is not saturated

```bash
kubectl exec -n payments deploy/payments-gateway-proxy -- \
  psql -h 127.0.0.1 -p 6432 -U pgbouncer -c 'SHOW POOLS'
```

`cl_waiting` above zero for a sustained period means clients are queueing for a
connection. If `cl_waiting` is 0 and `sv_active` is well below `pool_size`, the
pool is healthy and the cause is elsewhere - skip to step 3.

### 2. Check whether a recent deploy changed concurrency

```bash
kubectl rollout history -n payments deploy/payments
```

Concurrency regressions are the most common cause of this failure. A deploy
that raises worker count without raising `GATEWAY_POOL_SIZE` will exhaust the
pool under normal traffic. Correlate the deploy timestamp against the onset of
the first timeout in the logs.

### 3. Check gateway health directly

```bash
curl --max-time 10 -s -o /dev/null -w '%{http_code} %{time_total}\n' \
  https://gateway.example.com/healthz
```

If the gateway itself is slow, this is a vendor incident. Escalate to the
vendor and move to the mitigation section - do not keep scaling replicas, which
will increase load on an already-struggling upstream.

### 4. Check for DNS resolution latency

```bash
kubectl exec -n payments deploy/payments -- \
  dig +stats gateway.example.com | grep 'Query time'
```

Query times above 100ms indicate a CoreDNS problem, which presents as
connection timeouts that look identical to gateway slowness.

## Mitigation

Apply these before the root cause is confirmed if customer impact is active.

1. **Enable queued authorisation.** Accepts the payment and settles
   asynchronously, so checkout stops returning 503.

   ```bash
   kubectl set env -n payments deploy/checkout ENABLE_QUEUED_AUTH=true
   ```

   Reversible. Adds up to 5 minutes of settlement delay.

2. **Scale payments replicas.** Only helps if the pool is saturated and the
   gateway is healthy. Makes things worse if the gateway is the bottleneck.

   ```bash
   kubectl scale -n payments deploy/payments --replicas=6
   ```

## Resolution

If a deploy caused it, roll back:

```bash
kubectl rollout undo -n payments deploy/payments
```

If the pool is genuinely undersized for current traffic, raise it. The gateway
permits 200 concurrent connections per tenant, so `pool_size x replicas` must
stay under that ceiling.

```bash
kubectl set env -n payments deploy/payments-gateway-proxy GATEWAY_POOL_SIZE=40
```

## Escalation

Page the payments on-call rota if authorisation success rate stays below 95%
for more than 10 minutes. If the gateway is confirmed at fault, open a P1 with
the vendor and notify the finance on-call, since settlement will be delayed.
