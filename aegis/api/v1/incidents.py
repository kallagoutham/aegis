"""Incident management and the investigation endpoint.

``POST /incidents/investigate`` is the product's centre of gravity: it runs the
full agent workflow and returns a structured, cited report.
"""

from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Request,
    status,
)

from aegis.api.deps import (
    CurrentUser,
    DbSession,
    PaginationParams,
    ResponderUser,
)
from aegis.core.config import settings
from aegis.core.exceptions import ResourceNotFoundError
from aegis.core.langgraph.graph import get_agent
from aegis.core.limiter import limiter
from aegis.core.logging import logger
from aegis.models.incident import (
    IncidentStatus,
    InvestigationReport,
)
from aegis.models.user import Session
from aegis.schemas.incident import (
    IncidentCreate,
    IncidentListResponse,
    IncidentResponse,
    IncidentUpdate,
    InvestigateRequest,
    InvestigationReportSchema,
    InvestigationResponse,
    ReportFeedback,
)
from aegis.services.database import (
    IncidentRepository,
    UserRepository,
)

router = APIRouter()


@router.post(
    "",
    response_model=IncidentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open an incident",
)
async def create_incident(
    payload: IncidentCreate,
    user: ResponderUser,
    session: DbSession,
) -> IncidentResponse:
    """Record a new incident."""
    repository = IncidentRepository(session)
    incident = await repository.create_incident(
        title=payload.title,
        description=payload.description,
        severity=payload.severity,
        service=payload.service,
        affected_services=payload.affected_services,
        detected_at=payload.detected_at,
        tags=payload.tags,
        incident_metadata=payload.metadata,
        reporter_id=user.id,
    )
    await session.commit()
    return IncidentResponse.model_validate(incident)


