"""Multi-format log parsing.

Engineers paste whatever their platform emits. In practice that is one of four
shapes, and a tool that only understands one of them is useless during the
incident where the other three show up:

* **JSON lines** - structured logging (``{"ts":"...","level":"error",...}``).
* **logfmt** - ``ts=... level=error msg="..."``, common in Go services.
* **Bracketed/plain text** - ``2024-01-15 10:23:45 ERROR [payments] message``.
* **Free text** - anything else, including multi-line stack traces.

Each line is parsed into a :class:`LogEntry` with whatever fields could be
recovered. Nothing is discarded: an unparseable line still becomes an entry with
its raw text, because the line a parser cannot understand is disproportionately
often the interesting one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    UTC,
    datetime,
)
from enum import Enum
import json
import re
from typing import (
    Any,
)

from aegis.core.logging import logger


class LogLevel(str, Enum):
    """Normalised severity levels.

    Real-world logs use a wide vocabulary (``WARN``/``WARNING``,
    ``ERR``/``ERROR``, ``CRIT``/``FATAL``/``PANIC``). Collapsing to a fixed set
    means downstream analysis can count errors without enumerating synonyms.
    """

    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

    @property
    def is_problem(self) -> bool:
        """Whether this level indicates something went wrong."""
        return self in (LogLevel.WARNING, LogLevel.ERROR, LogLevel.CRITICAL)

    @property
    def rank(self) -> int:
        """Ordinal for sorting by severity."""
        return _LEVEL_RANKS[self]


_LEVEL_RANKS: dict[LogLevel, int] = {
    LogLevel.TRACE: 0,
    LogLevel.DEBUG: 1,
    LogLevel.INFO: 2,
    LogLevel.WARNING: 3,
    LogLevel.ERROR: 4,
    LogLevel.CRITICAL: 5,
    LogLevel.UNKNOWN: -1,
}

_LEVEL_ALIASES: dict[str, LogLevel] = {
    "trace": LogLevel.TRACE,
    "trc": LogLevel.TRACE,
    "verbose": LogLevel.TRACE,
    "debug": LogLevel.DEBUG,
    "dbg": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "inf": LogLevel.INFO,
    "information": LogLevel.INFO,
    "notice": LogLevel.INFO,
    "warn": LogLevel.WARNING,
    "warning": LogLevel.WARNING,
    "wrn": LogLevel.WARNING,
    "error": LogLevel.ERROR,
    "err": LogLevel.ERROR,
    "eror": LogLevel.ERROR,
    "severe": LogLevel.ERROR,
    "critical": LogLevel.CRITICAL,
    "crit": LogLevel.CRITICAL,
    "fatal": LogLevel.CRITICAL,
    "panic": LogLevel.CRITICAL,
    "emerg": LogLevel.CRITICAL,
    "alert": LogLevel.CRITICAL,
}

# Common timestamp shapes, tried in order of specificity.
_TIMESTAMP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+(?:Z|[+-]\d{2}:\d{2})"), "iso"),
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})"), "iso"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"), "naive"),
    (re.compile(r"\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}"), "clf"),
    (re.compile(r"[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"), "syslog"),
)

# Level token appearing as a bare word, optionally bracketed.
_LEVEL_RE = re.compile(
    r"(?:^|[\s\[\(\|])(" + "|".join(sorted(_LEVEL_ALIASES, key=len, reverse=True)) + r")(?:[\s\]\)\|:]|$)",
    re.IGNORECASE,
)

# logfmt key=value pairs, with optional double-quoted values.
_LOGFMT_RE = re.compile(r'(\w[\w.\-]*)=(?:"((?:[^"\\]|\\.)*)"|(\S*))')

# Bracketed component such as [payments] or [worker-3].
_COMPONENT_RE = re.compile(r"\[([a-zA-Z][\w.\-/]{1,60})\]")

# Field names that commonly carry each concept in structured logs.
_TS_KEYS = ("timestamp", "time", "ts", "@timestamp", "eventTime", "date", "asctime")
_LEVEL_KEYS = ("level", "levelname", "severity", "lvl", "loglevel", "log_level", "priority")
_MESSAGE_KEYS = ("message", "msg", "event", "text", "log", "body", "description")
_SERVICE_KEYS = ("service", "service_name", "app", "application", "component", "logger", "source", "container")


@dataclass(slots=True)
class LogEntry:
    """A single parsed log line."""

    raw: str
    message: str = ""
    level: LogLevel = LogLevel.UNKNOWN
    timestamp: datetime | None = None
    service: str | None = None
    line_number: int = 0
    fields: dict[str, Any] = field(default_factory=dict)
    format: str = "unknown"

    @property
    def is_problem(self) -> bool:
        """Whether this entry reports a warning or worse."""
        return self.level.is_problem

    def summary(self, max_chars: int = 200) -> str:
        """Render a compact one-line summary for prompts."""
        parts: list[str] = []
        if self.timestamp:
            parts.append(self.timestamp.isoformat())
        if self.level is not LogLevel.UNKNOWN:
            parts.append(self.level.value.upper())
        if self.service:
            parts.append(f"[{self.service}]")
        message = self.message or self.raw
        parts.append(message[:max_chars])
        return " ".join(parts)


def normalise_level(value: str | None) -> LogLevel:
    """Map a raw level token onto a :class:`LogLevel`."""
    if not value:
        return LogLevel.UNKNOWN
    return _LEVEL_ALIASES.get(str(value).strip().lower(), LogLevel.UNKNOWN)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp from a string or epoch number.

    Naive datetimes are assumed UTC. That assumption is stated rather than
    silently applied because mixing naive local times with UTC is a classic way
    to build an incident timeline that is wrong by hours - and the resulting
    "the error happened before the deploy" conclusion is confidently backwards.

    Args:
        value: A string, int, or float timestamp.

    Returns:
        A timezone-aware UTC datetime, or ``None`` when unparseable.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            # Heuristic: values past ~2001 in milliseconds are far larger than
            # any plausible epoch-seconds value for a live system.
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()
    if not text:
        return None

    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass

    for pattern, _kind in _TIMESTAMP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        fragment = match.group(0).replace(",", ".")
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%d/%b/%Y:%H:%M:%S",
        ):
            try:
                return datetime.strptime(fragment, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _first_present(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    """Return the first non-empty value among ``keys``, case-insensitively."""
    lowered = {key.lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _parse_json_line(line: str, line_number: int) -> LogEntry | None:
    """Parse a JSON-encoded log line, or return ``None`` if it is not JSON."""
    stripped = line.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    message = _first_present(payload, _MESSAGE_KEYS)
    return LogEntry(
        raw=line,
        message=str(message) if message is not None else stripped,
        level=normalise_level(_first_present(payload, _LEVEL_KEYS)),
        timestamp=parse_timestamp(_first_present(payload, _TS_KEYS)),
        service=(lambda value: str(value) if value else None)(_first_present(payload, _SERVICE_KEYS)),
        line_number=line_number,
        fields=payload,
        format="json",
    )


def _parse_logfmt_line(line: str, line_number: int) -> LogEntry | None:
    """Parse a logfmt line, or return ``None`` if it does not look like one."""
    pairs = _LOGFMT_RE.findall(line)
    # Require several pairs and an '=' early in the line; otherwise ordinary
    # prose containing "foo=bar" would be misread as structured.
    if len(pairs) < 2 or "=" not in line[:60]:
        return None

    fields: dict[str, Any] = {}
    for key, quoted, bare in pairs:
        fields[key] = (quoted or bare).replace('\\"', '"')

    message = _first_present(fields, _MESSAGE_KEYS)
    if message is None:
        return None

    return LogEntry(
        raw=line,
        message=str(message),
        level=normalise_level(_first_present(fields, _LEVEL_KEYS)),
        timestamp=parse_timestamp(_first_present(fields, _TS_KEYS)),
        service=(lambda value: str(value) if value else None)(_first_present(fields, _SERVICE_KEYS)),
        line_number=line_number,
        fields=fields,
        format="logfmt",
    )


def _parse_text_line(line: str, line_number: int) -> LogEntry:
    """Parse a plain-text log line on a best-effort basis.

    Always succeeds. Whatever cannot be identified is left unset and the full
    text is retained as the message.
    """
    timestamp = parse_timestamp(line)

    level = LogLevel.UNKNOWN
    level_match = _LEVEL_RE.search(line)
    if level_match:
        level = normalise_level(level_match.group(1))

    service = None
    component_match = _COMPONENT_RE.search(line)
    if component_match:
        candidate = component_match.group(1)
        # A bracketed token that is itself a level (e.g. "[ERROR]") names a
        # severity, not a service.
        if normalise_level(candidate) is LogLevel.UNKNOWN:
            service = candidate

    # Strip the recognised prefix so the message is the actual content, which
    # makes template clustering far more accurate.
    message = line
    if level_match:
        message = line[level_match.end() :].strip()
    elif timestamp:
        for pattern, _kind in _TIMESTAMP_PATTERNS:
            match = pattern.search(line)
            if match:
                message = line[match.end() :].strip()
                break
    message = message.lstrip(" -:|\t") or line

    return LogEntry(
        raw=line,
        message=message,
        level=level,
        timestamp=timestamp,
        service=service,
        line_number=line_number,
        format="text",
    )


def parse_line(line: str, line_number: int = 0) -> LogEntry:
    """Parse one log line, trying each format in order of specificity.

    Args:
        line: The raw line.
        line_number: Position in the source, retained for citation.

    Returns:
        A populated :class:`LogEntry`. Never raises.
    """
    for parser in (_parse_json_line, _parse_logfmt_line):
        entry = parser(line, line_number)
        if entry is not None:
            return entry
    return _parse_text_line(line, line_number)


def parse_logs(text: str, *, max_lines: int | None = None) -> list[LogEntry]:
    """Parse a log bundle into entries.

    Continuation lines - indented text, ``at com.example...`` frames,
    ``Caused by:`` - are appended to the preceding entry rather than becoming
    entries of their own. A stack trace is one event, and splitting it would
    both inflate error counts and destroy the frame ordering that identifies
    the failing code.

    Args:
        text: Raw log text.
        max_lines: Stop after this many source lines.

    Returns:
        Parsed entries in source order.
    """
    continuation = re.compile(r"^(\s+|at\s|Caused by:|\.{3}\s*\d+\s+more|Traceback\b|\s*File\s\")")
    entries: list[LogEntry] = []
    truncated = False

    for line_number, line in enumerate(text.splitlines(), start=1):
        if max_lines is not None and line_number > max_lines:
            truncated = True
            break
        if not line.strip():
            continue

        if entries and continuation.match(line):
            previous = entries[-1]
            previous.raw = f"{previous.raw}\n{line}"
            previous.message = f"{previous.message}\n{line.strip()}"
            continue

        entries.append(parse_line(line, line_number))

    if truncated:
        logger.warning("log_parsing_truncated", limit=max_lines, parsed=len(entries))

    logger.debug(
        "logs_parsed",
        entries=len(entries),
        errors=sum(1 for entry in entries if entry.is_problem),
        formats=sorted({entry.format for entry in entries}),
    )
    return entries


__all__ = [
    "LogEntry",
    "LogLevel",
    "normalise_level",
    "parse_line",
    "parse_logs",
    "parse_timestamp",
]
