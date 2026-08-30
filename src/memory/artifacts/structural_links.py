"""Project-wide structural code relations derived after artifact parsing."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from time import perf_counter
from typing import Iterable

from ..diagnostics import PerformanceLogger
from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore
from .models import ArtifactCompilationResult

STRUCTURAL_LINKER = "project_structural_linker"
SYMBOL_TYPES = {"Class", "Interface", "Function", "Method", "Variable"}
CALLABLE_TYPES = {"Function", "Method", "Class", "Interface"}
STRUCTURAL_PROPERTY_NODE_TYPES = {"Module", "File", "Class", "Interface", "Function", "Method"}
STRUCTURAL_INPUT_NODE_TYPES = {*STRUCTURAL_PROPERTY_NODE_TYPES, "Import", "Variable"}
STRUCTURAL_EDGE_TYPES = {"INHERITS", "OVERRIDES", "WRAPS", "RE_EXPORTS"}


def refresh_project_structural_links(
    store: GraphStore,
    project_id: str,
    *,
    profile_logger: PerformanceLogger | None = None,
) -> ArtifactCompilationResult:
    """Resolve re-exports, overrides, and thin wrappers across artifact boundaries."""
    result = ArtifactCompilationResult(artifact_id="*")
    load_started = perf_counter()
    nodes = [
        node
        for node_type in sorted(STRUCTURAL_INPUT_NODE_TYPES)
        for node in store.find_nodes_by_property(
            "project_id",
            project_id,
            type_=node_type,
            status="active",
            limit=100000,
            clone=False,
        )
    ]
    existing_edges = [
        edge
        for edge_type in sorted(STRUCTURAL_EDGE_TYPES)
        for edge in store.find_edges_by_property(
            "project_id",
            project_id,
            type_=edge_type,
            limit=100000,
            clone=False,
        )
        if edge.properties.get("extractor") == STRUCTURAL_LINKER
    ]
    _profile_span(profile_logger, "compile.structural.load", load_started, nodes=len(nodes), edges=len(existing_edges))

    derive_started = perf_counter()
    by_id = {node.id: node for node in nodes}
    by_path: dict[str, list[MemoryNode]] = defaultdict(list)
    imports_by_artifact: dict[str, list[MemoryNode]] = defaultdict(list)
    symbols_by_name: dict[str, list[MemoryNode]] = defaultdict(list)
    classes_by_qualified_name: dict[str, list[MemoryNode]] = defaultdict(list)
    methods_by_parent_name: dict[tuple[str, str], list[MemoryNode]] = defaultdict(list)
    modules_by_path: dict[str, list[MemoryNode]] = defaultdict(list)
    files_by_path: dict[str, list[MemoryNode]] = defaultdict(list)

    for node in nodes:
        path = _path(node)
        artifact_id = str(node.properties.get("artifact_id") or "")
        if path:
            by_path[path].append(node)
        if node.type == "Import" and artifact_id:
            imports_by_artifact[artifact_id].append(node)
        if node.type in SYMBOL_TYPES:
            name = str(node.properties.get("name") or "")
            qualified_name = str(node.properties.get("qualified_name") or "")
            if name:
                symbols_by_name[name].append(node)
            if node.type in {"Class", "Interface"} and qualified_name:
                classes_by_qualified_name[qualified_name].append(node)
            parent = str(node.properties.get("parent_qualified_name") or "")
            if node.type == "Method" and parent and name:
                methods_by_parent_name[(parent, name)].append(node)
        if node.type == "Module" and path:
            modules_by_path[path].append(node)
        if node.type == "File" and path:
            files_by_path[path].append(node)

    structural_nodes = [node for node in nodes if node.type in STRUCTURAL_PROPERTY_NODE_TYPES]
    desired_state = {node.id: _base_structural_state(node) for node in structural_nodes}

    new_edges: dict[str, MemoryEdge] = {}
    inheritance: dict[str, list[MemoryNode]] = defaultdict(list)
    resolved_bases: dict[tuple[str, str], str] = {}

    for class_node in (node for node in nodes if node.type in {"Class", "Interface"}):
        bases = class_node.properties.get("bases")
        if not isinstance(bases, list):
            continue
        owner_imports = imports_by_artifact.get(str(class_node.properties.get("artifact_id") or ""), [])
        for base_name in (str(value) for value in bases if value):
            base = _resolve_symbol_reference(
                class_node,
                base_name,
                owner_imports,
                by_path,
                symbols_by_name,
                allowed_types={"Class", "Interface"},
            )
            if base is None or base.id == class_node.id:
                continue
            inheritance[class_node.id].append(base)
            resolved_bases[(class_node.id, base_name)] = base.id
            edge = _structural_edge(class_node, base, "INHERITS", evidence=base_name, extra={"base": base_name})
            new_edges[edge.id] = edge

    for method in (node for node in nodes if node.type == "Method"):
        parent_name = str(method.properties.get("parent_qualified_name") or "")
        parent = _unique_node(classes_by_qualified_name.get(parent_name, []))
        if parent is None:
            continue
        overridden = _overridden_methods(
            parent,
            str(method.properties.get("name") or ""),
            inheritance,
            methods_by_parent_name,
        )
        if not overridden:
            continue
        _add_state_role(desired_state[method.id], "override")
        desired_state[method.id]["overrides"] = sorted(
            {str(target.properties.get("qualified_name") or target.label or target.id) for target in overridden}
        )
        for target in overridden:
            edge = _structural_edge(method, target, "OVERRIDES", evidence=str(method.properties.get("name") or ""))
            new_edges[edge.id] = edge

    for wrapper in (
        node
        for node in nodes
        if node.type in {"Function", "Method"} and "wrapper" in _roles(node)
    ):
        targets = wrapper.properties.get("wrapper_targets")
        if not isinstance(targets, list):
            continue
        owner_imports = imports_by_artifact.get(str(wrapper.properties.get("artifact_id") or ""), [])
        for target_name in (str(value) for value in targets if value):
            target = _resolve_symbol_reference(
                wrapper,
                target_name,
                owner_imports,
                by_path,
                symbols_by_name,
                allowed_types=CALLABLE_TYPES,
            )
            if target is None or target.id == wrapper.id:
                continue
            edge = _structural_edge(wrapper, target, "WRAPS", evidence=target_name, extra={"target": target_name})
            new_edges[edge.id] = edge

    for module in (node for node in nodes if node.type == "Module" and _is_initializer(_path(node))):
        artifact_id = str(module.properties.get("artifact_id") or "")
        imports = imports_by_artifact.get(artifact_id, [])
        file_node = _first(files_by_path.get(_path(module), []))
        _add_state_role(desired_state[module.id], "package-initializer")
        if file_node is not None:
            _add_state_role(desired_state[file_node.id], "package-initializer")
        if not imports:
            continue
        _add_state_role(desired_state[module.id], "re-export")
        if file_node is not None:
            _add_state_role(desired_state[file_node.id], "re-export")
        exported_labels: set[str] = set()
        for import_node in imports:
            targets = _re_export_targets(import_node, by_path, modules_by_path, files_by_path)
            for target in targets:
                exported_labels.add(str(target.properties.get("qualified_name") or target.properties.get("name") or target.label or target.id))
                edge = _structural_edge(
                    module,
                    target,
                    "RE_EXPORTS",
                    evidence=str(import_node.label or import_node.properties.get("raw") or "re-export"),
                    extra={
                        "import_id": import_node.id,
                        "module": import_node.properties.get("module"),
                        "name": import_node.properties.get("name"),
                        "alias": import_node.properties.get("alias"),
                    },
                )
                new_edges[edge.id] = edge
        desired_state[module.id]["re_exports"] = sorted(exported_labels)
        if file_node is not None:
            desired_state[file_node.id]["re_exports"] = sorted(exported_labels)

    pending_nodes = _changed_structural_nodes(structural_nodes, desired_state)
    existing_by_id = {edge.id: edge for edge in existing_edges}
    pending_edges = [
        edge
        for edge in new_edges.values()
        if not _structural_edges_equivalent(existing_by_id.get(edge.id), edge)
    ]
    _profile_span(
        profile_logger,
        "compile.structural.derive",
        derive_started,
        pending_nodes=len(pending_nodes),
        pending_edges=len(pending_edges),
    )

    persist_started = perf_counter()
    for node, created in store.batch_upsert_nodes(pending_nodes, return_clones=False):
        result.record_node(node.id, created=created)

    for edge in existing_edges:
        if edge.type != "INHERITS":
            continue
        base_name = str(edge.properties.get("base") or "")
        resolved_target = resolved_bases.get((edge.from_id, base_name))
        if resolved_target and edge.to_id != resolved_target:
            target = by_id.get(edge.to_id)
            if target is None or target.type not in {"Class", "Interface"}:
                _deactivate_edge(store, edge, result)

    for edge, created in store.batch_upsert_edges(pending_edges, return_clones=False):
        result.record_edge(edge.id, created=created)

    for edge in existing_edges:
        if edge.id in result.archived_edges:
            continue
        if edge.properties.get("extractor") != STRUCTURAL_LINKER or edge.id in new_edges:
            continue
        _deactivate_edge(store, edge, result)
    _profile_span(
        profile_logger,
        "compile.structural.persist",
        persist_started,
        updated_nodes=len(pending_nodes),
        updated_edges=len(pending_edges),
        archived_edges=len(result.archived_edges),
    )
    return result


def _base_structural_state(node: MemoryNode) -> dict[str, object]:
    roles = [role for role in _roles(node) if role not in {"override", "re-export", "re-exporter"}]
    path = _path(node)
    if node.type in {"Module", "File"} and not _is_initializer(path):
        roles = [role for role in roles if role != "package-initializer"]
    return {"semantic_roles": sorted(set(roles))}


def _changed_structural_nodes(
    nodes: list[MemoryNode],
    desired_state: dict[str, dict[str, object]],
) -> list[MemoryNode]:
    changed: list[MemoryNode] = []
    for node in nodes:
        properties = dict(node.properties)
        state = desired_state[node.id]
        properties["semantic_roles"] = list(state["semantic_roles"])
        for field in ("overrides", "re_exports"):
            if field in state:
                properties[field] = state[field]
            else:
                properties.pop(field, None)
        if properties != node.properties:
            changed.append(replace(node, properties=properties))
    return changed


def _structural_edges_equivalent(existing: MemoryEdge | None, candidate: MemoryEdge) -> bool:
    if existing is None:
        return False
    existing_properties = {
        key: value for key, value in existing.properties.items() if key not in {"created_at", "updated_at"}
    }
    candidate_properties = {
        key: value for key, value in candidate.properties.items() if key not in {"created_at", "updated_at"}
    }
    return (
        existing.from_id == candidate.from_id
        and existing.to_id == candidate.to_id
        and existing.type == candidate.type
        and existing.weight == candidate.weight
        and existing.confidence == candidate.confidence
        and existing.origin == candidate.origin
        and existing_properties == candidate_properties
    )


def _profile_span(
    logger: PerformanceLogger | None,
    name: str,
    started_at: float,
    **fields: object,
) -> None:
    if logger is None:
        return
    logger.event(
        name,
        category="span",
        duration_ms=round((perf_counter() - started_at) * 1000.0, 3),
        ok=True,
        **fields,
    )


def _overridden_methods(
    parent: MemoryNode,
    method_name: str,
    inheritance: dict[str, list[MemoryNode]],
    methods_by_parent_name: dict[tuple[str, str], list[MemoryNode]],
) -> list[MemoryNode]:
    """Return the nearest matching method in each inheritance branch."""

    overridden: list[MemoryNode] = []
    queue = deque(inheritance.get(parent.id, []))
    seen_classes: set[str] = set()
    while queue:
        base = queue.popleft()
        if base.id in seen_classes:
            continue
        seen_classes.add(base.id)
        base_name = str(base.properties.get("qualified_name") or "")
        matches = methods_by_parent_name.get((base_name, method_name), [])
        if matches:
            overridden.extend(matches)
        else:
            queue.extend(inheritance.get(base.id, []))
    return overridden


def _resolve_symbol_reference(
    owner: MemoryNode,
    reference: str,
    imports: list[MemoryNode],
    by_path: dict[str, list[MemoryNode]],
    symbols_by_name: dict[str, list[MemoryNode]],
    *,
    allowed_types: set[str],
) -> MemoryNode | None:
    clean = reference.strip()
    if not clean:
        return None
    root = clean.split(".", 1)[0].split("(", 1)[0]
    tail = clean.rsplit(".", 1)[-1].split("(", 1)[0]
    owner_path = _path(owner)
    owner_parent = str(owner.properties.get("parent_qualified_name") or "")

    local = [
        node
        for node in by_path.get(owner_path, [])
        if node.type in allowed_types
        and str(node.properties.get("name") or "") == tail
        and (not owner_parent or str(node.properties.get("parent_qualified_name") or "") == owner_parent)
    ]
    if len(local) == 1:
        return local[0]
    local = [
        node
        for node in by_path.get(owner_path, [])
        if node.type in allowed_types and str(node.properties.get("name") or "") == tail
    ]
    if len(local) == 1:
        return local[0]

    for import_node in imports:
        binding = str(import_node.properties.get("alias") or import_node.properties.get("name") or "")
        module_tail = str(import_node.properties.get("module") or "").rsplit(".", 1)[-1]
        if root not in {binding, module_tail}:
            continue
        target_path = _resolved_path(import_node)
        if not target_path:
            continue
        imported_name = str(import_node.properties.get("name") or "")
        desired = imported_name if binding == root and imported_name not in {"", "*"} and "." not in clean else tail
        candidates = [
            node
            for node in by_path.get(target_path, [])
            if node.type in allowed_types and str(node.properties.get("name") or "") == desired
        ]
        if len(candidates) == 1:
            return candidates[0]

    exact = [
        node
        for node in symbols_by_name.get(tail, [])
        if node.type in allowed_types and str(node.properties.get("qualified_name") or "") == clean
    ]
    if len(exact) == 1:
        return exact[0]
    candidates = [node for node in symbols_by_name.get(tail, []) if node.type in allowed_types]
    return candidates[0] if len(candidates) == 1 else None


def _re_export_targets(
    import_node: MemoryNode,
    by_path: dict[str, list[MemoryNode]],
    modules_by_path: dict[str, list[MemoryNode]],
    files_by_path: dict[str, list[MemoryNode]],
) -> list[MemoryNode]:
    target_path = _resolved_path(import_node)
    if not target_path:
        return []
    name = str(import_node.properties.get("name") or "")
    candidates = [node for node in by_path.get(target_path, []) if node.type in SYMBOL_TYPES]
    if name == "*":
        module_names = {
            str(node.properties.get("name") or node.label or "")
            for node in modules_by_path.get(target_path, [])
        }
        public = [
            node
            for node in candidates
            if not str(node.properties.get("name") or "").startswith("_")
            and str(node.properties.get("parent_qualified_name") or "") in module_names
        ]
        return _dedupe_nodes(public)
    if name:
        named = [node for node in candidates if str(node.properties.get("name") or "") == name]
        if named:
            return _dedupe_nodes(named)
    return _dedupe_nodes([*modules_by_path.get(target_path, []), *files_by_path.get(target_path, [])])


def _structural_edge(
    source: MemoryNode,
    target: MemoryNode,
    edge_type: str,
    *,
    evidence: str,
    extra: dict[str, object] | None = None,
) -> MemoryEdge:
    now = utcnow_iso()
    properties: dict[str, object] = {
        "project_id": source.properties.get("project_id"),
        "artifact_id": source.properties.get("artifact_id"),
        "source_id": source.id,
        "target_id": target.id,
        "type": edge_type,
        "confidence": 1.0,
        "source_file": _path(source),
        "line_start": source.properties.get("line_start") or source.properties.get("start_line"),
        "line_end": source.properties.get("line_end") or source.properties.get("end_line"),
        "extractor": STRUCTURAL_LINKER,
        "evidence": evidence,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "mode": "compile",
        "is_semantic": False,
        "is_technical": True,
    }
    properties.update(extra or {})
    return MemoryEdge(
        id=stable_id("edge", source.id, edge_type, target.id),
        from_id=source.id,
        to_id=target.id,
        type=edge_type,
        weight=1.0,
        confidence=1.0,
        origin="deterministic",
        properties=properties,
        created_at=now,
        updated_at=now,
    )


def _deactivate_edge(store: GraphStore, edge: MemoryEdge, result: ArtifactCompilationResult) -> None:
    properties = dict(edge.properties)
    if properties.get("status") == "archived" and edge.weight <= 0.0:
        return
    properties["status"] = "archived"
    properties["updated_at"] = utcnow_iso()
    store.update_edge_fields(edge.id, properties=properties, weight=0.0, confidence=0.0)
    result.archived_edges.add(edge.id)
    result.affected_edge_ids.add(edge.id)


def _resolved_path(import_node: MemoryNode) -> str:
    direct = import_node.properties.get("resolved_relative_path")
    metadata = import_node.properties.get("metadata")
    nested = metadata.get("resolved_relative_path") if isinstance(metadata, dict) else None
    return str(direct or nested or "").replace("\\", "/")


def _path(node: MemoryNode) -> str:
    return str(node.properties.get("relative_path") or "").replace("\\", "/")


def _is_initializer(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == "__init__.py"


def _roles(node: MemoryNode) -> list[str]:
    value = node.properties.get("semantic_roles")
    return [str(item) for item in value if item] if isinstance(value, list) else []


def _add_state_role(state: dict[str, object], role: str) -> None:
    roles = state.get("semantic_roles")
    current = {str(item) for item in roles} if isinstance(roles, list) else set()
    state["semantic_roles"] = sorted({*current, role})


def _dedupe_nodes(nodes: Iterable[MemoryNode]) -> list[MemoryNode]:
    return list({node.id: node for node in nodes}.values())


def _unique_node(nodes: Iterable[MemoryNode]) -> MemoryNode | None:
    values = _dedupe_nodes(nodes)
    return values[0] if len(values) == 1 else None


def _first(nodes: Iterable[MemoryNode]) -> MemoryNode | None:
    return next(iter(nodes), None)
