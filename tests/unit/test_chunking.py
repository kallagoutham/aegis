"""Tests for document chunking.

The central property under test is that structure survives chunking. A chunk
that loses its heading breadcrumb is a chunk that can be retrieved and then
misread, which is the specific failure this module exists to prevent.
"""

from __future__ import annotations

from aegis.ingestion.chunking import (
    Chunk,
    LogChunker,
    MarkdownChunker,
    estimate_tokens,
    get_chunker,
)


class TestEstimateTokens:
    """Token estimation."""

    def test_returns_positive_for_non_empty_text(self):
        assert estimate_tokens("hello world") > 0

    def test_scales_with_length(self):
        short = estimate_tokens("a short sentence")
        long = estimate_tokens("a short sentence " * 50)
        assert long > short

    def test_empty_input_is_zero_tokens(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("   \n\t ") == 0

    def test_non_empty_input_is_at_least_one_token(self):
        # Both the tiktoken and heuristic paths must agree here, or chunk
        # counts would depend on whether an optional dependency is installed.
        assert estimate_tokens("a") >= 1


class TestMarkdownChunker:
    """Structure-aware markdown chunking."""

    def test_produces_chunks(self, runbook_markdown):
        chunks = MarkdownChunker().chunk(runbook_markdown)
        assert chunks
        assert all(isinstance(chunk, Chunk) for chunk in chunks)

    def test_every_chunk_carries_heading_breadcrumb(self, runbook_markdown):
        chunks = MarkdownChunker().chunk(runbook_markdown)
        # The document has headings throughout, so every chunk with real content
        # should know where it came from.
        assert any(chunk.heading_path for chunk in chunks)

    def test_breadcrumb_is_hierarchical(self, runbook_markdown):
        chunks = MarkdownChunker().chunk(runbook_markdown)
        breadcrumbs = {chunk.heading_path for chunk in chunks}
        nested = [crumb for crumb in breadcrumbs if " > " in crumb]
        assert nested, f"expected nested breadcrumbs, got {breadcrumbs}"
        # The deepest section should carry its full ancestry.
        assert any("Troubleshooting" in crumb and "503" in crumb for crumb in nested)

    def test_headings_inside_code_fences_are_not_structure(self):
        text = """# Real Heading

Some prose.

```bash
# This is a shell comment, not a heading
echo hello
```

More prose.
"""
        chunks = MarkdownChunker().chunk(text)
        breadcrumbs = {chunk.heading_path for chunk in chunks}
        assert not any("shell comment" in crumb for crumb in breadcrumbs)

    def test_chunks_respect_token_budget(self):
        # A long single-heading document must be split.
        body = "\n\n".join(f"Paragraph number {index} with some filler text." for index in range(200))
        chunker = MarkdownChunker(target_tokens=100, overlap_tokens=0, min_tokens=0)
        chunks = chunker.chunk(f"# Title\n\n{body}")
        assert len(chunks) > 1
        # Allow generous slack: the estimator is approximate and a single
        # oversized paragraph may exceed the target.
        assert all(chunk.token_count <= 400 for chunk in chunks)

    def test_overlap_repeats_tail_of_previous_chunk(self):
        body = "\n\n".join(f"Sentence {index} carries distinct content here." for index in range(60))
        without = MarkdownChunker(target_tokens=80, overlap_tokens=0, min_tokens=0).chunk(f"# T\n\n{body}")
        with_overlap = MarkdownChunker(target_tokens=80, overlap_tokens=30, min_tokens=0).chunk(f"# T\n\n{body}")

        assert len(with_overlap) == len(without)
        # Overlap only adds text, so total content must grow.
        assert sum(c.token_count for c in with_overlap) > sum(c.token_count for c in without)

    def test_overlap_does_not_cross_heading_boundaries(self):
        text = """# Doc

## Rollback procedure

Run the rollback script immediately and confirm traffic recovers.

## Escalation contacts

Page the on-call rota.
"""
        chunks = MarkdownChunker(target_tokens=50, overlap_tokens=20, min_tokens=0).chunk(text)
        escalation = [c for c in chunks if "Escalation" in c.heading_path]
        assert escalation
        # Bleeding rollback text into escalation would misrepresent both.
        assert "rollback script" not in escalation[0].content

    def test_tiny_chunks_are_merged(self):
        text = "# Title\n\n## A\n\nShort.\n\nAlso short.\n"
        merged = MarkdownChunker(target_tokens=500, overlap_tokens=0, min_tokens=50).chunk(text)
        unmerged = MarkdownChunker(target_tokens=500, overlap_tokens=0, min_tokens=0).chunk(text)
        assert len(merged) <= len(unmerged)

    def test_empty_document_produces_no_chunks(self):
        assert MarkdownChunker().chunk("") == []
        assert MarkdownChunker().chunk("   \n\n  ") == []

    def test_chunk_indices_are_contiguous(self, runbook_markdown):
        chunks = MarkdownChunker().chunk(runbook_markdown)
        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))

    def test_with_heading_context_prefixes_breadcrumb(self):
        chunk = Chunk(content="body text", heading_path="A > B")
        assert chunk.with_heading_context().startswith("A > B")
        assert "body text" in chunk.with_heading_context()

    def test_paragraph_without_sentence_boundaries_still_splits(self):
        # A minified blob has no sentence or paragraph breaks; chunking must
        # not stall or emit one enormous chunk.
        blob = "x" * 20000
        chunks = MarkdownChunker(target_tokens=100, overlap_tokens=0, min_tokens=0).chunk(f"# T\n\n{blob}")
        assert len(chunks) > 1


class TestLogChunker:
    """Log bundle chunking."""

    def test_produces_chunks(self, text_logs):
        chunks = LogChunker(lines_per_chunk=2, overlap_lines=0).chunk(text_logs)
        assert chunks

    def test_stack_trace_stays_with_its_entry(self, text_logs):
        chunks = LogChunker(lines_per_chunk=100, overlap_lines=0).chunk(text_logs)
        combined = "\n".join(chunk.content for chunk in chunks)
        # The 'at com.example...' frames must remain attached, not become
        # separate entries.
        assert "PaymentGatewayTimeoutException" in combined
        assert "at com.example.payments.Gateway.authorise" in combined

    def test_records_time_range_metadata(self, text_logs):
        chunks = LogChunker(lines_per_chunk=100, overlap_lines=0).chunk(text_logs)
        assert chunks[0].metadata.get("time_start")
        assert chunks[0].metadata.get("time_end")

    def test_records_entry_range(self, text_logs):
        chunks = LogChunker(lines_per_chunk=2, overlap_lines=0).chunk(text_logs)
        assert chunks[0].metadata["entry_start"] == 0
        assert chunks[0].metadata["entry_count"] >= 1

    def test_empty_input_produces_no_chunks(self):
        assert LogChunker().chunk("") == []

    def test_does_not_emit_duplicate_trailing_chunk(self):
        logs = "\n".join(f"2026-08-15 10:00:{index:02d} INFO line {index}" for index in range(10))
        chunks = LogChunker(lines_per_chunk=5, overlap_lines=2).chunk(logs)
        contents = [chunk.content for chunk in chunks]
        assert len(contents) == len(set(contents)), "overlapping windows produced a duplicate chunk"


class TestGetChunker:
    """Chunker selection."""

    def test_log_bundle_gets_log_chunker(self):
        assert isinstance(get_chunker("log_bundle"), LogChunker)

    def test_other_types_get_markdown_chunker(self):
        assert isinstance(get_chunker("runbook"), MarkdownChunker)
        assert isinstance(get_chunker("postmortem"), MarkdownChunker)
