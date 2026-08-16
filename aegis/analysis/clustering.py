"""Log template extraction, clustering, and anomaly detection.

A 10,000-line log bundle does not fit in a context window, and pasting the first
2,000 lines usually shows the model the least interesting part of the incident.
Summarisation is not optional here - it is what makes log analysis tractable.

The key observation is that production logs are highly repetitive. Ten thousand
lines are typically a few dozen *templates* with variable parts substituted::

    Connection to db-primary-7f3a timed out after 30000ms
    Connection to db-primary-9c21 timed out after 30000ms
    Connection to db-replica-2b8e timed out after 5000ms

all reduce to::

    Connection to <ID> timed out after <NUM>ms   (x3)

Collapsing to templates compresses by one to three orders of magnitude while
*increasing* signal: a template with a count of 4,812 that first appeared at
10:23 is a far stronger lead than any individual line.

The approach is a simplified `Drain <https://github.com/logpai/Drain3>`_:
mask known-variable token classes with regexes, then group by the masked form.
Full Drain builds a parse tree to discover variable positions without prior
knowledge. That is more general, but the token classes that actually vary in
production logs (ids, IPs, durations, paths) are well known, and masking them
directly is faster, deterministic, and needs no training corpus.
"""

from __future__ import annotations

from collections import (
    Counter,
    defaultdict,
)
from collections.abc import Sequence
from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    datetime,
    timedelta,
)
import re

from aegis.analysis.parser import (
    LogEntry,
    LogLevel,
)

# Token classes replaced with placeholders, ordered most specific first. Order
# matters: a UUID would otherwise be partly consumed by the hex-number rule.
_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TIMESTAMP>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b[a-fA-F0-9]{2}(?::[a-fA-F0-9]{2}){5}\b"), "<MAC>"),
    (re.compile(r"\bhttps?://[^\s\"'<>]+"), "<URL>"),
    (re.compile(r"\b[\w.\-]+@[\w.\-]+\.\w+\b"), "<EMAIL>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<HASH>"),
    # Identifier-looking tokens: a word containing both letters and digits, or
    # separator-joined segments ending in digits (pod-7f3a, worker_12).
    (re.compile(r"\b[a-zA-Z][\w]*[-_][a-zA-Z0-9]*\d[\w-]*\b"), "<ID>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|us|ns|m|h|kb|mb|gb|KB|MB|GB)\b"), "<DURATION>"),
    (re.compile(r"(?<![\w.])\d+\.\d+(?![\w.])"), "<FLOAT>"),
    (re.compile(r"(?<![\w.<])\d+(?![\w.>])"), "<NUM>"),
    (re.compile(r"(?:/[\w.\-]+){2,}/?"), "<PATH>"),
    (re.compile(r"'[^']*'"), "<STR>"),
    (re.compile(r'"[^"]*"'), "<STR>"),
)

_WHITESPACE_RE = re.compile(r"\s+")


def extract_template(message: str) -> str:
    """Reduce a log message to its template by masking variable tokens.

    Args:
        message: The log message body.

    Returns:
        The masked template, e.g.
        ``"Connection to <ID> timed out after <DURATION>"``.
    """
    # Only the first line: a stack trace's frames are variable detail whose
    # inclusion would make every trace its own template.
    template = message.split("\n", 1)[0]
    for pattern, placeholder in _MASKS:
        template = pattern.sub(placeholder, template)
    return _WHITESPACE_RE.sub(" ", template).strip()


