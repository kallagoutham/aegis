"""Document chunking strategies.

Chunking is the highest-leverage and most under-appreciated step in a RAG
system. Retrieval can only ever return a chunk; if the chunk boundary falls in
the wrong place, no amount of embedding quality or reranking recovers the lost
context.

The naive approach - split every N characters - fails operational documentation
badly. A runbook section reading::

    ## Payment service: 503 from checkout
    1. Check the connection pool: `kubectl exec ... -- pgbouncer -s`
    2. If saturated, scale replicas: `kubectl scale deploy/payments --replicas=6`

split at 500 characters yields a chunk containing step 2 with no indication of
which failure it addresses. Retrieved during an incident, that is worse than
nothing: it reads like authoritative advice detached from its precondition.

Two strategies live here:

:class:`MarkdownChunker`
    Splits on heading structure first, then packs sections into token-budget
    windows. Every chunk carries its full heading breadcrumb, so a chunk always
    knows what it is about.

:class:`LogChunker`
    Groups log lines into overlapping windows, keeping multi-line stack traces
    intact and preserving timestamp ranges for correlation.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import (
    dataclass,
    field,
)
import re
from typing import (
    Any,
)

from aegis.core.config import settings
from aegis.core.logging import logger

# Matches ATX markdown headings, capturing depth and text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$", re.MULTILINE)

# Fenced code blocks. Chunking must never split one of these: half a shell
# command is worse than useless in a runbook.
_FENCE_RE = re.compile(r"^```")

# Paragraph boundary: a blank line. The preferred split point when a section
# exceeds the token budget.
_PARAGRAPH_RE = re.compile(r"\n\s*\n")

# Sentence-ish boundary, used only when a single paragraph is itself oversized.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a string.

    Uses ``tiktoken`` when installed for an exact count, falling back to a
    characters/4 heuristic otherwise. The fallback keeps chunking usable in
    minimal environments; it runs slightly conservative, producing marginally
    smaller chunks rather than ones that overflow a context window.

    Both paths agree on the edges: empty or whitespace-only input is 0 tokens,
    and anything else is at least 1. Without the explicit clamp the two
    implementations disagree (tiktoken returns 0 for ``""``, the heuristic
    returns 1), which makes chunk counts depend on whether an optional
    dependency happens to be installed.

    Args:
        text: Text to measure.

    Returns:
        Estimated token count.
    """
    if not text.strip():
        return 0

    encoder = _get_encoder()
    if encoder is not None:
        return max(1, len(encoder.encode(text, disallowed_special=())))
    return max(1, len(text) // 4)


_encoder_cache: Any = None
_encoder_loaded = False


def _get_encoder() -> Any:
    """Load and memoise the tiktoken encoder, tolerating its absence."""
    global _encoder_cache, _encoder_loaded
    if not _encoder_loaded:
        _encoder_loaded = True
        try:
            import tiktoken

            _encoder_cache = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # tiktoken missing, or its vocabulary download failed in an offline
            # build. The heuristic is good enough to keep ingestion working.
            _encoder_cache = None
            logger.debug("tiktoken_unavailable_using_heuristic")
    return _encoder_cache


@dataclass(slots=True)
class Chunk:
    """One retrievable passage produced by a chunker."""

    content: str
    heading_path: str = ""
    token_count: int = 0
    index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Compute the token count when the caller did not supply one."""
        if not self.token_count:
            self.token_count = estimate_tokens(self.content)

    def with_heading_context(self) -> str:
        """Return the content prefixed with its heading breadcrumb.

        This is what actually gets embedded. Prepending the breadcrumb means the
        vector encodes *"Payment Service > Troubleshooting > 503 errors"* along
        with the body text, which measurably improves retrieval for queries that
        name a service or symptom without repeating the body's vocabulary.
        """
        if not self.heading_path:
            return self.content
        return f"{self.heading_path}\n\n{self.content}"


@dataclass(slots=True)
class _Section:
    """An intermediate heading-delimited region of a markdown document."""

    heading_path: str
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The section body as a single string."""
        return "\n".join(self.lines).strip()


class MarkdownChunker:
    """Structure-aware chunker for markdown documents.

    Algorithm:

    1. Walk the document line by line, tracking the heading stack and whether we
       are inside a fenced code block (headings inside fences are literal text,
       not structure).
    2. Emit a :class:`_Section` per heading region, each tagged with its full
       breadcrumb.
    3. Pack sections into chunks up to ``CHUNK_TARGET_TOKENS``. A section that
       fits becomes one chunk; an oversized one is split on paragraph, then
       sentence, then hard token boundaries.
    4. Merge chunks below ``CHUNK_MIN_TOKENS`` into their neighbour. Tiny chunks
       ("See the table below.") carry no retrievable signal and pollute results.
    """

    def __init__(
        self,
        target_tokens: int | None = None,
        overlap_tokens: int | None = None,
        min_tokens: int | None = None,
    ) -> None:
        """Configure chunk sizing.

        Args:
            target_tokens: Preferred chunk size.
            overlap_tokens: Tokens repeated between adjacent chunks, so a fact
                spanning a boundary is retrievable from either side.
            min_tokens: Chunks below this are merged forward.
        """
        self.target_tokens = target_tokens or settings.CHUNK_TARGET_TOKENS
        self.overlap_tokens = overlap_tokens if overlap_tokens is not None else settings.CHUNK_OVERLAP_TOKENS
        self.min_tokens = min_tokens if min_tokens is not None else settings.CHUNK_MIN_TOKENS

    def chunk(self, text: str, base_metadata: dict[str, Any] | None = None) -> list[Chunk]:
        """Split a markdown document into chunks.

        Args:
            text: Raw markdown.
            base_metadata: Metadata copied onto every produced chunk.

        Returns:
            Ordered chunks with heading breadcrumbs and token counts.
        """
        base_metadata = base_metadata or {}
        sections = list(self._split_sections(text))
        if not sections:
            return []

        chunks: list[Chunk] = []
        for section in sections:
            body = section.text
            if not body:
                continue
            for piece in self._split_to_budget(body):
                chunks.append(
                    Chunk(
                        content=piece,
                        heading_path=section.heading_path,
                        metadata=dict(base_metadata),
                    )
                )

        chunks = self._merge_undersized(chunks)
        chunks = self._apply_overlap(chunks)

        for position, chunk in enumerate(chunks):
            chunk.index = position

        logger.debug(
            "markdown_chunked",
            sections=len(sections),
            chunks=len(chunks),
            avg_tokens=round(sum(chunk.token_count for chunk in chunks) / len(chunks), 1) if chunks else 0,
        )
        return chunks

    def _split_sections(self, text: str) -> Iterator[_Section]:
        """Yield heading-delimited sections, tracking the heading stack."""
        heading_stack: list[tuple[int, str]] = []
        current = _Section(heading_path="")
        in_fence = False

        for line in text.splitlines():
            if _FENCE_RE.match(line.strip()):
                in_fence = not in_fence
                current.lines.append(line)
                continue

            # Only treat a '#' line as a heading outside a code fence, otherwise
            # a shell comment in an example would restructure the document.
            heading_match = None if in_fence else _HEADING_RE.match(line)

            if heading_match:
                if current.lines and current.text:
                    yield current

                depth = len(heading_match.group(1))
                title = heading_match.group(2).strip()

                # Pop deeper-or-equal headings; this heading replaces them.
                while heading_stack and heading_stack[-1][0] >= depth:
                    heading_stack.pop()
                heading_stack.append((depth, title))

                breadcrumb = " > ".join(entry[1] for entry in heading_stack)
                current = _Section(heading_path=breadcrumb)
            else:
                current.lines.append(line)

        if current.text:
            yield current

    def _split_to_budget(self, body: str) -> list[str]:
        """Split a section body into pieces within the token budget.

        Falls back progressively: paragraphs, then sentences, then a hard split.
        Each level is only reached when the previous one produced something that
        still does not fit.
        """
        if estimate_tokens(body) <= self.target_tokens:
            return [body]

        pieces: list[str] = []
        buffer: list[str] = []
        buffer_tokens = 0

        for paragraph in _PARAGRAPH_RE.split(body):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            paragraph_tokens = estimate_tokens(paragraph)

            # A single paragraph larger than the budget cannot be packed; split
            # it further before it enters the buffer.
            if paragraph_tokens > self.target_tokens:
                if buffer:
                    pieces.append("\n\n".join(buffer))
                    buffer, buffer_tokens = [], 0
                pieces.extend(self._split_oversized_paragraph(paragraph))
                continue

            if buffer_tokens + paragraph_tokens > self.target_tokens and buffer:
                pieces.append("\n\n".join(buffer))
                buffer, buffer_tokens = [], 0

            buffer.append(paragraph)
            buffer_tokens += paragraph_tokens

        if buffer:
            pieces.append("\n\n".join(buffer))
        return pieces

    def _split_oversized_paragraph(self, paragraph: str) -> list[str]:
        """Split a paragraph that alone exceeds the token budget."""
        sentences = _SENTENCE_RE.split(paragraph)

        # No sentence boundaries at all (a minified blob, a giant table row):
        # fall back to a hard character split so ingestion cannot stall.
        if len(sentences) == 1:
            return self._hard_split(paragraph)

        pieces: list[str] = []
        buffer: list[str] = []
        buffer_tokens = 0
        for sentence in sentences:
            sentence_tokens = estimate_tokens(sentence)
            if buffer_tokens + sentence_tokens > self.target_tokens and buffer:
                pieces.append(" ".join(buffer))
                buffer, buffer_tokens = [], 0
            if sentence_tokens > self.target_tokens:
                pieces.extend(self._hard_split(sentence))
                continue
            buffer.append(sentence)
            buffer_tokens += sentence_tokens
        if buffer:
            pieces.append(" ".join(buffer))
        return pieces

    def _hard_split(self, text: str) -> list[str]:
        """Split on a character budget as a last resort."""
        # Derive a character window from the token budget using the same 4:1
        # ratio the estimator assumes.
        window = self.target_tokens * 4
        return [text[start : start + window] for start in range(0, len(text), window)]

    def _merge_undersized(self, chunks: list[Chunk]) -> list[Chunk]:
        """Merge chunks below the minimum size into an adjacent chunk.

        Only merges chunks sharing a heading path, so merging never fuses two
        unrelated runbook sections into one misleading passage.
        """
        if self.min_tokens <= 0 or len(chunks) < 2:
            return chunks

        merged: list[Chunk] = []
        for chunk in chunks:
            if (
                merged
                and chunk.token_count < self.min_tokens
                and merged[-1].heading_path == chunk.heading_path
                and merged[-1].token_count + chunk.token_count <= self.target_tokens
            ):
                previous = merged[-1]
                previous.content = f"{previous.content}\n\n{chunk.content}"
                previous.token_count = estimate_tokens(previous.content)
                continue
            merged.append(chunk)
        return merged

    def _apply_overlap(self, chunks: list[Chunk]) -> list[Chunk]:
        """Prefix each chunk with the tail of its predecessor.

        Overlap makes a fact that straddles a boundary retrievable from either
        chunk. Only applied within a shared heading path: bleeding the end of
        "Rollback procedure" into the start of "Escalation contacts" would
        create a chunk that misrepresents both.
        """
        if self.overlap_tokens <= 0 or len(chunks) < 2:
            return chunks

        overlap_chars = self.overlap_tokens * 4
        for position in range(1, len(chunks)):
            previous, current = chunks[position - 1], chunks[position]
            if previous.heading_path != current.heading_path:
                continue
            tail = previous.content[-overlap_chars:].lstrip()
            if tail:
                current.content = f"{tail}\n\n{current.content}"
                current.token_count = estimate_tokens(current.content)
        return chunks


class LogChunker:
    """Chunker for log bundles.

    Logs need different handling from prose:

    * **Entries, not sentences.** A log entry is the atomic unit; splitting one
      in half produces an unparseable fragment.
    * **Stack traces stay whole.** Continuation lines (indented, or starting
      with ``at``/``Caused by``) belong to the entry above them. A stack trace
      cut in half loses the frame that identifies the failing code.
    * **Time ranges are metadata.** Recording each chunk's first and last
      timestamp lets the agent correlate a retrieved chunk against an incident
      window instead of guessing whether it is even relevant.
    """

    # Lines that continue the previous entry rather than starting a new one.
    _CONTINUATION_RE = re.compile(r"^(\s+|at\s|Caused by:|\.\.\.\s*\d+\s+more|Traceback|\s*File\s)")

    def __init__(self, lines_per_chunk: int = 60, overlap_lines: int = 8) -> None:
        """Configure log windowing.

        Args:
            lines_per_chunk: Target entries per chunk.
            overlap_lines: Entries repeated between adjacent chunks, so a causal
                sequence spanning a boundary stays visible in one place.
        """
        self.lines_per_chunk = lines_per_chunk
        self.overlap_lines = overlap_lines

    def chunk(self, text: str, base_metadata: dict[str, Any] | None = None) -> list[Chunk]:
        """Split a log bundle into overlapping windows.

        Args:
            text: Raw log text.
            base_metadata: Metadata copied onto every chunk.

        Returns:
            Ordered chunks, each annotated with its line range and time span.
        """
        base_metadata = base_metadata or {}
        entries = self._group_entries(text.splitlines())
        if not entries:
            return []

        chunks: list[Chunk] = []
        step = max(1, self.lines_per_chunk - self.overlap_lines)

        for position, start in enumerate(range(0, len(entries), step)):
            window = entries[start : start + self.lines_per_chunk]
            if not window:
                continue

            content = "\n".join(window)
            metadata = dict(base_metadata)
            metadata.update(
                {
                    "entry_start": start,
                    "entry_end": start + len(window) - 1,
                    "entry_count": len(window),
                }
            )

            timestamps = self._extract_timestamps(window)
            if timestamps:
                metadata["time_start"] = timestamps[0]
                metadata["time_end"] = timestamps[-1]

            chunks.append(
                Chunk(
                    content=content,
                    heading_path=self._describe_window(metadata),
                    index=position,
                    metadata=metadata,
                )
            )

            # Stop once the window has consumed the tail, otherwise the final
            # overlapping step would emit a duplicate trailing chunk.
            if start + self.lines_per_chunk >= len(entries):
                break

        logger.debug("log_chunked", entries=len(entries), chunks=len(chunks))
        return chunks

    def _group_entries(self, lines: Sequence[str]) -> list[str]:
        """Fold continuation lines into the entry they belong to."""
        entries: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            if entries and self._CONTINUATION_RE.match(line):
                entries[-1] = f"{entries[-1]}\n{line}"
            else:
                entries.append(line)
        return entries

    @staticmethod
    def _extract_timestamps(entries: Sequence[str]) -> list[str]:
        """Pull ISO-8601-ish timestamps from a window, in order of appearance."""
        pattern = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
        found: list[str] = []
        for entry in entries:
            match = pattern.search(entry)
            if match:
                found.append(match.group(0))
        return found

    @staticmethod
    def _describe_window(metadata: dict[str, Any]) -> str:
        """Build a human-readable label used as the chunk's heading path."""
        start, end = metadata.get("entry_start", 0), metadata.get("entry_end", 0)
        label = f"log entries {start}-{end}"
        if "time_start" in metadata:
            label += f" ({metadata['time_start']} to {metadata['time_end']})"
        return label


def get_chunker(source_type: str) -> MarkdownChunker | LogChunker:
    """Select the chunker appropriate to a document type.

    Args:
        source_type: A :class:`~aegis.models.knowledge.SourceType` value.

    Returns:
        A configured chunker.
    """
    return LogChunker() if source_type == "log_bundle" else MarkdownChunker()


__all__ = [
    "Chunk",
    "LogChunker",
    "MarkdownChunker",
    "estimate_tokens",
    "get_chunker",
]
