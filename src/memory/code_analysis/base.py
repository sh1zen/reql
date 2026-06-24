"""Shared Tree-sitter parser and extractor primitives."""
from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Any

from ..artifacts.models import SourceArtifact
from ..domain.ids import stable_id
from .models import (
    CodeCall,
    CodeImport,
    CodeModule,
    CodeParseResult,
    CodeReference,
    CodeSymbol,
    CodeText,
    CodeTextKind,
    SymbolKind,
)
from .catalog import CODE_LANGUAGE_CATALOG, language_key, load_tree_sitter_language, tree_sitter_language_key


class TreeSitterCodeParser:
    parser_name = "tree_sitter"
    parser_version = "tree-sitter-code-v11"

    def supports(self, language: str) -> bool:
        return language_key(language) in CODE_LANGUAGE_CATALOG

    def parse_artifact(self, artifact: SourceArtifact, text: str) -> CodeParseResult:
        language = _language(artifact)
        parser_language = tree_sitter_language_key(artifact.path, language)
        source = text.removeprefix("\ufeff").encode("utf-8", errors="replace")
        parser = _parser_for(parser_language)
        tree = parser.parse(source)
        root = tree.root_node
        if bool(getattr(root, "has_error", False)):
            return _empty_result(artifact, language, f"Tree-sitter syntax error in {artifact.relative_path}")
        return _extractor_for(artifact, source, language, parser_language).extract(root)


class TreeSitterExtractorBase(ABC):
    """Minimal shared extraction state and result assembly."""

    parser_name = TreeSitterCodeParser.parser_name
    parser_version = TreeSitterCodeParser.parser_version

    def __init__(self, artifact: SourceArtifact, source: bytes, language: str, language_key: str) -> None:
        self.artifact = artifact
        self.source = source
        self.language = language
        self.language_key = language_key
        self.module = CodeModule(
            id=stable_id("module", artifact.id),
            artifact_id=artifact.id,
            name=_module_name(artifact.relative_path),
            path=artifact.relative_path,
            language=language,
            metadata={"tree_sitter": True},
        )
        self.symbols: list[CodeSymbol] = []
        self.imports: list[CodeImport] = []
        self.calls: list[CodeCall] = []
        self.references: list[CodeReference] = []
        self.comments: list[CodeText] = []
        self.docstrings: list[CodeText] = []

    def extract(self, root: Any) -> CodeParseResult:
        self._walk_root(root)
        return CodeParseResult(
            module=self.module,
            symbols=self.symbols,
            imports=self.imports,
            calls=self.calls,
            references=self.references,
            classes=[symbol for symbol in self.symbols if symbol.kind == "class"],
            functions=[symbol for symbol in self.symbols if symbol.kind in {"function", "async_function"}],
            methods=[symbol for symbol in self.symbols if symbol.kind in {"method", "async_method"}],
            comments=self.comments,
            docstrings=self.docstrings,
            errors=[],
            parser_name=self.parser_name,
            parser_version=self.parser_version,
        )

    @abstractmethod
    def _walk_root(self, root: Any) -> None:
        """Populate parse collections from the Tree-sitter root node."""


def _extractor_for(
    artifact: SourceArtifact,
    source: bytes,
    language: str,
    language_key_: str,
) -> TreeSitterExtractorBase:
    from .factory import extractor_for

    return extractor_for(artifact, source, language, language_key_)


@lru_cache(maxsize=64)
def _parser_for(language: str) -> Any:
    tree_sitter = import_module("tree_sitter")
    language_obj = load_tree_sitter_language(tree_sitter, language)
    parser_cls = getattr(tree_sitter, "Parser")
    try:
        return parser_cls(language_obj)
    except TypeError:
        parser = parser_cls()
        try:
            parser.language = language_obj
        except AttributeError:
            parser.set_language(language_obj)
        return parser


def _language(artifact: SourceArtifact) -> str:
    key = language_key(artifact.language or Path(artifact.path).suffix)
    return CODE_LANGUAGE_CATALOG.get(key, {}).get("display", key)


def _empty_result(artifact: SourceArtifact, language: str, error: str) -> CodeParseResult:
    module = CodeModule(id=stable_id("module", artifact.id), artifact_id=artifact.id, name=_module_name(artifact.relative_path), path=artifact.relative_path, language=language, metadata={"tree_sitter": True})
    return CodeParseResult(module=module, symbols=[], imports=[], calls=[], references=[], classes=[], functions=[], methods=[], comments=[], docstrings=[], errors=[error], parser_name=TreeSitterCodeParser.parser_name, parser_version=TreeSitterCodeParser.parser_version)


def _symbol(
    artifact: SourceArtifact,
    kind: SymbolKind,
    parent: str,
    name: str,
    line: int,
    end_line: int | None,
    *,
    decorators: list[str] | None = None,
    args: list[str] | None = None,
    bases: list[str] | None = None,
    returns: str | None = None,
) -> CodeSymbol:
    qualified = f"{parent}.{name}" if parent else name
    return CodeSymbol(id=stable_id("code-symbol", artifact.id, kind, qualified), artifact_id=artifact.id, kind=kind, name=name, qualified_name=qualified, start_line=line, end_line=end_line, parent_qualified_name=parent, decorators=decorators or [], bases=bases or [], args=args or [], returns=returns)


