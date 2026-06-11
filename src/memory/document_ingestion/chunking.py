"""Paragraph-oriented chunking helpers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParagraphChunk:
    text: str
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int


def paragraph_chunks(text: str, *, max_chars: int = 3000) -> list[ParagraphChunk]:
    chunks: list[ParagraphChunk] = []
    lines = text.splitlines()
    offset = 0
    paragraph: list[str] = []
    paragraph_start_line = 1
    paragraph_start_offset = 0

    for index, line in enumerate(lines, start=1):
        line_start = offset
        offset += len(line) + 1
        if not line.strip():
            _flush_paragraph(chunks, paragraph, paragraph_start_line, index - 1, paragraph_start_offset, line_start, max_chars)
            paragraph = []
            paragraph_start_line = index + 1
            paragraph_start_offset = offset
            continue
        if not paragraph:
            paragraph_start_line = index
            paragraph_start_offset = line_start
        paragraph.append(line)

    end_offset = len(text)
    _flush_paragraph(chunks, paragraph, paragraph_start_line, len(lines) or 1, paragraph_start_offset, end_offset, max_chars)
    return chunks


def _flush_paragraph(
    chunks: list[ParagraphChunk],
    paragraph: list[str],
    start_line: int,
    end_line: int,
    start_offset: int,
    end_offset: int,
    max_chars: int,
) -> None:
    text = "\n".join(paragraph).strip()
    if not text:
        return
    if len(text) <= max_chars:
        chunks.append(ParagraphChunk(text, start_line, end_line, start_offset, end_offset))
        return
    cursor = 0
    current_start = start_offset
    while cursor < len(text):
        piece = text[cursor : cursor + max_chars].strip()
        if piece:
            piece_end = current_start + len(piece)
            chunks.append(ParagraphChunk(piece, start_line, end_line, current_start, piece_end))
            current_start = piece_end
        cursor += max_chars
