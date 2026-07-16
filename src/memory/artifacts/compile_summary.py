"""Deterministic post-compile summaries for coding-agent verification."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ..domain.models import MemoryNode
from ..storage.graph_store import GraphStore
from .delta import GraphDelta
from .revision import ProjectRevision

CODE_SYMBOL_TYPES = {"Module", "Function", "Class", "Interface", "Method", "Endpoint"}
ASSOCIATION_EDGE_TYPES = {
    "CALLS",
    "IMPORTS",
    "IMPORTS_FROM",
    "INSTANTIATES",
    "OVERRIDES",
    "READS",
    "REFERENCES",
    "RE_EXPORTS",
    "RETURNS",
    "TESTS",
}
MAX_ASSOCIATION_SEEDS = 250
MAX_LEXICAL_TESTS = 8
LEXICAL_ASSOCIATION_NODE_TYPES = {"Module", "Function", "Class", "Method", "Test", "SourceFragment"}


@dataclass(frozen=True, slots=True)
class UpdatedSymbol:
    id: str
    type: str
    name: str
    relative_path: str
    status: str
    line_start: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "relative_path": self.relative_path,
            "status": self.status,
            "line_start": self.line_start,
        }


@dataclass(frozen=True, slots=True)
class AssociatedTest:
    path: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass(slots=True)
class CompilationSummary:
    changed_files: list[dict[str, object]] = field(default_factory=list)
    updated_symbols: list[UpdatedSymbol] = field(default_factory=list)
    associated_tests: list[AssociatedTest] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "changed_files": [dict(item) for item in self.changed_files],
            "updated_symbols": [item.to_dict() for item in self.updated_symbols],
            "associated_tests": [item.to_dict() for item in self.associated_tests],
        }


def build_compilation_summary(
    store: GraphStore,
    *,
    revision: ProjectRevision | None,
    delta: GraphDelta,
) -> CompilationSummary:
    """Build a summary from the revision and the graph delta just persisted."""
    changed_files = [change.to_dict() for change in revision.changes] if revision is not None else []
    changed_paths = {_normalize_path(str(item["path"])) for item in changed_files}
    node_status = _node_statuses(delta)
    nodes = store.get_nodes(sorted(node_status))
    changed_nodes = [node for node in nodes if _relative_path(node) in changed_paths]
    updated_symbols = [
        UpdatedSymbol(
            id=node.id,
            type=node.type,
            name=_symbol_name(node),
            relative_path=_relative_path(node),
            status=node_status[node.id],
            line_start=_line_start(node),
        )
        for node in changed_nodes
        if node.type in CODE_SYMBOL_TYPES
    ]
    updated_symbols.sort(key=lambda item: (item.relative_path, item.line_start or 0, item.type, item.name, item.id))
    associated_tests = _associated_tests(store, changed_paths=changed_paths, seed_nodes=changed_nodes)
    return CompilationSummary(
        changed_files=changed_files,
        updated_symbols=updated_symbols,
        associated_tests=associated_tests,
    )


def _node_statuses(delta: GraphDelta) -> dict[str, str]:
    statuses = {node_id: "added" for node_id in delta.added_nodes}
    statuses.update({node_id: "updated" for node_id in delta.updated_nodes})
    statuses.update({node_id: "archived" for node_id in delta.archived_nodes})
    return statuses


def _associated_tests(
    store: GraphStore,
    *,
    changed_paths: set[str],
    seed_nodes: list[MemoryNode],
) -> list[AssociatedTest]:
    reasons: dict[str, str] = {path: "changed test file" for path in changed_paths if _is_test_path(path)}
    source_nodes = [node for node in seed_nodes if not _is_test_path(_relative_path(node))]
    for node in sorted(source_nodes, key=lambda item: item.id)[:MAX_ASSOCIATION_SEEDS]:
        for _edge, neighbor in store.neighbors(
            node.id,
            direction="both",
            edge_types=ASSOCIATION_EDGE_TYPES,
            limit=200,
        ):
            path = _relative_path(neighbor)
            if _is_test_path(path):
                reasons.setdefault(path, "direct graph relationship")

    for path in sorted(changed_paths):
        for candidate in _conventional_test_paths(path):
            if candidate in reasons:
                continue
            if store.find_nodes_by_property("relative_path", candidate, status="active", limit=1, clone=False):
                reasons[candidate] = "matching test filename"

    terms = _association_terms(changed_paths, source_nodes)
    if terms:
        lexical_tests = 0
        for node, _score in store.lexical_search(
            " ".join(terms),
            top_k=100,
            node_types=LEXICAL_ASSOCIATION_NODE_TYPES,
        ):
            path = _relative_path(node)
            if not _is_test_path(path) or path in reasons or not _node_matches_terms(node, terms):
                continue
            reasons[path] = "changed symbol match"
            lexical_tests += 1
            if lexical_tests >= MAX_LEXICAL_TESTS:
                break

    return [AssociatedTest(path=path, reason=reasons[path]) for path in sorted(reasons)]


def _conventional_test_paths(path: str) -> tuple[str, ...]:
    normalized = _normalize_path(path)
    if _is_test_path(normalized):
        return ()
    stem = PurePosixPath(normalized).stem
    if not stem or stem == "__init__":
        return ()
    return (f"tests/test_{stem}.py", f"test/test_{stem}.py")


def _association_terms(changed_paths: set[str], nodes: list[MemoryNode]) -> list[str]:
    terms = {PurePosixPath(path).stem for path in changed_paths if not _is_test_path(path)}
    for node in nodes:
        if node.type not in CODE_SYMBOL_TYPES:
            continue
        name = _symbol_name(node).rsplit(".", 1)[-1].strip("_")
        if len(name) >= 4 and not name.startswith("test_"):
            terms.add(name)
    return sorted(terms)[:40]


def _node_matches_terms(node: MemoryNode, terms: list[str]) -> bool:
    haystack = " ".join(
        (
            _relative_path(node),
            _symbol_name(node),
            node.label,
            node.text,
        )
    ).casefold()
    return any(term.casefold() in haystack for term in terms)


def _relative_path(node: MemoryNode) -> str:
    return _normalize_path(str(node.properties.get("relative_path") or node.properties.get("source_file") or ""))


def _symbol_name(node: MemoryNode) -> str:
    properties: dict[str, Any] = node.properties
    return str(properties.get("qualified_name") or properties.get("name") or node.label or node.id)


def _line_start(node: MemoryNode) -> int | None:
    value = node.properties.get("line_start", node.properties.get("start_line"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _is_test_path(path: str) -> bool:
    normalized = _normalize_path(path)
    return normalized.startswith("tests/") or normalized.startswith("test/")