@dataclass(slots=True)
class LogCluster:
    """A group of log entries sharing one template."""

    template: str
    count: int = 0
    level: LogLevel = LogLevel.UNKNOWN
    services: set[str] = field(default_factory=set)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    examples: list[str] = field(default_factory=list)
    line_numbers: list[int] = field(default_factory=list)

    def add(self, entry: LogEntry, max_examples: int = 3) -> None:
        """Fold an entry into this cluster."""
        self.count += 1

        # Keep the highest severity seen. A template appearing at both INFO and
        # ERROR should be surfaced at ERROR.
        if entry.level.rank > self.level.rank:
            self.level = entry.level

        if entry.service:
            self.services.add(entry.service)

        if entry.timestamp:
            if self.first_seen is None or entry.timestamp < self.first_seen:
                self.first_seen = entry.timestamp
            if self.last_seen is None or entry.timestamp > self.last_seen:
                self.last_seen = entry.timestamp

        if len(self.examples) < max_examples:
            self.examples.append(entry.raw[:500])
        if len(self.line_numbers) < 20:
            self.line_numbers.append(entry.line_number)

    @property
    def duration_seconds(self) -> float | None:
        """Time span between the first and last occurrence."""
        if self.first_seen and self.last_seen:
            return (self.last_seen - self.first_seen).total_seconds()
        return None

    @property
    def rate_per_minute(self) -> float | None:
        """Occurrences per minute across the cluster's span."""
        duration = self.duration_seconds
        if duration is None or duration <= 0:
            return None
        return self.count / (duration / 60)

    def to_dict(self) -> dict[str, object]:
        """Serialise for prompts and API responses."""
        return {
            "template": self.template,
            "count": self.count,
            "level": self.level.value,
            "services": sorted(self.services),
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "rate_per_minute": round(self.rate_per_minute, 2) if self.rate_per_minute else None,
            "example": self.examples[0] if self.examples else "",
            "line_numbers": self.line_numbers[:5],
        }


@dataclass(slots=True)
class TimeBucket:
    """Entry counts within one time window, used for burst detection."""

    start: datetime
    total: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, object]:
        """Serialise for the analysis payload."""
        return {"start": self.start.isoformat(), "total": self.total, "errors": self.errors}


@dataclass(slots=True)
class LogAnalysis:
    """Structured summary of a log bundle.

    This is what gets handed to the model instead of the raw text. It is
    deliberately dense: counts, templates, time ranges, and a handful of verbatim
    examples, rather than thousands of near-identical lines.
    """

    total_entries: int
    level_counts: dict[str, int]
    clusters: list[LogCluster]
    error_clusters: list[LogCluster]
    services: list[str]
    time_range: tuple[datetime | None, datetime | None]
    buckets: list[TimeBucket] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def error_count(self) -> int:
        """Total warning-or-worse entries."""
        return sum(
            count
            for level, count in self.level_counts.items()
            if level in (LogLevel.WARNING.value, LogLevel.ERROR.value, LogLevel.CRITICAL.value)
        )

    @property
    def error_rate(self) -> float:
        """Fraction of entries that are warnings or worse."""
        return self.error_count / self.total_entries if self.total_entries else 0.0

    def to_dict(self) -> dict[str, object]:
        """Serialise the full analysis."""
        start, end = self.time_range
        return {
            "total_entries": self.total_entries,
            "level_counts": self.level_counts,
            "error_count": self.error_count,
            "error_rate": round(self.error_rate, 4),
            "services": self.services,
            "formats": self.formats,
            "time_range": {
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "duration_seconds": (end - start).total_seconds() if start and end else None,
            },
            "distinct_templates": len(self.clusters),
            "top_error_templates": [cluster.to_dict() for cluster in self.error_clusters[:10]],
            "top_templates": [cluster.to_dict() for cluster in self.clusters[:10]],
            "anomalies": self.anomalies,
            "truncated": self.truncated,
        }

    def to_prompt_summary(self, max_clusters: int = 12) -> str:
        """Render the analysis as compact text for an LLM prompt.

        Errors are listed before general activity because that ordering matches
        how an engineer reads a log during an incident, and because the model
        weights earlier context more heavily.
        """
        start, end = self.time_range
        lines: list[str] = ["## Log analysis", ""]

        window = ""
        if start and end:
            window = f" spanning {start.isoformat()} to {end.isoformat()} ({(end - start).total_seconds():.0f}s)"
        lines.append(f"{self.total_entries} entries{window}.")
        lines.append(
            "Severity breakdown: "
            + ", ".join(f"{level}={count}" for level, count in sorted(self.level_counts.items()))
        )
        if self.services:
            lines.append(f"Services present: {', '.join(self.services[:15])}")
        lines.append(f"{len(self.clusters)} distinct message templates.")
        if self.truncated:
            lines.append("NOTE: input was truncated; counts are a lower bound.")
        lines.append("")

        if self.error_clusters:
            lines.append("### Error and warning templates (most frequent first)")
            for index, cluster in enumerate(self.error_clusters[:max_clusters], start=1):
                detail = f"{index}. [{cluster.level.value.upper()}] x{cluster.count} - {cluster.template}"
                if cluster.first_seen:
                    detail += f"\n   first: {cluster.first_seen.isoformat()}"
                if cluster.rate_per_minute:
                    detail += f", rate: {cluster.rate_per_minute:.1f}/min"
                if cluster.services:
                    detail += f", services: {', '.join(sorted(cluster.services)[:5])}"
                if cluster.examples:
                    detail += f"\n   example: {cluster.examples[0][:300]}"
                lines.append(detail)
            lines.append("")

        non_error = [cluster for cluster in self.clusters if not cluster.level.is_problem]
        if non_error:
            lines.append("### Other high-volume templates")
            for cluster in non_error[:5]:
                lines.append(f"- x{cluster.count} [{cluster.level.value}] {cluster.template}")
            lines.append("")

        if self.anomalies:
            lines.append("### Detected anomalies")
            lines.extend(f"- {anomaly}" for anomaly in self.anomalies)

        return "\n".join(lines)


