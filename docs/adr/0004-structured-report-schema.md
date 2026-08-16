# 0004 — Force the agent into a validated report schema

**Status:** Accepted

## Context

The natural output of an LLM investigation is prose. Prose is what models are
best at, and it is what a human would write.

It is also unusable as a product surface:

- **Unevaluable.** You cannot score "was this right?" against a paragraph.
- **Unrenderable.** Every client reimplements parsing to highlight the
  recommendation.
- **Unactionable.** Nothing can branch on "this needs a human".
- **Unfalsifiable.** Prose blurs the line between "the logs show X" and "I think
  X", which is exactly the distinction that matters at 03:00.

## Decision

Force synthesis into `InvestigationReportSchema` — a Pydantic model with
validators that repair known LLM failure modes. Persist it as JSONB, render
markdown from it for display.

## Alternatives considered

**Free text.** Rejected for the reasons above.

**Free text plus a post-hoc extraction pass.** Two LLM calls, and the extractor
can only recover what the writer happened to include. If the model never
considered a second hypothesis, no extractor invents one. Asking for the
structure *up front* changes what the model reasons about.

**Function calling / native structured output.** Used where available —
`complete_json` sets `response_format: json_object`. But gateways ignore it,
self-hosted models vary, and even compliant providers return valid JSON that is
semantically inconsistent. Schema validation on our side is required regardless.

**Loose JSON without validation.** Rejected. Model output is *structurally*
valid and *internally* inconsistent often enough that the validators earn their
keep — see below.

## Fields that exist for a specific reason

| Field | Why |
|---|---|
| `hypotheses[]` (plural) | A single hypothesis is a guess dressed as a conclusion. Asking for two or three forces the model to consider alternatives it would otherwise skip |
| `confidence` per hypothesis | Makes belief explicit and comparable |
| `disconfirming_checks` | The most valuable field. "What cheap observation would rule this out?" forces testable claims and gives the responder something to *do* |
| `evidence[].supports: false` | Contradicting evidence is modelled explicitly. Recording it is what separates analysis from confirmation bias |
| `remediation_steps[].risk` | "Restart the pod" and "failover the primary" must be visibly different *before* someone runs one |
| `remediation_steps[].command: null` | Populated only from retrieved sources. An invented command gets pasted into a production shell |
| `immediate_actions` | Separate from remediation. Stopping the bleeding and fixing the cause are different decisions on different timescales |
| `open_questions` | What it could not determine. An empty list on an uncertain investigation is a false claim of completeness |
| `citations` | Attached from *state*, not model output — trusting the model to reproduce citation metadata is how a fabricated source gets rendered as real |

## Validators, and what they fix

Models produce these errors reliably, not occasionally:

| Validator | Failure it repairs |
|---|---|
| `_sync_confidence_with_leading_hypothesis` | Top-level confidence left at 0.0 next to a 0.9 hypothesis |
| `_drop_dangling_evidence_references` | A hypothesis citing `E7` when no `E7` was defined — a hallucinated citation that would render as real |
| `_renumber_remediation_steps` | Duplicate or skipped ordinals, which render an out-of-order checklist |
| `_rank_hypotheses` | Hypotheses not ordered by confidence |

Dangling references are **dropped, not rejected**. Failing the whole report over
one bad reference would deny the responder a mostly-good report during an
incident.

`extra="ignore"` for the same reason — models add stray keys, and discarding
them beats failing.

## Consequences

**Good**

- `needs_human_review` is computable: no hypothesis, confidence below 0.5, or
  any high-risk step. Callers can route rather than automate.
- `aegis_agent_confidence` becomes a monitorable metric. Persistent low
  confidence means poor grounding.
- Reports are comparable over time and against the human-recorded `root_cause`.
- `to_markdown()` gives every client identical rendering for free.

**Costs accepted**

- The synthesis prompt is long, and contains a full JSON example. That is what
  drove the `{{placeholder}}` change — `str.format()` chokes on JSON braces.
- Occasional retries when output does not parse. `complete_json` feeds the parse
  error back to the model, which is markedly more effective than a blind retry.
- Schema changes need care once reports are persisted. Mitigated by JSONB
  storage and `extra="ignore"`, so old rows remain readable.

## On evaluation

This is the decision that makes evaluation possible at all. Once a human records
the true `root_cause` on a resolved incident, every historical report for it
becomes a labelled example: did the leading hypothesis match? Was confidence
calibrated? Did `open_questions` name the thing that turned out to matter?

That scorer is not written yet — see the README's project status — but the data
model supports it, which was the point of separating `Incident` (fact) from
`InvestigationReport` (opinion at a point in time).
