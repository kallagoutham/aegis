# Log analysis

Turning 10,000 log lines into something a model can reason about.

Source: [`aegis/analysis/`](../aegis/analysis/)

This module has **no dependencies** — no database, no network, no API key. It is
pure computation over strings, which is why `aegis analyze file.log` works with
nothing configured and why its tests run in milliseconds.

---

## The problem

A 10,000-line log bundle does not fit in a context window. Pasting the first
2,000 lines is worse than useless: the beginning of a log is almost always the
least interesting part of an incident, because the failure has not started yet.

But production logs are extremely repetitive. Ten thousand lines are typically
a few dozen *templates* with variable parts substituted:

```
Connection to db-primary-7f3a timed out after 30000ms
Connection to db-primary-9c21 timed out after 30000ms
Connection to db-replica-2b8e timed out after 5000ms
```

all reduce to:

```
Connection to <ID> timed out after <DURATION>   ×3
```

Collapsing to templates compresses by 1–3 orders of magnitude while
**increasing** signal. A template with a count of 4,812 that first appeared at
10:23 is a far stronger lead than any individual line.

---

## Parsing

Source: [`parser.py`](../aegis/analysis/parser.py)

Engineers paste whatever their platform emits. In practice that is one of four
shapes, and a tool that only understands one of them is useless during the
incident where the other three show up.

| Format | Example |
|---|---|
| JSON lines | `{"ts":"...","level":"error","service":"payments","msg":"..."}` |
| logfmt | `ts=... level=error service=payments msg="..."` |
| Bracketed text | `2026-08-15 10:23:04 ERROR [payments] message` |
| Free text | anything else, including stack traces |

Parsers are tried in order of specificity. Nothing is ever discarded: an
unparseable line still becomes an entry carrying its raw text, because the line
a parser cannot understand is disproportionately often the interesting one.

### Level normalisation

Real logs use a wide vocabulary — `WARN`/`WARNING`, `ERR`/`ERROR`,
`CRIT`/`FATAL`/`PANIC`. All collapse onto a fixed `LogLevel`, so downstream
analysis can count errors without enumerating synonyms.

### Timestamps

ISO-8601 (with and without timezone), space-separated, Common Log Format,
syslog, epoch seconds, and epoch milliseconds. The seconds/milliseconds
distinction uses a magnitude heuristic — values past ~2001 in milliseconds are
far larger than any plausible epoch-seconds value for a live system.

**Naive datetimes are assumed UTC.** That assumption is stated rather than
silently applied, because mixing naive local times with UTC produces an incident
timeline that is wrong by hours — and the resulting "the error happened before
the deploy" conclusion is confidently backwards.

### Stack traces

Continuation lines — indented text, `at com.example...`, `Caused by:`,
`Traceback`, `File "..."` — are folded into the entry above them.

A stack trace is **one event**. Splitting it would both inflate error counts and
destroy the frame ordering that identifies the failing code.

---

## Template extraction

Source: [`clustering.py`](../aegis/analysis/clustering.py)