def _import(
    artifact: SourceArtifact,
    module: str | None,
    name: str | None,
    alias: str | None,
    line: int,
    raw: str,
    *,
    level: int = 0,
    metadata: dict[str, Any] | None = None,
) -> CodeImport:
    return CodeImport(
        id=stable_id("import", artifact.id, line, module or "", name or "", alias or "", level),
        artifact_id=artifact.id,
        module=module,
        name=name,
        alias=alias,
        level=level,
        line=line,
        raw=raw,
        metadata=metadata or {},
    )


def _text(artifact: SourceArtifact, owner: str | None, text: str, line: int, end_line: int | None, kind: CodeTextKind) -> CodeText:
    return CodeText(id=stable_id(kind, artifact.id, owner or "", line, text), artifact_id=artifact.id, owner=owner, text=text, start_line=line, end_line=end_line, kind=kind)


def _reference(artifact: SourceArtifact, owner: str | None, name: str, line: int, column: int, access: str) -> CodeReference:
    return CodeReference(id=stable_id("reference", artifact.id, owner or "", name, line, column, access), artifact_id=artifact.id, owner=owner, name=name, line=line, column=column, access=access)  # type: ignore[arg-type]


def _children(node: Any | None) -> list[Any]:
    if node is None:
        return []
    children = getattr(node, "children", None)
    if children is not None:
        return list(children)
    return list(getattr(node, "named_children", None) or [])


def _named_children(node: Any | None) -> list[Any]:
    if node is None:
        return []
    return list(getattr(node, "named_children", None) or [])


def _field(node: Any | None, name: str) -> Any | None:
    if node is None:
        return None
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if child_by_field_name is None:
        return None
    try:
        return child_by_field_name(name)
    except Exception:
        return None


def _node_text(source: bytes, node: Any | None) -> str:
    if node is None:
        return ""
    return source[int(getattr(node, "start_byte", 0)) : int(getattr(node, "end_byte", 0))].decode("utf-8", errors="replace")


def _line(node: Any) -> int:
    point = getattr(node, "start_point", (0, 0))
    return int(point[0] if isinstance(point, tuple) else point.row) + 1


def _end_line(node: Any) -> int:
    point = getattr(node, "end_point", (0, 0))
    return int(point[0] if isinstance(point, tuple) else point.row) + 1


def _column(node: Any) -> int:
    point = getattr(node, "start_point", (0, 0))
    return int(point[1] if isinstance(point, tuple) else point.column)


def _parent(module: CodeModule, scope: list[CodeSymbol]) -> str:
    return scope[-1].qualified_name if scope else module.name


def _owner(scope: list[CodeSymbol]) -> str | None:
    return scope[-1].qualified_name if scope else None


def _call_owner(scope: list[CodeSymbol]) -> str | None:
    for symbol in reversed(scope):
        if symbol.kind in {"function", "async_function", "method", "async_method"}:
            return symbol.qualified_name
    return None


def _first_named_child(node: Any | None) -> Any | None:
    children = _named_children(node)
    return children[0] if children else None


def _last_named_child(node: Any | None) -> Any | None:
    children = _named_children(node)
    return children[-1] if children else None


def _first_child_of_type(node: Any | None, node_types: set[str]) -> Any | None:
    for child in _children(node):
        if str(getattr(child, "type", "")) in node_types:
            return child
    return None


def _has_direct_child(node: Any, child_type: str) -> bool:
    return any(str(getattr(child, "type", "")) == child_type for child in _children(node))


def _clean_type_text(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("->"):
        value = value[2:].strip()
    if value.startswith(":"):
        value = value[1:].strip()
    return value or None


def _params(source: bytes, node: Any | None) -> list[str]:
    names: list[str] = []
    for child in _named_children(node):
        name = _param_name(source, child)
        if name:
            names.append(name)
    return names


def _param_name(source: bytes, node: Any) -> str | None:
    node_type = str(getattr(node, "type", ""))
    if node_type in {"identifier", "property_identifier"}:
        return _node_text(source, node)
    pattern = _field(node, "pattern")
    if pattern is not None:
        return _param_name(source, pattern)
    for child in _named_children(node):
        name = _param_name(source, child)
        if name:
            return name
    return None


def _call_target(source: bytes, node: Any | None) -> str | None:
    if node is None:
        return None
    raw = _node_text(source, node).strip()
    raw = raw.replace("?.", ".")
    raw = "".join(raw.split())
    return raw or None


def _string_literal(raw: str) -> str | None:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in {"'", '"', "`"} and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw or None


def _last_quoted(raw: str) -> str | None:
    for quote in ("'", '"'):
        end = raw.rfind(quote)
        if end <= 0:
            continue
        start = raw.rfind(quote, 0, end)
        if start >= 0:
            return raw[start + 1 : end]
    return None


_SYMBOL_NAME_STOP_WORDS = {
    "class",
    "def",
    "enum",
    "fn",
    "func",
    "function",
    "import",
    "include",
    "interface",
    "package",
    "struct",
    "trait",
    "type",
    "use",
}


def _valid_symbol_name(name: str) -> bool:
    value = name.strip()
    if not value or value.casefold() in _SYMBOL_NAME_STOP_WORDS:
        return False
    return any(char.isalpha() or char == "_" for char in value)


def _module_name(relative_path: str) -> str:
    path = Path(relative_path)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or path.stem
