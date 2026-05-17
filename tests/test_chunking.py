"""Tests for text chunking."""
from __future__ import annotations

import pytest

from rfp_responder.chunking import chunk_parsed, _split_text
from rfp_responder.parsers import ParsedChunk


def _make_chunk(text: str) -> ParsedChunk:
    return ParsedChunk(text=text, source="test.txt")


class TestChunkParsed:
    def test_short_text_returns_empty(self):
        """Texts shorter than MIN_CHUNK_CHARS should produce no chunks."""
        result = chunk_parsed(_make_chunk("short"), chunk_size=3200, overlap=800)
        assert result == []

    def test_single_chunk_within_limit(self):
        text = "A " * 500  # ~1000 chars — below 3200
        result = chunk_parsed(_make_chunk(text), chunk_size=3200, overlap=800)
        assert len(result) == 1
        assert result[0].text == text.strip()

    def test_long_text_produces_multiple_chunks(self):
        # ~10000 chars → should produce multiple chunks with chunk_size=3200
        text = "Lorem ipsum dolor sit amet. " * 400
        result = chunk_parsed(_make_chunk(text), chunk_size=3200, overlap=800)
        assert len(result) > 1

    def test_paragraph_break_preferred(self):
        """Chunks should prefer splitting on double-newlines."""
        para_a = "This is paragraph A. " * 80  # ~1760 chars
        para_b = "This is paragraph B. " * 80
        para_c = "This is paragraph C. " * 80
        text = para_a + "\n\n" + para_b + "\n\n" + para_c
        result = chunk_parsed(_make_chunk(text), chunk_size=3200, overlap=800)
        assert all(len(r.text) >= 100 for r in result)

    def test_very_long_single_paragraph_hard_splits(self):
        """A single paragraph with no breaks should still be split."""
        text = "x" * 10_000
        result = chunk_parsed(_make_chunk(text), chunk_size=3200, overlap=800)
        assert len(result) > 1

    def test_no_paragraph_breaks_sentences(self):
        """Text with only sentence breaks should be chunked reasonably."""
        text = "".join(f"This is sentence number {i:03d}. " for i in range(200))
        result = chunk_parsed(_make_chunk(text), chunk_size=3200, overlap=800)
        assert len(result) > 1

    def test_metadata_preserved(self):
        parsed = ParsedChunk(text="A" * 500, source="doc.pdf", page=3, section="Intro")
        result = chunk_parsed(parsed)
        for chunk in result:
            assert chunk.source == "doc.pdf"
            assert chunk.page == 3
            assert chunk.section == "Intro"
