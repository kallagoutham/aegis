# Synthesise the investigation report

Turn the evidence gathered below into a structured incident report. This is the artefact an on-call engineer will act on, so precision matters more than completeness and honesty matters more than either.

Current time: {{current_datetime}}

## The question

{{query}}

## Reported severity

{{severity}}

## Knowledge base evidence

{{retrieved_context}}

## Log analysis

{{log_summary}}

## Prior incidents

{{past_incidents}}

## Investigation transcript

{{investigation_notes}}

---

## Rules

**Ground every claim.** Each hypothesis must reference evidence entries by id. A hypothesis with no supporting evidence should either be dropped or given a confidence below 0.3 and labelled as speculative in its reasoning.

**Record contradicting evidence.** If something in the material argues *against* your leading hypothesis, add it as an evidence entry with `"supports": false`. Omitting it is how a confident wrong answer gets produced.

**Calibrate honestly.**
- `0.8`–`1.0` — retrieved evidence directly supports this; a runbook or postmortem describes this exact failure
- `0.5`–`0.8` — evidence is consistent with this and inconsistent with the alternatives
- `0.3`–`0.5` — plausible pattern match, not directly evidenced
- below `0.3` — speculation, included only for completeness

**Give at least two hypotheses** when the evidence does not decisively identify one cause. For each, state a cheap disconfirming check — something the responder can run in under a minute that would rule it out. Ranking by confidence is how you express your actual belief; producing only one hypothesis claims certainty you rarely have.

**Never invent commands.** Populate `command` only when a retrieved source contains that exact command. Otherwise leave it `null` and describe the action in `action`. An invented command will be pasted into a production shell.

**Mark risk accurately.** `safe` is read-only or trivially reversible. `high` means possible data loss or extended outage. When in doubt, rate it higher — the cost of an unnecessary approval is a minute, the cost of an unflagged destructive action is the incident getting worse.

**Separate mitigation from fix.** `immediate_actions` are things that reduce customer impact right now, even before the cause is confirmed. `remediation_steps` address the underlying cause. During an active SEV1 or SEV2, `immediate_actions` is the more important field.

**State the gaps.** `open_questions` should name what you could not determine and what evidence would settle it. An empty `open_questions` on a genuinely uncertain investigation is a false claim of completeness.

## Output

Respond with a single JSON object matching this shape exactly, and nothing else:

{
  "summary": "Two to four sentences an incident commander could paste into a status update.",
  "severity_assessment": "sev2",
  "affected_services": ["payments", "checkout"],
  "evidence": [
    {
      "id": "E1",
      "type": "log_pattern",
      "description": "4812 occurrences of 'upstream connect timeout' beginning 10:23:04, rate ~340/min.",
      "source": "supplied log bundle",
      "supports": true,
      "strength": 0.8
    }
  ],
  "hypotheses": [
    {
      "id": "H1",
      "statement": "The payments service exhausted its downstream connection pool after the 10:20 deploy raised concurrency.",
      "confidence": 0.65,
      "reasoning": "Onset at 10:23 aligns with the deploy at 10:20; the runbook [2] describes this exact signature.",
      "evidence_ids": ["E1", "E3"],
      "disconfirming_checks": [
        "Check pool saturation metric; if below 60% this is not it.",
        "Confirm the 10:20 deploy actually changed concurrency settings."
      ]
    }
  ],
  "immediate_actions": [
    "Scale payments replicas to absorb the connection demand while the cause is confirmed."
  ],
  "remediation_steps": [
    {
      "order": 1,
      "action": "Inspect pgbouncer pool saturation.",
      "rationale": "Confirms or eliminates H1 in under a minute.",
      "risk": "safe",
      "reversible": true,
      "command": "kubectl exec deploy/pgbouncer -- psql -c 'SHOW POOLS'",
      "expected_outcome": "cl_waiting near zero if the pool is healthy.",
      "addresses_hypothesis": "H1"
    }
  ],
  "open_questions": [
    "No metrics were available, so pool saturation could not be confirmed directly."
  ],
  "confidence": 0.65
}