def cluster_entries(entries: Sequence[LogEntry], max_examples: int = 3) -> list[LogCluster]:
    """Group entries by extracted template.

    Args:
        entries: Parsed log entries.
        max_examples: Verbatim examples retained per cluster.

    Returns:
        Clusters sorted by descending count.
    """
    clusters: dict[str, LogCluster] = {}
    for entry in entries:
        template = extract_template(entry.message or entry.raw)
        cluster = clusters.get(template)
        if cluster is None:
            cluster = LogCluster(template=template)
            clusters[template] = cluster
        cluster.add(entry, max_examples=max_examples)
    return sorted(clusters.values(), key=lambda cluster: cluster.count, reverse=True)


def bucket_entries(entries: Sequence[LogEntry], bucket_count: int = 20) -> list[TimeBucket]:
    """Distribute timestamped entries into equal time buckets.

    Bucket *width* is derived from the span so the resolution adapts: a
    five-minute capture gets fifteen-second buckets, a six-hour one gets
    eighteen-minute buckets. A fixed width would produce either one bucket or
    thousands depending on the input.

    Args:
        entries: Parsed entries.
        bucket_count: Number of buckets to produce.

    Returns:
        Buckets in chronological order, or an empty list when timestamps are
        absent or all identical.
    """
    timestamped = [entry for entry in entries if entry.timestamp]
    if len(timestamped) < 2:
        return []

    start = min(entry.timestamp for entry in timestamped)  # type: ignore[type-var]
    end = max(entry.timestamp for entry in timestamped)  # type: ignore[type-var]
    span = (end - start).total_seconds()
    if span <= 0:
        return []

    width = span / bucket_count
    buckets = [TimeBucket(start=start + timedelta(seconds=width * index)) for index in range(bucket_count)]

    for entry in timestamped:
        offset = (entry.timestamp - start).total_seconds()  # type: ignore[operator]
        index = min(int(offset / width), bucket_count - 1)
        buckets[index].total += 1
        if entry.is_problem:
            buckets[index].errors += 1

    return buckets


