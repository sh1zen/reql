"""Shared metadata and hashing helpers for document parsers."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from ..domain.ids import stable_id
from .models import DocumentFragment, FragmentType


def content_hash(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", errors="replace"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def structural_hash(
    fragment_type: str,
    index: int,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    start_offset: int | None = None,
    end_offset: int | None = None,
    page_number: int | None = None,
    section_path: str | None = None,
) -> str:
    if fragment_type == "code_block" and section_path:
        return content_hash(fragment_type, section_path)
    return content_hash(fragment_type, index, start_line, end_line, page_number, section_path)


def make_fragment(
    *,
    artifact_id: str,
    fragment_type: FragmentType,
    text: str,
    index: int,
    start_line: int | None = None,
    end_line: int | None = None,
    start_offset: int | None = None,
    end_offset: int | None = None,
    page_number: int | None = None,
    section_path: str | None = None,
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> DocumentFragment:
    structure = structural_hash(
        fragment_type,
        index,
        start_line=start_line,
        end_line=end_line,
        start_offset=start_offset,
        end_offset=end_offset,
        page_number=page_number,
        section_path=section_path,
    )
    props = dict(metadata or {})
    props["structural_hash"] = structure
    props["fragment_index"] = index
    return DocumentFragment(
        id=stable_id("fragment", artifact_id, structure),
        artifact_id=artifact_id,
        fragment_type=fragment_type,
        text=text,
        start_line=start_line,
        end_line=end_line,
        start_offset=start_offset,
        end_offset=end_offset,
        page_number=page_number,
        section_path=section_path,
        hash=content_hash(fragment_type, section_path or "", page_number or "", text),
        confidence=max(0.0, min(1.0, confidence)),
        metadata=props,
    )


def basic_file_metadata(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    stat = target.stat()
    mime, _ = mimetypes.guess_type(str(target))
    return {
        "filename": target.name,
        "suffix": target.suffix.lower(),
        "size_bytes": int(stat.st_size),
        "mtime": float(stat.st_mtime),
        "mime_type": mime,
    }


def line_offsets(text: str) -> list[int]:
    offsets = []
    current = 0
    for line in text.splitlines(keepends=True):
        offsets.append(current)
        current += len(line)
    if not offsets:
        offsets.append(0)
    return offsets
