"""Knowledge base tools: runbook search and past-incident lookup.

These are what ground the agent. Without them it can only produce plausible
generic advice; with them it can quote the specific runbook section that covers
the failure in front of it.

Every tool here follows three rules that matter for agent reliability:

1. **Never raise.** A tool exception aborts the graph. Failures are returned as
   text the model can read and react to, so a retrieval outage degrades the
   investigation instead of ending it.
2. **Return text, not objects.** The result becomes a ``ToolMessage`` in the
   conversation, so it must be self-describing prose the model can reason over.
3. **Say when nothing was found, explicitly.** An empty result rendered as an
   empty string invites the model to fill the silence with invention. "No
   runbook covers this" is a useful, actionable finding.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import (
    Annotated,
    Any,
)

from langchain_core.tools import tool

from aegis.core.logging import logger
from aegis.models.knowledge import SourceType
from aegis.retrieval.hybrid import (
    HybridRetriever,
    RetrievalRequest,
)
from aegis.services.database import (
    IncidentRepository,
    session_scope,
)

# Citations accumulated by retrieval tools during one investigation, so the
# graph can attach machine-readable provenance to the final report. The model
# only ever sees the text rendering.
#
# A ContextVar rather than a module global: two investigations running
# concurrently in the same worker would otherwise interleave their citations
# into one list, and each report would cite the other's sources. ContextVars are
# scoped per asyncio task and propagate into child tasks, which matches the
# lifetime of a single investigation exactly.
#
# The default is an immutable empty tuple, not a list. A ContextVar's default
# object is shared by every context that never called ``set()``, so a mutable
# default could accumulate entries visible to unrelated tasks. A tuple makes
# that mistake impossible to write.
_citations: ContextVar[tuple[dict[str, Any], ...]] = ContextVar("aegis_tool_citations", default=())


def _record_citations(new_citations: list[dict[str, Any]]) -> None:
    """Append citations to the current task's buffer."""
    _citations.set((*_citations.get(), *new_citations))


def reset_citations() -> None:
    """Start a fresh citation buffer for the current task."""
    _citations.set(())


def drain_citations() -> list[dict[str, Any]]:
    """Return the citations gathered in this context, then clear them.

    Deduplicates by chunk id: the same runbook passage is frequently returned by
    several searches within one investigation, and listing it three times in the
    report's sources would imply corroboration that does not exist.
    """
    collected = _citations.get()
    _citations.set(())

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for citation in collected:
        key = str(citation.get("chunk_id", ""))
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return unique


@tool
async def search_runbooks(
    query: Annotated[str, "What to look for, phrased as a symptom or question."],
    service: Annotated[str | None, "Restrict to one service. Use when the affected service is known."] = None,
    top_k: Annotated[int, "How many passages to return (1-15)."] = 6,
) -> str:
    """Search operational runbooks, architecture notes, and alert definitions.

    Use this first for any question about how a system behaves, how to diagnose
    a symptom, or what a specific alert means. Prefer specific symptom wording
    over generic phrasing: "checkout returns 503 with upstream connect timeout"
    retrieves far better than "payment problem".

    Returns numbered passages with their source, which you should cite by number.
    """
    top_k = max(1, min(15, top_k))
    try:
        async with session_scope() as session:
            retriever = HybridRetriever(session)
            response = await retriever.search(
                RetrievalRequest(
                    query=query,
                    top_k=top_k,
                    service=service,
                    source_types=[
                        SourceType.RUNBOOK,
                        SourceType.ARCHITECTURE,
                        SourceType.ALERT_DEFINITION,
                    ],
                )
            )

        _record_citations(response.citations())

        if response.is_empty:
            scope = f" for service '{service}'" if service else ""
            return (
                f"No runbook passages matched '{query}'{scope}. "
                "The knowledge base may not cover this failure mode. Do not invent a runbook; "
                "say plainly that no documented procedure was found and reason from first principles instead."
            )

        return (
            f"Found {len(response.results)} runbook passages for '{query}':\n\n{response.to_context(max_chars=12000)}"
        )

    except Exception as exc:
        logger.error("search_runbooks_tool_failed", query=query, error=str(exc), exc_info=True)
        return (
            f"Runbook search failed: {exc}. Proceed using the evidence you already have "
            "and note in your report that the knowledge base was unavailable."
        )


