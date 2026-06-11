"""Models produced by code parsers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SymbolKind = Literal["module", "class", "function", "method", "async_function", "async_method", "variable", "decorator", "external"]
CodeTextKind = Literal["comment", "docstring"]
ReferenceAccess = Literal["read", "write", "raise", "return"]


@dataclass(slots=True)
class CodeModule:
    id: str
    artifact_id: str
    name: str
    path: str
    language: str
    docstring: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodeSymbol:
    id: str
    artifact_id: str
    kind: SymbolKind
    name: str
    qualified_name: str
    start_line: int | None = None
    end_line: int | None = None
    parent_qualified_name: str | None = None
    decorators: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    returns: str | None = None
    docstring: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodeImport:
    id: str
    artifact_id: str
    module: str | None
    name: str | None
    alias: str | None
    level: int = 0
    line: int | None = None
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodeCall:
    id: str
    artifact_id: str
    caller: str | None
    target: str
    line: int | None = None
    column: int | None = None
    resolved_symbol_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodeText:
    id: str
    artifact_id: str
    owner: str | None
    text: str
    start_line: int | None
    end_line: int | None
    kind: CodeTextKind
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodeReference:
    id: str
    artifact_id: str
    owner: str | None
    name: str
    line: int | None = None
    column: int | None = None
    access: ReferenceAccess | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodeParseResult:
    module: CodeModule
    symbols: list[CodeSymbol]
    imports: list[CodeImport]
    calls: list[CodeCall]
    references: list[CodeReference]
    classes: list[CodeSymbol]
    functions: list[CodeSymbol]
    methods: list[CodeSymbol]
    comments: list[CodeText]
    docstrings: list[CodeText]
    errors: list[str]
    parser_name: str
    parser_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module.to_dict(),
            "symbols": [item.to_dict() for item in self.symbols],
            "imports": [item.to_dict() for item in self.imports],
            "calls": [item.to_dict() for item in self.calls],
            "references": [item.to_dict() for item in self.references],
            "classes": [item.to_dict() for item in self.classes],
            "functions": [item.to_dict() for item in self.functions],
            "methods": [item.to_dict() for item in self.methods],
            "comments": [item.to_dict() for item in self.comments],
            "docstrings": [item.to_dict() for item in self.docstrings],
            "errors": list(self.errors),
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
        }