@router.get("", response_model=IncidentListResponse, summary="List incidents")
async def list_incidents(
    user: CurrentUser,
    session: DbSession,
    pagination: PaginationParams,
    incident_status: IncidentStatus | None = None,
    service: str | None = None,
) -> IncidentListResponse:
    """List incidents with optional status and service filters."""
    repository = IncidentRepository(session)
    rows, total = await repository.list_incidents(
        status=incident_status,
        service=service,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return IncidentListResponse(
        items=[IncidentResponse.model_validate(row) for row in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{incident_id}", response_model=IncidentResponse, summary="Read one incident")
async def read_incident(incident_id: uuid.UUID, user: CurrentUser, session: DbSession) -> IncidentResponse:
    """Return a single incident."""
    repository = IncidentRepository(session)
    incident = await repository.require_incident(incident_id)
    return IncidentResponse.model_validate(incident)


@router.patch("/{incident_id}", response_model=IncidentResponse, summary="Update an incident")
async def update_incident(
    incident_id: uuid.UUID,
    payload: IncidentUpdate,
    user: ResponderUser,
    session: DbSession,
) -> IncidentResponse:
    """Patch an incident.

    Recording ``root_cause`` here is what turns the incident into ground truth
    for evaluation: every report previously generated for it becomes a labelled
    example of whether the agent was right.
    """
    repository = IncidentRepository(session)
    incident = await repository.require_incident(incident_id)

    # exclude_unset so an omitted field is left alone rather than nulled.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(incident, field, value)

    # Stamp resolution time automatically when an incident closes without one,
    # so time-to-resolve stays computable even if the caller forgets.
    if incident.status.is_terminal and incident.resolved_at is None:
        from aegis.models.base import utcnow

        incident.resolved_at = utcnow()

    session.add(incident)
    await session.commit()

    logger.info("incident_updated", incident_id=str(incident_id), status=incident.status.value)
    return IncidentResponse.model_validate(incident)


@router.post(
    "/investigate",
    response_model=InvestigationResponse,
    summary="Run an investigation",
)
@limiter.limit(settings.rate_limit_for("investigate"))
async def investigate(
    request: Request,
    payload: InvestigateRequest,
    user: ResponderUser,
    session: DbSession,
) -> InvestigationResponse:
    """Run the full agent workflow and return a structured report.

    Creates a dedicated session so the investigation has its own checkpointed
    thread, which makes it independently replayable and keeps it from polluting
    an ongoing chat.
    """
    user_repository = UserRepository(session)
    investigation_session: Session = await user_repository.create_session(user.id, title=payload.query[:100])
    await session.commit()

    logger.info(
        "investigation_requested",
        session_id=str(investigation_session.id),
        incident_id=str(payload.incident_id) if payload.incident_id else None,
        has_logs=bool(payload.logs),
        service=payload.service,
    )

    incident_repository = IncidentRepository(session)
    if payload.incident_id is not None:
        # Validate up front so a bad id fails before the expensive agent run.
        await incident_repository.require_incident(payload.incident_id)

    agent = get_agent()
    result = await agent.investigate(
        payload.query,
        session_id=investigation_session.id,
        user_id=user.id,
        logs=payload.logs,
        service=payload.service,
        severity=payload.severity,
        incident_id=payload.incident_id,
    )

    report_schema = InvestigationReportSchema.model_validate(result["report"])

    stored = InvestigationReport(
        incident_id=payload.incident_id,
        session_id=investigation_session.id,
        summary=report_schema.summary,
        hypotheses=[item.model_dump(mode="json") for item in report_schema.hypotheses],
        evidence=[item.model_dump(mode="json") for item in report_schema.evidence],
        remediation_steps=[item.model_dump(mode="json") for item in report_schema.remediation_steps],
        citations=[item.model_dump(mode="json") for item in report_schema.citations],
        confidence=report_schema.confidence,
        model=result.get("model", ""),
        duration_ms=result.get("duration_ms", 0),
        tool_calls=result.get("tool_calls", []),
    )
    await incident_repository.save_report(stored)

    # Move the incident into "investigating" if it is still untouched, so the
    # status board reflects that work has started.
    if payload.incident_id is not None:
        incident = await incident_repository.require_incident(payload.incident_id)
        if incident.status is IncidentStatus.OPEN:
            incident.status = IncidentStatus.INVESTIGATING
            session.add(incident)

    await session.commit()

    return InvestigationResponse(
        report_id=stored.id,
        incident_id=payload.incident_id,
        session_id=investigation_session.id,
        report=report_schema,
        markdown=report_schema.to_markdown(),
        needs_human_review=report_schema.needs_human_review,
        duration_ms=result.get("duration_ms", 0),
        model=result.get("model", ""),
        tool_calls=result.get("tool_calls", []),
    )


@router.get("/{incident_id}/reports", summary="List an incident's reports")
async def list_reports(
    incident_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
) -> list[dict]:
    """Return every investigation report for an incident, newest first."""
    repository = IncidentRepository(session)
    await repository.require_incident(incident_id)
    reports = await repository.list_reports(incident_id)

    return [
        {
            "id": str(report.id),
            "summary": report.summary,
            "confidence": report.confidence,
            "hypotheses": report.hypotheses,
            "remediation_steps": report.remediation_steps,
            "citations": report.citations,
            "model": report.model,
            "duration_ms": report.duration_ms,
            "was_helpful": report.was_helpful,
            "created_at": report.created_at.isoformat(),
        }
        for report in reports
    ]


@router.post(
    "/reports/{report_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record feedback on a report",
)
async def submit_feedback(
    report_id: uuid.UUID,
    payload: ReportFeedback,
    user: CurrentUser,
    session: DbSession,
) -> None:
    """Attach human feedback to a report.

    This is the cheapest high-quality evaluation signal available - a responder
    marking a report unhelpful, in the moment, is worth more than a synthetic
    benchmark.
    """
    report = await session.get(InvestigationReport, report_id)
    if report is None:
        raise ResourceNotFoundError("InvestigationReport", report_id)

    report.was_helpful = payload.was_helpful
    report.feedback = payload.feedback
    session.add(report)
    await session.commit()

    logger.info("report_feedback_recorded", report_id=str(report_id), was_helpful=payload.was_helpful)


__all__ = ["router"]
