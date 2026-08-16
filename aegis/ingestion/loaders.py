"""Loaders that turn raw sources into normalised documents ready for chunking.

A loader's job is to answer four questions about a blob of bytes:

1. What is the text? (decode, strip front matter, normalise whitespace)
2. What is it called? (explicit title, first heading, or filename)
3. What service does it concern? (front matter, path convention, or heading)
4. What kind of document is it? (runbook, postmortem, log bundle, ...)

Getting 3 right matters more than it looks. ``service`` is the single most
selective retrieval filter available - during a payments incident, restricting
search to payments documentation removes most of the corpus and most of the
opportunity for a confidently irrelevant answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import (
    dataclass,
    field,
)
import hashlib
from pathlib import Path
import re
from typing import (
    Any,
)

from aegis.core.exceptions import (
    PayloadTooLargeError,
    UnsupportedContentError,
)
from aegis.core.logging import logger
from aegis.models.knowledge import SourceType

# YAML front matter delimited by --- at the very start of the file.
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# First ATX heading, used as a title fallback.
_FIRST_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Extensions the pipeline knows how to read, mapped to a default source type.
_EXTENSION_TYPES: dict[str, SourceType] = {
    ".md": SourceType.RUNBOOK,
    ".markdown": SourceType.RUNBOOK,
    ".txt": SourceType.OTHER,
    ".log": SourceType.LOG_BUNDLE,
    ".jsonl": SourceType.LOG_BUNDLE,
    ".rst": SourceType.OTHER,
    ".yaml": SourceType.ALERT_DEFINITION,
    ".yml": SourceType.ALERT_DEFINITION,
}

# Directory names that imply a source type, used when front matter is absent.
_DIRECTORY_HINTS: dict[str, SourceType] = {
    "runbooks": SourceType.RUNBOOK,
    "runbook": SourceType.RUNBOOK,
    "postmortems": SourceType.POSTMORTEM,
    "postmortem": SourceType.POSTMORTEM,
    "incidents": SourceType.POSTMORTEM,
    "architecture": SourceType.ARCHITECTURE,
    "design": SourceType.ARCHITECTURE,
    "logs": SourceType.LOG_BUNDLE,
    "alerts": SourceType.ALERT_DEFINITION,
}


@dataclass(slots=True)
class LoadedDocument:
    """A source normalised and ready for chunking."""

    title: str
    content: str
    source_uri: str
    source_type: SourceType
    service: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """SHA-256 of the normalised content.

        Computed over normalised text rather than raw bytes, so a file that
        differs only in line endings or trailing whitespace hashes identically
        and is correctly skipped as unchanged.
        """
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def normalise_text(raw: str) -> str:
    """Canonicalise text before hashing and chunking.

    Collapses line endings, strips trailing whitespace per line, and caps
    runs of blank lines. This keeps the content hash stable across editors and
    prevents whitespace noise from creating spurious chunk boundaries.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML front matter from the head of a document.

    Falls back to a minimal ``key: value`` parser when PyYAML is unavailable,
    which covers the flat metadata blocks runbooks actually use.

    Args:
        text: Document text, possibly beginning with ``---``.

    Returns:
        A ``(metadata, remaining_text)`` pair. Metadata is empty when there is
        no front matter or it fails to parse.
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text

    block, remainder = match.group(1), text[match.end() :]

    try:
        import yaml

        parsed = yaml.safe_load(block)
        metadata = parsed if isinstance(parsed, dict) else {}
    except Exception:
        metadata = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip().strip("\"'")

    return metadata, remainder


def infer_title(text: str, metadata: dict[str, Any], fallback: str) -> str:
    """Choose a document title.

    Preference order: explicit front matter, first level-1 heading, then the
    filename with separators humanised.
    """
    if isinstance(metadata.get("title"), str) and metadata["title"].strip():
        return metadata["title"].strip()

    heading = _FIRST_HEADING_RE.search(text)
    if heading:
        return heading.group(1).strip()

    return fallback.replace("-", " ").replace("_", " ").strip() or "Untitled document"


