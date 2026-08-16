"""Log parsing and analysis.

Public entry point is :func:`analyse_logs`, which turns raw log text into a
structured :class:`~aegis.analysis.clustering.LogAnalysis`.
"""

from __future__ import annotations

from aegis.analysis.clustering import (
    LogAnalysis,
    LogCluster,
    TimeBucket,
    analyse_entries,
    bucket_entries,
    cluster_entries,
    detect_anomalies,
    extract_template,
)
from aegis.analysis.parser import (
    LogEntry,
    LogLevel,
    parse_line,
    parse_logs,
    parse_timestamp,
)
from aegis.core.config import settings


def analyse_logs(text: str, *, max_lines: int | None = None) -> LogAnalysis:
    """Parse and analyse a raw log bundle in one step.

    Args:
        text: Raw log text of any supported format.
        max_lines: Cap on lines parsed; defaults to ``AGENT_MAX_LOG_LINES``.
            The cap exists so a pasted 500 MB log cannot exhaust worker memory.

    Returns:
        A structured analysis suitable for prompting or API response.
    """
    limit = max_lines if max_lines is not None else settings.AGENT_MAX_LOG_LINES
    total_lines = text.count("\n") + 1
    entries = parse_logs(text, max_lines=limit)
    return analyse_entries(entries, truncated=total_lines > limit)


__all__ = [
    "LogAnalysis",
    "LogCluster",
    "LogEntry",
    "LogLevel",
    "TimeBucket",
    "analyse_entries",
    "analyse_logs",
    "bucket_entries",
    "cluster_entries",
    "detect_anomalies",
    "extract_template",
    "parse_line",
    "parse_logs",
    "parse_timestamp",
]
