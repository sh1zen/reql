"""Markdown parser with lightweight structural extraction."""
from __future__ import annotations

import re

from ...artifacts.models import SourceArtifact
from ..base import BaseDocumentParser
from ..metadata import line_offsets, make_fragment
from ..models import DocumentFragment, DocumentParseResult

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^(```+|~~~+)\s*([A-Za-z0-9_+.-]+)?\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")


class MarkdownParser(BaseDocumentParser):
    parser_name = "markdown"
    parser_version = "markdown-v1"
    artifact_types = frozenset({"markdown"})
    languages = frozenset({"markdown"})

    def parse(self, artifact: SourceArtifact, content: bytes) -> DocumentParseResult:
        text = self.decode_text(content)
        lines = text.splitlines()
        offsets = line_offsets(text)
        metadata = self.base_metadata(artifact)
        fragments: list[DocumentFragment] = []
        links: list[dict[str, object]] = []
        tables: list[dict[str, object]] = []
        heading_stack: list[tuple[int, str, str]] = []
        title = self.title_from_path(artifact)
        current_heading_id: str | None = None
        index = 0
        i = 0

        while i < len(lines):
            line = lines[i]
            line_number = i + 1
            if not line.strip():
                i += 1
                continue

            heading = HEADING_RE.match(line)
            if heading:
                level = len(heading.group(1))
                heading_text = heading.group(2).strip().strip("#").strip()
                if level == 1 and title == self.title_from_path(artifact):
                    title = heading_text
                heading_stack = [item for item in heading_stack if item[0] < level]
                section_path = _section_path(title, heading_stack, heading_text)
                fragment = make_fragment(
                    artifact_id=artifact.id,
                    fragment_type="heading",
                    text=heading_text,
                    index=index,
                    start_line=line_number,
                    end_line=line_number,
                    start_offset=_offset(offsets, i),
                    end_offset=_offset(offsets, i) + len(line),
                    section_path=section_path,
                    metadata={"level": level, "parser": self.parser_name},
                )
                fragments.append(fragment)
                current_heading_id = fragment.id
                heading_stack.append((level, heading_text, fragment.id))
                index += 1
                i += 1
                continue

            fence = FENCE_RE.match(line)
            if fence:
                language = fence.group(2)
                fence_token = fence.group(1)
                start = i
                i += 1
                body: list[str] = []
                while i < len(lines) and not lines[i].startswith(fence_token[:3]):
                    body.append(lines[i])
                    i += 1
                end = i if i < len(lines) else max(start, i - 1)
                if i < len(lines):
                    i += 1
                fragment = make_fragment(
                    artifact_id=artifact.id,
                    fragment_type="code_block",
                    text="\n".join(body),
                    index=index,
                    start_line=start + 1,
                    end_line=end + 1,
                    start_offset=_offset(offsets, start),
                    end_offset=_line_end(offsets, lines, end),
                    section_path=_current_section(title, heading_stack),
                    metadata={"language": language, "parser": self.parser_name, "parent_heading_id": current_heading_id},
                )
                fragments.append(fragment)
                index += 1
                continue

            if _is_table_start(lines, i):
                start = i
                table_lines = [lines[i], lines[i + 1]]
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    table_lines.append(lines[i])
                    i += 1
                fragment = make_fragment(
                    artifact_id=artifact.id,
                    fragment_type="table",
                    text="\n".join(table_lines),
                    index=index,
                    start_line=start + 1,
                    end_line=start + len(table_lines),
                    start_offset=_offset(offsets, start),
                    end_offset=_line_end(offsets, lines, start + len(table_lines) - 1),
                    section_path=_current_section(title, heading_stack),
                    metadata={"parser": self.parser_name, "parent_heading_id": current_heading_id},
                )
                fragments.append(fragment)
                tables.append({"fragment_id": fragment.id, "rows": max(0, len(table_lines) - 2), "columns": _table_columns(table_lines[0])})
                index += 1
                continue

            if LIST_RE.match(line):
                start = i
                items: list[str] = []
                while i < len(lines) and LIST_RE.match(lines[i]):
                    items.append(lines[i])
                    i += 1
                fragment = make_fragment(
                    artifact_id=artifact.id,
                    fragment_type="list",
                    text="\n".join(items),
                    index=index,
                    start_line=start + 1,
                    end_line=start + len(items),
                    start_offset=_offset(offsets, start),
                    end_offset=_line_end(offsets, lines, start + len(items) - 1),
                    section_path=_current_section(title, heading_stack),
                    metadata={"parser": self.parser_name, "parent_heading_id": current_heading_id},
                )
                fragments.append(fragment)
                _collect_references(fragment, links)
                index += 1
                continue

            start = i
            paragraph_lines = []
            while i < len(lines) and lines[i].strip() and not _is_block_start(lines, i):
                paragraph_lines.append(lines[i])
                i += 1
            fragment = make_fragment(
                artifact_id=artifact.id,
                fragment_type="paragraph",
                text="\n".join(paragraph_lines).strip(),
                index=index,
                start_line=start + 1,
                end_line=start + len(paragraph_lines),
                start_offset=_offset(offsets, start),
                end_offset=_line_end(offsets, lines, start + len(paragraph_lines) - 1),
                section_path=_current_section(title, heading_stack),
                metadata={"parser": self.parser_name, "parent_heading_id": current_heading_id},
            )
            fragments.append(fragment)
            _collect_references(fragment, links)
            index += 1

        metadata.update({"title": title})
        return DocumentParseResult(
            title=title,
            metadata=metadata,
            fragments=fragments,
            links=links,
            tables=tables,
            errors=[],
            parser_name=self.parser_name,
            parser_version=self.parser_version,
        )


def _collect_references(fragment: DocumentFragment, links: list[dict[str, object]]) -> None:
    for match in LINK_RE.finditer(fragment.text):
        links.append({"source_fragment_id": fragment.id, "text": match.group(1), "uri": match.group(2)})


def _is_block_start(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(HEADING_RE.match(line) or FENCE_RE.match(line) or LIST_RE.match(line) or _is_table_start(lines, index))


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    if "|" not in header or "|" not in separator:
        return False
    cells = [cell.strip() for cell in separator.strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _table_columns(header: str) -> int:
    return len([cell for cell in header.strip().strip("|").split("|") if cell.strip()])


def _section_path(title: str, stack: list[tuple[int, str, str]], heading_text: str) -> str:
    names = [item[1] for item in stack]
    if not names or names[0] != title:
        names.insert(0, title)
    if not names or names[-1] != heading_text:
        names.append(heading_text)
    return " > ".join(name for name in names if name)


def _current_section(title: str, stack: list[tuple[int, str, str]]) -> str:
    names = [item[1] for item in stack]
    if not names or names[0] != title:
        names.insert(0, title)
    return " > ".join(name for name in names if name)


def _offset(offsets: list[int], index: int) -> int:
    if index < len(offsets):
        return offsets[index]
    return offsets[-1] if offsets else 0


def _line_end(offsets: list[int], lines: list[str], index: int) -> int:
    if index < 0:
        return 0
    return _offset(offsets, index) + len(lines[index])
