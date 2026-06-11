"""Document parser result models."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FragmentType = Literal[
    "heading",
    "paragraph",
    "list",
    "code_block",
    "table",
    "link",
    "page",
    "metadata",
    "raw_text",
]


@dataclass(slots=True)
class DocumentFragment:
    id: str
    artifact_id: str
    fragment_type: FragmentType
    text: str
    start_line: int | None
    end_line: int | None
    start_offset: int | None
    end_offset: int | None
    page_number: int | None
    section_path: str | None
    hash: str
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DocumentParseResult:
    title: str | None
    metadata: dict[str, Any]
    fragments: list[DocumentFragment]
    links: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    errors: list[str]
    parser_name: str
    parser_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "metadata": dict(self.metadata),
            "fragments": [fragment.to_dict() for fragment in self.fragments],
            "links": list(self.links),
            "tables": list(self.tables),
            "errors": list(self.errors),
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
        }
