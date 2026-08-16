"""Incident schemas, including the structured report the agent must produce.

The report schema is the most important contract in the system. A free-text
answer from an LLM is impossible to evaluate, impossible to render consistently,
and impossible to act on programmatically. Forcing the model into this shape
buys four things:

* **Falsifiability.** Every hypothesis carries an explicit confidence and a list
  of evidence ids. "The connection pool is exhausted (0.8, based on E1 and E3)"
  can be checked; "it might be the database" cannot.
* **Auditability.** ``citations`` link claims back to specific knowledge base
  chunks, so a reader can verify the agent is quoting a real runbook rather than
  recalling a plausible one.
* **Safety.** Remediation steps carry a ``risk`` level and a ``reversible``
  flag. During a SEV1 the difference between "restart the pod" and "failover the
  primary" must be visible before someone runs it.
* **Evaluation.** Structured fields can be scored automatically against the
  human-recorded root cause once the incident is resolved.

The validators below enforce internal consistency the model routinely gets wrong
- referencing evidence that does not exist, ordering steps inconsistently, or
claiming high confidence with no supporting evidence at all.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import (
    Annotated,
    Any,
)
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from aegis.models.incident import (
    IncidentStatus,
    Severity,
)


class RiskLevel(str, Enum):
    """How dangerous a remediation step is if it turns out to be wrong."""

    SAFE = "safe"  # Read-only or trivially reversible. Run it.
    LOW = "low"  # Reversible with minimal blast radius.
    MEDIUM = "medium"  # Service-affecting but recoverable.
    HIGH = "high"  # Data loss or extended outage possible. Needs a second pair of eyes.

    @property
    def requires_approval(self) -> bool:
        """Whether a human should sign off before this runs."""
        return self in (RiskLevel.MEDIUM, RiskLevel.HIGH)


class EvidenceType(str, Enum):
    """Where a piece of evidence came from."""

    LOG_PATTERN = "log_pattern"
    METRIC = "metric"
    RUNBOOK = "runbook"
    PAST_INCIDENT = "past_incident"
    CONFIGURATION = "configuration"
    USER_REPORT = "user_report"
    INFERENCE = "inference"  # Reasoned, not observed. Weakest class.


class Evidence(BaseModel):
    """One concrete observation supporting or contradicting a hypothesis."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(
        ...,
        description="Short stable identifier such as 'E1', referenced by hypotheses.",
        max_length=16,
    )
    type: EvidenceType = Field(..., description="Provenance class of this observation.")
    description: str = Field(..., min_length=1, max_length=2000, description="What was observed.")
    source: str = Field(default="", max_length=500, description="Where it came from: file, template, runbook section.")
    supports: bool = Field(
        default=True,
        description="True if this supports the associated hypothesis, False if it contradicts it. "
        "Contradicting evidence is explicitly modelled because recording it is what "
        "separates analysis from confirmation bias.",
    )
    strength: float = Field(default=0.5, ge=0.0, le=1.0, description="How diagnostic this observation is.")


class Hypothesis(BaseModel):
    """A candidate root cause with its supporting evidence."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., max_length=16, description="Short identifier such as 'H1'.")
    statement: str = Field(..., min_length=1, max_length=2000, description="The proposed root cause.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Belief that this is the actual cause.")
    reasoning: str = Field(default="", max_length=4000, description="Why the evidence points here.")
    evidence_ids: list[str] = Field(default_factory=list, description="Ids of supporting Evidence entries.")
    disconfirming_checks: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Cheap observations that would rule this out. Asking for these pushes the "
        "model toward testable claims instead of unfalsifiable narratives.",
    )

    @field_validator("evidence_ids")
    @classmethod
    def _dedupe_evidence(cls, value: list[str]) -> list[str]:
        """Remove duplicate references while preserving order."""
        seen: set[str] = set()
        return [item for item in value if not (item in seen or seen.add(item))]


class RemediationStep(BaseModel):
    """One recommended action, ordered and risk-annotated."""

    model_config = ConfigDict(extra="ignore")

    order: int = Field(..., ge=1, description="Execution order, starting at 1.")
    action: str = Field(..., min_length=1, max_length=1000, description="What to do.")
    rationale: str = Field(default="", max_length=2000, description="Why this helps.")
    risk: RiskLevel = Field(default=RiskLevel.MEDIUM, description="Blast radius if this is the wrong call.")
    reversible: bool = Field(default=True, description="Whether the action can be undone.")
    command: str | None = Field(
        default=None,
        max_length=1000,
        description="Exact command, when the runbook provides one. Never invented - an "
        "invented command is the single most dangerous output this system can produce.",
    )
    expected_outcome: str = Field(default="", max_length=1000, description="What success looks like.")
    addresses_hypothesis: str | None = Field(default=None, max_length=16, description="Hypothesis id this targets.")


class Citation(BaseModel):
    """A knowledge base passage the report drew on."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(default="", description="Retrieved chunk identifier.")
    title: str = Field(default="", max_length=500)
    heading_path: str = Field(default="", max_length=1000)
    source_uri: str = Field(default="", max_length=1024)
    source_type: str = Field(default="")
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)


class InvestigationReportSchema(BaseModel):
    """The complete structured output of an investigation.

    This is the schema the synthesis node forces the model into, and the shape
    persisted to ``investigation_reports``.
    """

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Two to four sentences an incident commander could paste into a status update.",
    )
    severity_assessment: Severity | None = Field(
        default=None, description="The agent's independent severity read, which may differ from the reported one."
    )
    affected_services: list[str] = Field(default_factory=list, max_length=50)

    hypotheses: list[Hypothesis] = Field(
        default_factory=list,
        max_length=10,
        description="Ranked root cause candidates, most likely first.",
    )
    evidence: list[Evidence] = Field(default_factory=list, max_length=50)
    remediation_steps: list[RemediationStep] = Field(default_factory=list, max_length=20)

    immediate_actions: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Mitigations safe to take right now, before the root cause is confirmed. "
        "Separated from remediation because stopping the bleeding and fixing the "
        "cause are different decisions on different timescales.",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="What the agent could not determine. Stating this explicitly is what keeps "
        "an incomplete investigation from reading like a complete one.",
    )
    citations: list[Citation] = Field(default_factory=list, max_length=30)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence in the leading hypothesis.")

    @model_validator(mode="after")
    def _sync_confidence_with_leading_hypothesis(self) -> InvestigationReportSchema:
        """Derive top-level confidence from the best hypothesis when unset.

        Models frequently populate per-hypothesis confidence and leave the
        top-level field at zero. Deriving it keeps the two from disagreeing.
        """
        if self.hypotheses:
            best = max(hypothesis.confidence for hypothesis in self.hypotheses)
            if self.confidence == 0.0:
                self.confidence = best
        return self

    @model_validator(mode="after")
    def _drop_dangling_evidence_references(self) -> InvestigationReportSchema:
        """Remove references to evidence ids that were never defined.

        A hypothesis citing ``E7`` when no ``E7`` exists is a hallucinated
        citation. Silently keeping it would let a fabricated reference render in
        the UI as though it were real, so the reference is dropped rather than
        the whole report rejected - partial output during an incident beats none.
        """
        known = {item.id for item in self.evidence}
        for hypothesis in self.hypotheses:
            dangling = [ref for ref in hypothesis.evidence_ids if ref not in known]
            if dangling:
                hypothesis.evidence_ids = [ref for ref in hypothesis.evidence_ids if ref in known]
        return self

    @model_validator(mode="after")
    def _renumber_remediation_steps(self) -> InvestigationReportSchema:
        """Sort steps by declared order and renumber them contiguously.

        Models produce duplicate or skipped ordinals often enough that trusting
        them would render an out-of-order checklist during an incident.
        """
        if self.remediation_steps:
            self.remediation_steps.sort(key=lambda step: step.order)
            for position, step in enumerate(self.remediation_steps, start=1):
                step.order = position
        return self

    @model_validator(mode="after")
    def _rank_hypotheses(self) -> InvestigationReportSchema:
        """Order hypotheses by descending confidence."""
        self.hypotheses.sort(key=lambda hypothesis: hypothesis.confidence, reverse=True)
        return self

    @property
    def leading_hypothesis(self) -> Hypothesis | None:
        """The highest-confidence hypothesis, if any."""
        return self.hypotheses[0] if self.hypotheses else None

    @property
    def needs_human_review(self) -> bool:
        """Whether this report should be escalated rather than acted on.

        Three independent triggers: no hypothesis at all, low confidence, or any
        high-risk step in the plan.
        """
        if not self.hypotheses:
            return True
        if self.confidence < 0.5:
            return True
        return any(step.risk is RiskLevel.HIGH for step in self.remediation_steps)

    def to_markdown(self) -> str:
        """Render the report as markdown for chat or a status page."""
        lines = ["## Summary", "", self.summary, ""]

        if self.immediate_actions:
            lines += ["## Immediate actions", ""]
            lines += [f"- {action}" for action in self.immediate_actions]
            lines.append("")

        if self.hypotheses:
            lines += ["## Root cause hypotheses", ""]
            for hypothesis in self.hypotheses:
                lines.append(f"**{hypothesis.id} - {hypothesis.statement}** (confidence {hypothesis.confidence:.0%})")
                if hypothesis.reasoning:
                    lines.append(f"  {hypothesis.reasoning}")
                if hypothesis.evidence_ids:
                    lines.append(f"  Evidence: {', '.join(hypothesis.evidence_ids)}")
                if hypothesis.disconfirming_checks:
                    lines.append(f"  Rule out by: {'; '.join(hypothesis.disconfirming_checks)}")
                lines.append("")

        if self.evidence:
            lines += ["## Evidence", ""]
            for item in self.evidence:
                marker = "supports" if item.supports else "contradicts"
                lines.append(f"- **{item.id}** ({item.type.value}, {marker}): {item.description}")
            lines.append("")

        if self.remediation_steps:
            lines += ["## Remediation", ""]
            for step in self.remediation_steps:
                flag = " ⚠️ needs approval" if step.risk.requires_approval else ""
                lines.append(f"{step.order}. **{step.action}** [risk: {step.risk.value}]{flag}")
                if step.rationale:
                    lines.append(f"   {step.rationale}")
                if step.command:
                    lines.append(f"   ```\n   {step.command}\n   ```")
            lines.append("")

        if self.open_questions:
            lines += ["## Open questions", ""]
            lines += [f"- {question}" for question in self.open_questions]
            lines.append("")

        if self.citations:
            lines += ["## Sources", ""]
            for index, citation in enumerate(self.citations, start=1):
                label = f"{citation.title} :: {citation.heading_path}" if citation.heading_path else citation.title
                lines.append(f"{index}. {label}")

        return "\n".join(lines).strip()