def detect_anomalies(clusters: Sequence[LogCluster], buckets: Sequence[TimeBucket]) -> list[str]:
    """Derive human-readable anomaly notes from clusters and time buckets.

    These are heuristics, not statistics, and they are phrased as observations
    rather than conclusions. Their job is to direct the model's attention, not
    to diagnose - a burst of errors at 10:23 is a fact; *why* is the agent's
    problem.

    Args:
        clusters: Template clusters.
        buckets: Time buckets.

    Returns:
        Short observation strings.
    """
    anomalies: list[str] = []

    # Error burst: a bucket carrying far more errors than the average.
    error_buckets = [bucket for bucket in buckets if bucket.errors > 0]
    if len(error_buckets) >= 2:
        mean_errors = sum(bucket.errors for bucket in buckets) / len(buckets)
        peak = max(buckets, key=lambda bucket: bucket.errors)
        if mean_errors > 0 and peak.errors > mean_errors * 3 and peak.errors >= 5:
            anomalies.append(
                f"Error burst at {peak.start.isoformat()}: {peak.errors} errors in one window "
                f"versus a {mean_errors:.1f} average - a {peak.errors / mean_errors:.1f}x spike."
            )

    # Onset: the first error appearing well after logging begins suggests a
    # discrete triggering event rather than a chronic condition.
    if buckets:
        first_error_index = next((index for index, bucket in enumerate(buckets) if bucket.errors > 0), None)
        if first_error_index is not None and first_error_index > len(buckets) * 0.2:
            anomalies.append(
                f"Errors begin at {buckets[first_error_index].start.isoformat()}, after a clean initial period - "
                "consistent with a discrete triggering event (deploy, config change, dependency failure)."
            )

    # A single template dominating the error volume.
    error_clusters = [cluster for cluster in clusters if cluster.level.is_problem]
    total_errors = sum(cluster.count for cluster in error_clusters)
    if error_clusters and total_errors >= 10:
        top = error_clusters[0]
        share = top.count / total_errors
        if share > 0.6:
            anomalies.append(
                f"One template accounts for {share:.0%} of all errors ({top.count} of {total_errors}): {top.template}"
            )

    # High-rate templates suggest a retry storm or hot loop.
    for cluster in error_clusters[:5]:
        rate = cluster.rate_per_minute
        if rate and rate > 60:
            anomalies.append(
                f"Very high error rate ({rate:.0f}/min) for: {cluster.template} - "
                "possible retry storm or tight failure loop."
            )

    # Errors spanning several services point at a shared dependency.
    multi_service = [cluster for cluster in error_clusters if len(cluster.services) > 1]
    if multi_service:
        affected = sorted({service for cluster in multi_service for service in cluster.services})
        if len(affected) > 1:
            anomalies.append(
                f"Correlated errors across {len(affected)} services ({', '.join(affected[:6])}) - "
                "suggests a shared dependency rather than a single-service fault."
            )

    return anomalies


def analyse_entries(entries: Sequence[LogEntry], *, truncated: bool = False) -> LogAnalysis:
    """Build a full :class:`LogAnalysis` from parsed entries.

    Args:
        entries: Parsed log entries.
        truncated: Whether the source was cut short by a line limit.

    Returns:
        The structured analysis.
    """
    if not entries:
        return LogAnalysis(
            total_entries=0,
            level_counts={},
            clusters=[],
            error_clusters=[],
            services=[],
            time_range=(None, None),
            truncated=truncated,
        )

    level_counts = Counter(entry.level.value for entry in entries)
    clusters = cluster_entries(entries)
    error_clusters = [cluster for cluster in clusters if cluster.level.is_problem]

    service_counts: defaultdict[str, int] = defaultdict(int)
    for entry in entries:
        if entry.service:
            service_counts[entry.service] += 1
    services = sorted(service_counts, key=lambda name: service_counts[name], reverse=True)

    timestamps = [entry.timestamp for entry in entries if entry.timestamp]
    time_range = (min(timestamps), max(timestamps)) if timestamps else (None, None)

    buckets = bucket_entries(entries)
    anomalies = detect_anomalies(clusters, buckets)

    return LogAnalysis(
        total_entries=len(entries),
        level_counts=dict(level_counts),
        clusters=clusters,
        error_clusters=error_clusters,
        services=services,
        time_range=time_range,
        buckets=buckets,
        anomalies=anomalies,
        formats=sorted({entry.format for entry in entries}),
        truncated=truncated,
    )


__all__ = [
    "LogAnalysis",
    "LogCluster",
    "TimeBucket",
    "analyse_entries",
    "bucket_entries",
    "cluster_entries",
    "detect_anomalies",
    "extract_template",
]