The approach is a simplified [Drain](https://github.com/logpai/Drain3): mask
known-variable token classes with regexes, then group by the masked form.

Full Drain builds a parse tree to *discover* variable positions without prior
knowledge. That is more general, but the token classes that actually vary in
production logs are well known, and masking them directly is faster,
deterministic, and needs no training corpus.

### Masks, most specific first

| Placeholder | Matches |
|---|---|
| `<UUID>` | Canonical UUIDs |
| `<TIMESTAMP>` | Embedded date-times |
| `<IP>` | IPv4, optionally with port |
| `<MAC>` | MAC addresses |
| `<URL>` | http/https URLs |
| `<EMAIL>` | Email addresses |
| `<HEX>` | `0x...` |
| `<HASH>` | Long hex runs (16+) |
| `<ID>` | Separator-joined identifiers ending in digits: `pod-7f3a`, `worker_12` |
| `<DURATION>` | Numbers with a unit suffix: `30000ms`, `1.5s`, `256MB` |
| `<FLOAT>`, `<NUM>` | Bare numbers |
| `<PATH>` | Filesystem paths |
| `<STR>` | Quoted strings |

Order matters: a UUID would otherwise be partly consumed by the hex rule.

Only the **first line** of a multi-line entry is templated — a stack trace's
frames are variable detail whose inclusion would make every trace its own
template.

### Cluster metadata

Each cluster records count, highest severity seen, contributing services, first
and last occurrence, derived rate per minute, up to three verbatim examples, and
source line numbers.

Highest severity wins: a template appearing at both INFO and ERROR is surfaced
at ERROR.

---

## Anomaly detection

Heuristics, not statistics — and phrased as **observations, not conclusions**.
Their job is to direct the model's attention. A burst of errors at 10:23 is a
fact; *why* is the agent's problem.

| Heuristic | Fires when | Why it matters |
|---|---|---|
| **Error burst** | A time bucket carries >3× the mean error count, minimum 5 | Localises the onset |
| **Delayed onset** | First error appears after the first 20% of the window | A clean period followed by errors implies a discrete trigger — a deploy, a config change, a dependency failing |
| **Dominant template** | One template is >60% of all errors | Focuses attention on the actual signal |
| **High rate** | An error template exceeds 60/min | Retry storm or tight failure loop |
| **Multi-service correlation** | One error template spans several services | Suggests a shared dependency rather than a single-service fault |

### Time bucketing

Bucket **width** is derived from the observed span, not fixed. A five-minute
capture gets fifteen-second buckets; a six-hour one gets eighteen-minute
buckets. A fixed width would produce either one bucket or thousands depending
on the input.

---

## Output

### Prompt summary

`LogAnalysis.to_prompt_summary()` renders compact text for the model. Errors are
listed **before** general activity, which matches how an engineer reads a log
during an incident and takes advantage of the fact that models weight earlier
context more heavily.

```
## Log analysis

4812 entries spanning 2026-08-15T10:20:00+00:00 to 2026-08-15T10:47:31+00:00 (1651s).
Severity breakdown: error=4801, info=11
Services present: payments, checkout
7 distinct message templates.

### Error and warning templates (most frequent first)
1. [ERROR] x4801 - [payments] Connection to <ID> timed out after <DURATION>
   first: 2026-08-15T10:23:04+00:00, rate: 174.6/min, services: payments
   example: 2026-08-15 10:23:04 ERROR [payments] Connection to db-primary-7f3a ...

### Detected anomalies
- Errors begin at 2026-08-15T10:23:00+00:00, after a clean initial period -
  consistent with a discrete triggering event (deploy, config change, dependency failure).
- One template accounts for 100% of all errors (4801 of 4801)
```

### Structured output

`to_dict()` returns the full analysis for the API and for storage: level counts,
error rate, services, formats detected, time range with duration, template
count, top error templates, anomalies, and a truncation flag.

---

## Truncation is always flagged

`AGENT_MAX_LOG_LINES` (default 5000) bounds parsing so a pasted 500 MB log
cannot exhaust worker memory. When the cap is hit, `truncated` is set and the
summary says:

```
NOTE: input was truncated; counts are a lower bound.
```

Silent truncation would let a responder believe they have seen the whole picture
when they have seen a fraction of it.

---

## Using it directly

```bash
# Human-readable summary
aegis analyze /var/log/payments.log

# Structured JSON
aegis analyze /var/log/payments.log --json

# Over HTTP, no LLM involved
curl -sX POST localhost:8000/api/v1/knowledge/analyze-logs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"logs": "..."}' | jq .summary -r
```

In Python:

```python
from aegis.analysis import analyse_logs

analysis = analyse_logs(open("payments.log").read())

print(analysis.error_rate)                       # 0.9977
print(analysis.error_clusters[0].template)       # "Connection to <ID> timed out after <DURATION>"
print(analysis.error_clusters[0].count)          # 4801
print(analysis.error_clusters[0].first_seen)     # 2026-08-15 10:23:04+00:00
print(analysis.anomalies)
```

---

## Extending it

**A new log format** — add a `_parse_*_line` function in `parser.py` and put it
in the `parse_line` chain ahead of the plain-text fallback. Order by
specificity: a parser that matches too eagerly will steal lines from the ones
after it.

**A new mask** — add a pattern to `_MASKS` in `clustering.py`. Position it by
specificity; a broad pattern placed early will consume text a narrower one
should have matched.

**A new anomaly heuristic** — add it to `detect_anomalies`. Keep the phrasing
observational. A heuristic that asserts a cause will be believed, and heuristics
are not good enough to be believed.