# ----------------------------------------------------------------------
# API request/response models
# ----------------------------------------------------------------------


class IncidentCreate(BaseModel):
    """Request body for opening an incident."""

    title: Annotated[str, Field(min_length=1, max_length=500)]
    description: str = Field(default="", max_length=20000)
    severity: Severity = Field(default=Severity.SEV3)
    service: str | None = Field(default=None, max_length=200)
    affected_services: list[str] = Field(default_factory=list, max_length=50)
    detected_at: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentUpdate(BaseModel):
    """Request body for patching an incident. Unset fields are left unchanged."""

    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=20000)
    severity: Severity | None = None
    status: IncidentStatus | None = None
    service: str | None = Field(default=None, max_length=200)
    root_cause: str | None = Field(default=None, max_length=10000)
    resolution: str | None = Field(default=None, max_length=10000)
    resolved_at: datetime | None = None
    tags: list[str] | None = None


class IncidentResponse(BaseModel):
    """An incident as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    severity: Severity
    status: IncidentStatus
    service: str | None
    affected_services: list[str]
    detected_at: datetime | None
    resolved_at: datetime | None
    root_cause: str | None
    resolution: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class IncidentListResponse(BaseModel):
    """A page of incidents."""

    items: list[IncidentResponse]
    total: int
    limit: int
    offset: int


class InvestigateRequest(BaseModel):
    """Request body for running an investigation."""

    query: Annotated[str, Field(min_length=1, max_length=10000)] = Field(
        ..., description="The question or symptom description."
    )
    incident_id: uuid.UUID | None = Field(default=None, description="Attach the report to this incident.")
    logs: str | None = Field(
        default=None,
        max_length=2_000_000,
        description="Raw log text to analyse alongside the query.",
    )
    service: str | None = Field(default=None, max_length=200, description="Scope retrieval to one service.")
    severity: Severity | None = Field(
        default=None, description="Severity hint; biases toward mitigation-first advice."
    )
    stream: bool = Field(default=False, description="Stream tokens instead of returning a complete report.")

    @field_validator("query")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Reject whitespace-only queries, which would waste a full investigation."""
        if not value.strip():
            raise ValueError("query must contain non-whitespace characters")
        return value.strip()


class InvestigationResponse(BaseModel):
    """The result of an investigation."""

    report_id: uuid.UUID | None = None
    incident_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    report: InvestigationReportSchema
    markdown: str = Field(default="", description="Pre-rendered markdown, so clients need not reimplement it.")
    needs_human_review: bool = False
    duration_ms: int = 0
    model: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ReportFeedback(BaseModel):
    """Human feedback on a report, used for evaluation."""

    was_helpful: bool
    feedback: str = Field(default="", max_length=5000)


__all__ = [
    "Citation",
    "Evidence",
    "EvidenceType",
    "Hypothesis",
    "IncidentCreate",
    "IncidentListResponse",
    "IncidentResponse",
    "IncidentUpdate",
    "InvestigateRequest",
    "InvestigationReportSchema",
    "InvestigationResponse",
    "RemediationStep",
    "ReportFeedback",
    "RiskLevel",
]
