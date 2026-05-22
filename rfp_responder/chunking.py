from __future__ import annotations

"""Text chunking with overlap, respecting paragraph and sentence boundaries."""

import re
from dataclasses import dataclass
from typing import Optional

from .parsers import ParsedChunk


@dataclass
class TextChunk:
    """A chunk ready for embedding."""
    text: str
    source: str
    page: Optional[int] = None
    section: Optional[str] = None
    chunk_index: int = 0


# Approx tokens → chars: 1 token ≈ 4 chars
_CHUNK_SIZE_CHARS  = 2_000   # ~500 tokens
_OVERLAP_CHARS     = 400     # ~100 tokens
_MIN_CHUNK_CHARS   = 100


def chunk_parsed(parsed: ParsedChunk,
                 chunk_size: int = _CHUNK_SIZE_CHARS,
                 overlap: int = _OVERLAP_CHARS) -> list[TextChunk]:
    """Split a ParsedChunk into overlapping TextChunks."""
    text = parsed.text.strip()
    if len(text) < _MIN_CHUNK_CHARS:
        return []

    segments = _split_text(text, chunk_size, overlap)
    return [
        TextChunk(
            text=seg,
            source=parsed.source,
            page=parsed.page,
            section=parsed.section,
            chunk_index=i,
        )
        for i, seg in enumerate(segments)
        if len(seg.strip()) >= _MIN_CHUNK_CHARS
    ]


def chunk_corpus(parsed_chunks: list[ParsedChunk],
                 chunk_size: int = _CHUNK_SIZE_CHARS,
                 overlap: int = _OVERLAP_CHARS) -> list[TextChunk]:
    """Chunk an entire parsed corpus."""
    result: list[TextChunk] = []
    for pc in parsed_chunks:
        result.extend(chunk_parsed(pc, chunk_size, overlap))
    return result


# ---------------------------------------------------------------------------
# Internal splitting helpers
# ---------------------------------------------------------------------------

def _split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text preferring paragraph boundaries, then sentence boundaries."""
    if len(text) <= chunk_size:
        return [text]

    # 1. Try paragraph splits
    paragraphs = re.split(r"\n\n+", text)
    if len(paragraphs) > 1:
        valid_segs = []
        for p in paragraphs:
            if len(p) > chunk_size:
                valid_segs.extend(_split_text(p, chunk_size, overlap))
            else:
                valid_segs.append(p)
        return _merge_segments(valid_segs, chunk_size, overlap)

    # 2. Try sentence splits
    sentences = re.split(r"(?<=\. )", text)
    if len(sentences) > 1:
        valid_segs = []
        for s in sentences:
            if len(s) > chunk_size:
                valid_segs.extend(_hard_split(s, chunk_size, overlap))
            else:
                valid_segs.append(s)
        return _merge_segments(valid_segs, chunk_size, overlap)

    # 3. Hard split by chars
    return _hard_split(text, chunk_size, overlap)


def _merge_segments(segments: list[str], chunk_size: int, overlap: int) -> list[str]:
    """Greedily merge segments into chunks of <= chunk_size, with overlap."""
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for seg in segments:
        seg_len = len(seg)
        # Flush if we exceed chunk size AND we have more than just the overlap chunk
        if current_len + seg_len > chunk_size and len(current_parts) >= 1:
            # If current_parts only has 1 item and adding seg makes it too big, we still must flush 
            # if that 1 item isn't just an overlap from a previous flush, but to be safe we flush if current_len > overlap.
            if current_len > overlap or len(current_parts) > 1:
                chunk_text = "\n\n".join(current_parts)
                chunks.append(chunk_text)
                overlap_text = chunk_text[-overlap:] if len(chunk_text) > overlap else chunk_text
                current_parts = [overlap_text]
                current_len = len(overlap_text)

        current_parts.append(seg)
        current_len += seg_len

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return chunks or [segments[0]]


def _hard_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Byte-level hard split."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks
