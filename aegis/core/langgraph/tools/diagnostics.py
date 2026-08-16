"""Diagnostic tools: log analysis and general web search.

:func:`analyze_log_excerpt` is the tool the agent reaches for when a user pastes
logs mid-conversation. The bulk log analysis attached to an investigation is
handled up front by the ``analyze`` graph node; this covers the follow-up case
("here's another 200 lines from the replica").

:func:`web_search` is deliberately last-resort. Public search results are not
authoritative about a private production system, and an agent that reaches for
them early produces generic StackOverflow advice instead of reading the runbook
sitting in the knowledge base. The docstring says so, because the docstring is
what the model actually reads when choosing a tool.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool

from aegis.analysis import analyse_logs
from aegis.core.config import settings
from aegis.core.logging import logger


@tool
async def analyze_log_excerpt(
    logs: Annotated[str, "Raw log text of any format: JSON lines, logfmt, or plain text."],
    max_lines: Annotated[int, "Maximum lines to parse (1-20000)."] = 5000,
) -> str:
    """Parse and summarise a block of logs.

    Extracts message templates, counts occurrences by severity, establishes the
    time range, and flags anomalies such as error bursts or retry storms.

    Use this whenever raw logs appear in the conversation. Reading the summary
    is strictly better than reading the raw lines: it surfaces the highest-volume
    error patterns and their onset time, which raw text buries.
    """
    max_lines = max(1, min(20_000, max_lines))
    try:
        analysis = analyse_logs(logs, max_lines=max_lines)

        if analysis.total_entries == 0:
            return "No parseable log entries were found in the supplied text."

        return analysis.to_prompt_summary()

    except Exception as exc:
        logger.error("analyze_log_excerpt_tool_failed", error=str(exc), exc_info=True)
        return f"Log analysis failed: {exc}. Read the raw lines directly instead."


@tool
async def extract_error_signatures(
    logs: Annotated[str, "Raw log text to scan."],
    top_n: Annotated[int, "How many distinct error templates to return (1-25)."] = 10,
) -> str:
    """Extract the most frequent distinct error signatures from logs.

    Returns only warning-or-worse templates with their counts and first-seen
    times. Narrower than ``analyze_log_excerpt``; use it when you already know
    the logs contain errors and want the shortlist of what to search runbooks
    for.
    """
    top_n = max(1, min(25, top_n))
    try:
        analysis = analyse_logs(logs)

        if not analysis.error_clusters:
            return (
                f"No warning or error entries found across {analysis.total_entries} parsed lines. "
                "The failure may not be logging at an error level - consider that the symptom "
                "is a silent one such as latency, saturation, or a wrong-but-successful response."
            )

        lines = [f"{len(analysis.error_clusters)} distinct error signatures:"]
        for index, cluster in enumerate(analysis.error_clusters[:top_n], start=1):
            entry = f"{index}. [{cluster.level.value.upper()}] x{cluster.count} - {cluster.template}"
            if cluster.first_seen:
                entry += f" (first seen {cluster.first_seen.isoformat()})"
            if cluster.services:
                entry += f" [services: {', '.join(sorted(cluster.services)[:4])}]"
            lines.append(entry)

        if analysis.anomalies:
            lines.append("")
            lines.append("Anomalies:")
            lines.extend(f"- {anomaly}" for anomaly in analysis.anomalies)

        return "\n".join(lines)

    except Exception as exc:
        logger.error("extract_error_signatures_tool_failed", error=str(exc), exc_info=True)
        return f"Error signature extraction failed: {exc}."


@tool
async def web_search(
    query: Annotated[str, "Search query. Include the exact error string or library name."],
    max_results: Annotated[int, "How many results to return (1-10)."] = 5,
) -> str:
    """Search the public web for information about an error or technology.

    Use this ONLY after the knowledge base has been searched and came back
    without an answer, and only for questions about third-party software - a
    library's error message, a known bug in a database version, upstream
    release notes.

    Public results know nothing about this organisation's architecture,
    configuration, or deployment. Never treat a web result as evidence about
    what this system is doing; treat it as background about what a component is
    capable of doing.
    """
    max_results = max(1, min(10, max_results))
    try:
        from langchain_community.tools import DuckDuckGoSearchResults

        searcher = DuckDuckGoSearchResults(num_results=max_results, handle_tool_error=True)
        # The community wrapper is synchronous under the hood; ainvoke offloads
        # it to a thread so the event loop is not blocked for the round trip.
        results = await searcher.ainvoke(query)

        if not results:
            return f"No web results found for '{query}'."

        return f"Public web results for '{query}' (background only - not evidence about this system):\n\n{results}"

    except Exception as exc:
        logger.error("web_search_tool_failed", query=query, error=str(exc), exc_info=True)
        return f"Web search failed: {exc}. Continue without it."


@tool
async def compute_incident_timeline(
    logs: Annotated[str, "Raw log text to derive a timeline from."],
    bucket_count: Annotated[int, "Number of time buckets (5-50)."] = 20,
) -> str:
    """Build a time-bucketed view of log volume and error rate.

    Use this to establish *when* a problem started, which is often the single
    most diagnostic fact available - the onset time is what you correlate
    against deploys, config changes, and traffic shifts.
    """
    bucket_count = max(5, min(50, bucket_count))
    try:
        analysis = analyse_logs(logs, max_lines=settings.AGENT_MAX_LOG_LINES)

        if not analysis.buckets:
            return (
                "Could not build a timeline: the logs contain too few parseable timestamps. "
                "Check whether the log format includes timestamps at all."
            )

        start, end = analysis.time_range
        lines = [
            f"Timeline across {bucket_count} buckets "
            f"({start.isoformat() if start else '?'} to {end.isoformat() if end else '?'}):",
            "",
        ]

        peak = max(bucket.total for bucket in analysis.buckets) or 1
        for bucket in analysis.buckets:
            # A fixed-width bar makes the shape of the incident legible at a
            # glance, which prose counts do not.
            filled = int((bucket.total / peak) * 30)
            bar = "#" * filled + "." * (30 - filled)
            lines.append(
                f"{bucket.start.strftime('%H:%M:%S')} |{bar}| {bucket.total:5d} total, {bucket.errors:5d} errors"
            )

        if analysis.anomalies:
            lines.append("")
            lines.extend(f"- {anomaly}" for anomaly in analysis.anomalies)

        return "\n".join(lines)

    except Exception as exc:
        logger.error("compute_incident_timeline_tool_failed", error=str(exc), exc_info=True)
        return f"Timeline computation failed: {exc}."


__all__ = [
    "analyze_log_excerpt",
    "compute_incident_timeline",
    "extract_error_signatures",
    "web_search",
]
