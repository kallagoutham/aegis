"""Incident records and the investigation reports the agent produces.

Separating ``Incident`` from ``InvestigationReport`` is the important modelling
decision here. An incident is a *fact about the world* - it happened, it had a
severity, it was eventually resolved. A report is *one machine-generated opinion
about that fact*, produced at a point in time from whatever evidence was
available then.

Keeping them apart buys three things:

* Multiple investigations of the same incident (as new logs arrive, the picture
  changes) without destroying earlier reasoning.
* An honest audit trail: what did the agent think at 03:14, before anyone had
  looked at the database metrics?
* An evaluation corpus. Once a human records the true root cause on the
  incident, every historical report becomes a labelled example of whether the
  agent was right - which is what ``evals/`` grades against.
"""

from datetime import datetime
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
)
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    String,
    Text,
)
from sqlmodel import (
    Field,
    Relationship,
)

from aegis.models.base import (
    AegisTable,
    empty_dict,
    jsonb_column,
)

if TYPE_CHECKING:
    from aegis.models.user import User


class Severity(str, Enum):
    """Incident severity, following the common SEV1-SEV4 convention.

    Severity drives agent behaviour, not just reporting: a SEV1 prompt biases
    toward immediate mitigation ("stop the bleeding"), while a SEV4 allows the
    agent to recommend deeper investigation before acting.
    """

    SEV1 = "sev1"  # Total outage or data loss. All hands.
    SEV2 = "sev2"  # Major functionality broken for many users.
    SEV3 = "sev3"  # Degraded or partial impact with a workaround.
    SEV4 = "sev4"  # Minor issue, no customer impact.

    @property
    def is_critical(self) -> bool:
        """Whether this severity warrants mitigation-first guidance."""
        return self in (Severity.SEV1, Severity.SEV2)

    @property
    def rank(self) -> int:
        """Numeric rank where 1 is most severe. Useful for sorting."""
        return int(self.value[-1])


class IncidentStatus(str, Enum):
    """Where an incident sits in its lifecycle."""

    OPEN = "open"
    INVESTIGATING = "investigating"
    IDENTIFIED = "identified"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    CLOSED = "closed"

    @property
    def is_terminal(self) -> bool:
        """Whether the incident needs no further agent attention."""
        return self in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)


class Incident(AegisTable, table=True):
    """A production incident under investigation.

    ``root_cause`` and ``resolution`` are filled in by a *human* after the fact.
    They are the ground truth the agent is graded against, which is why they are
    nullable and never written by the agent itself.
    """

    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_incidents_status_severity", "status", "severity"),
        Index("ix_incidents_service_detected", "service", "detected_at"),
    )

    title: str = Field(sa_column=Column(String(500), nullable=False))
    description: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    severity: Severity = Field(default=Severity.SEV3, index=True, nullable=False)
    status: IncidentStatus = Field(default=IncidentStatus.OPEN, index=True, nullable=False)

    service: str | None = Field(default=None, index=True, max_length=200, description="Primary affected service.")
    affected_services: list[str] = Field(
        default_factory=list,
        sa_column=Column(jsonb_column().type, nullable=False, server_default="[]"),
        description="Blast radius, including downstream dependencies.",
    )

    detected_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
        description="When the problem started, which is often earlier than when it was reported.",
    )
    resolved_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))

    reporter_id: uuid.UUID | None = Field(default=None, foreign_key="users.id", index=True, ondelete="SET NULL")

    # Human-authored ground truth, used to score the agent retrospectively.
    root_cause: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    resolution: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    tags: list[str] = Field(
        default_factory=list,
        sa_column=Column(jsonb_column().type, nullable=False, server_default="[]"),
    )
    incident_metadata: dict[str, Any] = Field(
        default_factory=empty_dict,
        sa_column=jsonb_column(),
        description="Links out to PagerDuty, Slack channels, dashboards.",
    )

    reporter: "User" = Relationship(back_populates="incidents")
    reports: list["InvestigationReport"] = Relationship(
        back_populates="incident",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "lazy": "selectin"},
    )

    @property
    def duration_seconds(self) -> float | None:
        """Time to resolution in seconds, or ``None`` while still open."""
        if self.detected_at and self.resolved_at:
            return (self.resolved_at - self.detected_at).total_seconds()
        return None


class InvestigationReport(AegisTable, table=True):
    """One agent-produced analysis of an incident.

    The structured fields (``hypotheses``, ``evidence``, ``remediation_steps``)
    are stored as JSONB rather than being normalised into child tables. That is
    a deliberate trade: these documents are always read as a whole, never queried
    field-by-field, and their shape is set by the LLM output schema in
    :mod:`aegis.schemas.incident` - which will keep evolving. JSONB lets the
    schema move without a migration for every prompt revision.
    """

    __tablename__ = "investigation_reports"
    __table_args__ = (Index("ix_reports_incident_created", "incident_id", "created_at"),)

    incident_id: uuid.UUID | None = Field(default=None, foreign_key="incidents.id", index=True, ondelete="CASCADE")
    session_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="sessions.id",
        index=True,
        ondelete="SET NULL",
        description="Conversation that produced this report, for replay and debugging.",
    )

    summary: str = Field(sa_column=Column(Text, nullable=False), description="Executive summary for a status update.")
    hypotheses: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(jsonb_column().type, nullable=False, server_default="[]"),
        description="Ranked root cause candidates, each with confidence and supporting evidence.",
    )
    evidence: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(jsonb_column().type, nullable=False, server_default="[]"),
        description="Concrete observations cited by the hypotheses.",
    )
    remediation_steps: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(jsonb_column().type, nullable=False, server_default="[]"),
        description="Ordered actions, each flagged with its risk level.",
    )
    citations: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(jsonb_column().type, nullable=False, server_default="[]"),
        description="Knowledge base chunks the report drew on; makes claims auditable.",
    )

    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        index=True,
        description="Confidence in the leading hypothesis. Low values should route to a human.",
    )
    model: str = Field(default="", max_length=100, description="Model that generated the report.")
    tokens_used: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(jsonb_column().type, nullable=False, server_default="[]"),
        description="Trace of tools invoked, for debugging a bad conclusion.",
    )

    # Human feedback, which turns this row into a labelled training example.
    was_helpful: bool | None = Field(default=None, index=True)
    feedback: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    incident: Incident = Relationship(back_populates="reports")

    @property
    def leading_hypothesis(self) -> dict[str, Any] | None:
        """The highest-confidence root cause candidate, if any."""
        if not self.hypotheses:
            return None
        return max(self.hypotheses, key=lambda h: h.get("confidence", 0.0))


__all__ = [
    "Incident",
    "IncidentStatus",
    "InvestigationReport",
    "Severity",
]
