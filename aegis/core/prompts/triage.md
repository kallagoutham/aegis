# Incident triage

Classify an incoming request so the investigation can be routed and scoped correctly. You are not solving the problem here — you are deciding what to go and look for.

Current time: {{current_datetime}}

## Request

{{query}}

## Additional signals

{{signals}}

## What to determine

**intent** — what the user actually wants:
- `investigate` — diagnose a live or recent production problem
- `explain` — understand how a system or alert works, no active incident
- `procedure` — find the documented steps for a known task
- `followup` — continue or refine an investigation already in progress
- `unrelated` — not about this system's operations

**service** — which service is affected. Extract it from the text if named explicitly. Infer it only when the evidence is strong (a distinctive error string, a hostname, a URL path). Return `null` rather than guessing; a wrong service filter silently hides the correct runbook, which is worse than no filter at all.

**severity** — your read of impact, using `sev1` through `sev4`. `sev1` is a total outage or data loss, `sev4` is a minor issue with no customer impact. Return `null` if there is not enough information.

**search_queries** — two to four search phrasings for the knowledge base. This is the highest-value part of the output. Vary them deliberately:
- one using the user's own vocabulary
- one using the technical terms a runbook author would have used
- one built around any exact error string present
- one describing the underlying failure mode in general terms

Retrieval quality depends heavily on this, because the user's phrasing frequently shares no vocabulary at all with the runbook that answers them.

**needs_clarification** — `true` only when the request is too vague to search for meaningfully. A specific question is worth more than four bad searches. Set `false` if you can make a reasonable attempt.

**clarifying_question** — one specific question, if `needs_clarification` is true. Ask for the single most useful missing fact, not a checklist.

**key_terms** — distinctive literal tokens worth searching for exactly: error codes, exception class names, hostnames, metric names.

## Output

Respond with a single JSON object and nothing else:

{
  "intent": "investigate",
  "service": "payments",
  "severity": "sev2",
  "search_queries": [
    "checkout returns 503 during payment authorisation",
    "payment service upstream connect timeout troubleshooting",
    "PaymentGatewayTimeoutException",
    "downstream dependency timeout causing 503"
  ],
  "key_terms": ["503", "PaymentGatewayTimeoutException", "checkout"],
  "needs_clarification": false,
  "clarifying_question": null,
  "reasoning": "One sentence on why this classification."
}