@tool
async def search_postmortems(
    query: Annotated[str, "Symptom or failure description to match against past incidents."],
    service: Annotated[str | None, "Restrict to one service."] = None,
    top_k: Annotated[int, "How many passages to return (1-10)."] = 5,
) -> str:
    """Search written postmortems from previous incidents.

    Use this when a failure looks like it may have happened before. A matching
    postmortem is the strongest possible evidence, because it documents a cause
    that was actually confirmed rather than one that merely sounds plausible.
    """
    top_k = max(1, min(10, top_k))
    try:
        async with session_scope() as session:
            retriever = HybridRetriever(session)
            response = await retriever.search(
                RetrievalRequest(
                    query=query,
                    top_k=top_k,
                    service=service,
                    source_types=[SourceType.POSTMORTEM],
                )
            )

        _record_citations(response.citations())

        if response.is_empty:
            return f"No postmortems matched '{query}'. This failure mode has no written precedent in the corpus."

        return (
            f"Found {len(response.results)} postmortem passages for '{query}':\n\n"
            f"{response.to_context(max_chars=10000)}"
        )

    except Exception as exc:
        logger.error("search_postmortems_tool_failed", query=query, error=str(exc), exc_info=True)
        return f"Postmortem search failed: {exc}. Continue without this source."


@tool
async def find_similar_incidents(
    service: Annotated[str | None, "Service to look up resolved incidents for."] = None,
    limit: Annotated[int, "How many incidents to return (1-10)."] = 5,
) -> str:
    """List recently resolved incidents that have a human-confirmed root cause.

    Use this to check whether the current symptoms match something the team has
    already diagnosed. These records carry ground truth - a human wrote down what
    the cause actually turned out to be - so they outweigh inference from logs.
    """
    limit = max(1, min(10, limit))
    try:
        async with session_scope() as session:
            repository = IncidentRepository(session)
            incidents = await repository.resolved_incidents_for_context(service, limit=limit)

        if not incidents:
            scope = f" for service '{service}'" if service else ""
            return f"No resolved incidents with a recorded root cause were found{scope}."

        blocks: list[str] = []
        for incident in incidents:
            detail = [
                f"### {incident.title} ({incident.severity.value.upper()})",
                f"Service: {incident.service or 'unspecified'}",
                f"Resolved: {incident.resolved_at.isoformat() if incident.resolved_at else 'unknown'}",
                f"Root cause: {incident.root_cause}",
            ]
            if incident.resolution:
                detail.append(f"Resolution: {incident.resolution}")
            if incident.duration_seconds:
                detail.append(f"Time to resolve: {incident.duration_seconds / 60:.0f} minutes")
            blocks.append("\n".join(detail))

        return f"{len(incidents)} previously resolved incidents:\n\n" + "\n\n".join(blocks)

    except Exception as exc:
        logger.error("find_similar_incidents_tool_failed", error=str(exc), exc_info=True)
        return f"Incident history lookup failed: {exc}. Continue without this source."


@tool
async def search_knowledge_base(
    query: Annotated[str, "What to look for."],
    service: Annotated[str | None, "Restrict to one service."] = None,
    top_k: Annotated[int, "How many passages to return (1-15)."] = 8,
) -> str:
    """Search the entire knowledge base without filtering by document type.

    Use this as a fallback when the type-specific searches come back empty, or
    when you do not yet know what kind of document would answer the question.
    """
    top_k = max(1, min(15, top_k))
    try:
        async with session_scope() as session:
            retriever = HybridRetriever(session)
            response = await retriever.search(RetrievalRequest(query=query, top_k=top_k, service=service))

        _record_citations(response.citations())

        if response.is_empty:
            return (
                f"Nothing in the knowledge base matched '{query}'. "
                "Do not fabricate documentation; state that the corpus does not cover this."
            )

        return f"Found {len(response.results)} passages:\n\n{response.to_context(max_chars=12000)}"

    except Exception as exc:
        logger.error("search_knowledge_base_tool_failed", query=query, error=str(exc), exc_info=True)
        return f"Knowledge base search failed: {exc}."


@tool
async def list_documented_services() -> str:
    """List the services that have documentation in the knowledge base.

    Use this when a query names a service you are unsure exists, or to check
    whether a coverage gap explains an empty search result.
    """
    try:
        from sqlalchemy import text as sql_text

        async with session_scope() as session:
            rows = (
                await session.execute(
                    sql_text(
                        """
                        SELECT service, count(*) AS documents
                        FROM documents
                        WHERE service IS NOT NULL
                        GROUP BY service
                        ORDER BY documents DESC
                        LIMIT 100
                        """
                    )
                )
            ).all()

        if not rows:
            return "No documents in the knowledge base are tagged with a service."

        listing = ", ".join(f"{row.service} ({row.documents} docs)" for row in rows)
        return f"Services with documentation: {listing}"

    except Exception as exc:
        logger.error("list_documented_services_tool_failed", error=str(exc), exc_info=True)
        return f"Service listing failed: {exc}."


__all__ = [
    "drain_citations",
    "find_similar_incidents",
    "list_documented_services",
    "search_knowledge_base",
    "search_postmortems",
    "search_runbooks",
]