def infer_service(metadata: dict[str, Any], path: Path | None, title: str) -> str | None:
    """Determine which service a document describes.

    Preference order:

    1. Explicit ``service`` in front matter - always authoritative.
    2. A path convention such as ``runbooks/payments/503.md``, where the
       directory under the corpus root names the service.
    3. A ``service-name:`` prefix in the title.

    Returns ``None`` when no signal is available, which leaves the document
    globally searchable rather than mis-filed under a guessed service.
    """
    for key in ("service", "component", "system"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()

    if path is not None:
        parts = [part.lower() for part in path.parts]
        for hint in _DIRECTORY_HINTS:
            if hint in parts:
                index = parts.index(hint)
                # The directory immediately below the type directory, if it is
                # not the file itself, names the service.
                if index + 1 < len(parts) - 1:
                    return parts[index + 1]

    prefix = title.split(":", 1)[0].strip().lower()
    if prefix != title.strip().lower() and 2 <= len(prefix) <= 40:
        return prefix

    return None


def infer_source_type(metadata: dict[str, Any], path: Path | None) -> SourceType:
    """Determine the document type from front matter, directory, or extension."""
    declared = metadata.get("type") or metadata.get("source_type")
    if isinstance(declared, str):
        try:
            return SourceType(declared.strip().lower())
        except ValueError:
            pass

    if path is not None:
        for part in (segment.lower() for segment in path.parts):
            if part in _DIRECTORY_HINTS:
                return _DIRECTORY_HINTS[part]
        return _EXTENSION_TYPES.get(path.suffix.lower(), SourceType.OTHER)

    return SourceType.OTHER


def load_text(
    raw: str,
    *,
    source_uri: str,
    title: str | None = None,
    source_type: SourceType | None = None,
    service: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> LoadedDocument:
    """Build a :class:`LoadedDocument` from in-memory text.

    Used by the upload endpoint, where there is no file on disk. Explicit
    arguments always win over inference.

    Args:
        raw: Raw document text.
        source_uri: Provenance identifier recorded on the document.
        title: Explicit title; inferred when omitted.
        source_type: Explicit type; inferred when omitted.
        service: Explicit service; inferred when omitted.
        extra_metadata: Additional metadata merged over the parsed front matter.

    Returns:
        The normalised document.
    """
    metadata, body = parse_front_matter(raw)
    content = normalise_text(body)
    resolved_title = title or infer_title(content, metadata, Path(source_uri).stem)

    metadata.update(extra_metadata or {})

    return LoadedDocument(
        title=resolved_title,
        content=content,
        source_uri=source_uri,
        source_type=source_type or infer_source_type(metadata, None),
        service=service or infer_service(metadata, None, resolved_title),
        metadata=metadata,
    )


def load_file(path: Path, *, max_bytes: int | None = None) -> LoadedDocument:
    """Load and normalise a single file.

    Args:
        path: File to read.
        max_bytes: Reject files larger than this.

    Returns:
        The normalised document.

    Raises:
        UnsupportedContentError: If the extension has no registered loader.
        PayloadTooLargeError: If the file exceeds ``max_bytes``.
    """
    suffix = path.suffix.lower()
    if suffix not in _EXTENSION_TYPES:
        raise UnsupportedContentError(
            f"No loader registered for '{suffix}' files.",
            context={"path": str(path), "supported": sorted(_EXTENSION_TYPES)},
        )

    size = path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise PayloadTooLargeError(
            f"{path.name} is {size} bytes, above the {max_bytes} byte limit.",
            context={"path": str(path), "size": size, "limit": max_bytes},
        )

    # errors="replace" rather than strict: a single malformed byte in a log
    # capture should not abort ingestion of an otherwise useful bundle.
    raw = path.read_text(encoding="utf-8", errors="replace")
    metadata, body = parse_front_matter(raw)
    content = normalise_text(body)

    title = infer_title(content, metadata, path.stem)
    metadata.setdefault("filename", path.name)
    metadata.setdefault("size_bytes", size)

    return LoadedDocument(
        title=title,
        content=content,
        source_uri=str(path),
        source_type=infer_source_type(metadata, path),
        service=infer_service(metadata, path, title),
        metadata=metadata,
    )


def discover_files(root: Path, patterns: tuple[str, ...] = ("**/*",)) -> Iterator[Path]:
    """Yield ingestible files beneath a directory.

    Skips hidden files and directories (``.git``, editor swap files) and any
    extension without a loader, so pointing the pipeline at a repository root
    does something sensible instead of erroring on the first ``.png``.

    Args:
        root: Directory to walk.
        patterns: Glob patterns applied relative to ``root``.

    Yields:
        Paths that can be loaded.
    """
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            if any(part.startswith(".") for part in path.parts):
                continue
            if path.suffix.lower() not in _EXTENSION_TYPES:
                continue
            seen.add(path)
            yield path


def load_directory(root: Path, *, max_bytes: int | None = None) -> list[LoadedDocument]:
    """Load every ingestible file beneath a directory.

    Individual failures are logged and skipped rather than aborting the run: one
    unreadable file in a large runbook repository should not block the other
    several hundred.

    Args:
        root: Directory to walk.
        max_bytes: Per-file size limit.

    Returns:
        Successfully loaded documents.
    """
    documents: list[LoadedDocument] = []
    for path in discover_files(root):
        try:
            documents.append(load_file(path, max_bytes=max_bytes))
        except Exception as exc:
            logger.warning("document_load_skipped", path=str(path), error=str(exc))
    logger.info("directory_loaded", root=str(root), documents=len(documents))
    return documents


__all__ = [
    "LoadedDocument",
    "discover_files",
    "infer_service",
    "infer_source_type",
    "infer_title",
    "load_directory",
    "load_file",
    "load_text",
    "normalise_text",
    "parse_front_matter",
]
