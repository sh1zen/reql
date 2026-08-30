"""Build deterministic high-level project pipelines from compiled graph facts."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..explanation.service import (
    _capability_key,
    _capability_name,
    _CapabilityGroup,
    _dominant_source_root,
    _humanize_symbol,
    _implementation_body_text,
    _infer_layer,
    _is_public_owner,
    _is_test_path,
    _module_hint_matches_path,
    _node_label,
    _node_path,
    _source_references_name,
    _workflow_trigger_reason,
)
from ..storage.graph_store import GraphStore
from .models import (
    PipelineComponent,
    PipelineEdge,
    PipelineOutcome,
    PipelineSymbol,
    PipelineWorkflow,
    ProjectPipeline,
)

PIPELINE_NODE_TYPES = {"Class", "Endpoint", "Function", "Interface", "Method"}
PIPELINE_EDGE_TYPES = {"CALLS", "HANDLES_ROUTE", "IMPORTS_FROM", "INSTANTIATES", "WRAPS"}
OUTCOME_EDGE_TYPES = {"EMITS", "RAISES", "RETURNS", "WRITES"}
GENERIC_MODULE_NAMES = {"base", "common", "core", "helpers", "models", "service", "types", "utils"}


@dataclass(frozen=True, slots=True)
class _FlowStep:
    from_id: str
    to_id: str
    relation: str


@dataclass(slots=True)
class _Traversal:
    anchor: MemoryNode
    reason: str
    inferred: bool
    depth_by_node: dict[str, int]
    steps: list[_FlowStep]


class ProjectPipelineService:
    """Project a compiled code graph into shared high-level workflows."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def build(self, project: MemoryNode) -> ProjectPipeline:
        project_id = project.id
        active_nodes = self.store.find_nodes_by_property(
            "project_id",
            project_id,
            status="active",
            limit=None,
            clone=False,
        )
        pipeline_nodes = [node for node in active_nodes if _is_pipeline_node(node)]
        node_by_id = {node.id: node for node in pipeline_nodes}
        participants = {
            node.id: node
            for node in pipeline_nodes
            if node.type in PIPELINE_NODE_TYPES
        }
        all_edges = [
            edge
            for edge in self.store.all_edges()
            if edge.from_id in node_by_id and edge.to_id in node_by_id
        ]
        adjacency, handled_by_endpoint = _flow_adjacency(all_edges, participants)
        import_sources = {
            node.id
            for node in participants.values()
            if _workflow_trigger_reason(node) is not None
        }
        _merge_flow_steps(
            adjacency,
            _resolved_import_steps(pipeline_nodes, participants, import_sources),
        )
        effects = _outcome_edges(all_edges, node_by_id)
        entries, used_fallback = _entrypoints(participants, adjacency, handled_by_endpoint)
        traversals = [
            _traverse(anchor, reason=reason, inferred=inferred, adjacency=adjacency)
            for anchor, reason, inferred in entries
        ]
        resolved_import_sources = set(import_sources)
        while traversals:
            reachable_ids = {
                node_id
                for traversal in traversals
                for node_id in traversal.depth_by_node
            }
            pending_sources = reachable_ids - resolved_import_sources
            if not pending_sources:
                break
            resolved_import_sources.update(pending_sources)
            added = _merge_flow_steps(
                adjacency,
                _resolved_import_steps(pipeline_nodes, participants, pending_sources),
            )
            if not added:
                break
            traversals = [
                _traverse(anchor, reason=reason, inferred=inferred, adjacency=adjacency)
                for anchor, reason, inferred in entries
            ]
        traversals = [traversal for traversal in traversals if traversal.depth_by_node]

        project_name = str(project.properties.get("name") or project.label or project.id)
        root_path = str(project.properties.get("root_path") or project.canonical_key or project.text or "")
        project_payload = {"id": project.id, "name": project_name, "root_path": root_path}
        if not traversals:
            return ProjectPipeline(
                project=project_payload,
                summary=f"No deterministic project pipeline was detected for {project_name}.",
                basis=_basis(project_id, used_fallback=used_fallback, nodes=0, raw_steps=0),
                warnings=["No explicit entrypoint or inferred call-graph root has an observable project-local flow."],
            )

        traversed_node_ids = {
            node_id
            for traversal in traversals
            for node_id in traversal.depth_by_node
            if node_id in participants
        }
        traversed_paths = [
            _node_path(participants[node_id])
            for node_id in sorted(traversed_node_ids)
            if _node_path(participants[node_id])
        ]
        dominant_root = _dominant_source_root(traversed_paths)
        component_key_by_node = {
            node_id: _component_key(participants[node_id], dominant_root)
            for node_id in traversed_node_ids
        }
        entry_ids = {traversal.anchor.id for traversal in traversals}
        workflow_id_by_anchor = {
            traversal.anchor.id: stable_id("pipeline-workflow", project_id, traversal.anchor.id)
            for traversal in traversals
        }
        degree_by_id = _degree_by_id(all_edges)
        components, component_id_by_key = _components(
            project_id,
            traversals,
            participants,
            component_key_by_node,
            workflow_id_by_anchor,
            entry_ids,
            degree_by_id,
        )
        component_by_id = {component.id: component for component in components}

        workflow_models: list[PipelineWorkflow] = []
        outcome_models: list[PipelineOutcome] = []
        edge_relations: dict[tuple[str, str], set[str]] = defaultdict(set)
        edge_workflows: dict[tuple[str, str], set[str]] = defaultdict(set)
        all_symbol_steps: list[tuple[str, str]] = []
        observed_terminals = False

        for traversal in traversals:
            workflow_id = workflow_id_by_anchor[traversal.anchor.id]
            component_depths: dict[str, int] = {}
            for node_id, depth in traversal.depth_by_node.items():
                key = component_key_by_node.get(node_id)
                component_id = component_id_by_key.get(key or "")
                if component_id is None:
                    continue
                previous = component_depths.get(component_id)
                if previous is None or depth < previous:
                    component_depths[component_id] = depth

            trigger_key = component_key_by_node.get(traversal.anchor.id)
            trigger_component_id = component_id_by_key.get(trigger_key or "")
            if trigger_component_id is None:
                continue

            for step in traversal.steps:
                all_symbol_steps.append((step.from_id, step.to_id))
                from_component = component_id_by_key.get(component_key_by_node.get(step.from_id, ""))
                to_component = component_id_by_key.get(component_key_by_node.get(step.to_id, ""))
                if not from_component or not to_component or from_component == to_component:
                    continue
                pair = (from_component, to_component)
                edge_relations[pair].add(step.relation)
                edge_workflows[pair].add(workflow_id)

            workflow_outcomes, has_observed_terminal = _workflow_outcomes(
                traversal,
                workflow_id=workflow_id,
                participants=participants,
                node_by_id=node_by_id,
                adjacency=adjacency,
                effects=effects,
                component_key_by_node=component_key_by_node,
                component_id_by_key=component_id_by_key,
            )
            outcome_models.extend(workflow_outcomes)
            observed_terminals = observed_terminals or has_observed_terminal
            workflow_models.append(
                PipelineWorkflow(
                    id=workflow_id,
                    name=_humanize_symbol(_node_label(traversal.anchor)),
                    trigger=_pipeline_symbol(traversal.anchor, entrypoint=True),
                    trigger_reason=traversal.reason,
                    inferred=traversal.inferred,
                    trigger_component_id=trigger_component_id,
                    component_ids=[
                        component_id
                        for component_id, _depth in sorted(
                            component_depths.items(),
                            key=lambda item: (item[1], component_by_id[item[0]].name.casefold(), item[0]),
                        )
                    ],
                    outcome_ids=[outcome.id for outcome in workflow_outcomes],
                )
            )

        component_pairs = list(edge_relations)
        component_sccs = _strongly_connected_components(
            [component.id for component in components],
            component_pairs,
        )
        cyclic_components = {
            node_id
            for component_ids in component_sccs
            if len(component_ids) > 1
            for node_id in component_ids
        }
        symbol_sccs = _strongly_connected_components(traversed_node_ids, all_symbol_steps)
        for symbol_ids in symbol_sccs:
            if len(symbol_ids) <= 1:
                continue
            keys = {component_key_by_node.get(node_id) for node_id in symbol_ids}
            if len(keys) == 1:
                component_id = component_id_by_key.get(next(iter(keys)) or "")
                if component_id:
                    cyclic_components.add(component_id)
        for component in components:
            component.cyclic = component.id in cyclic_components

        component_scc_by_id = {
            node_id: index
            for index, component_ids in enumerate(component_sccs)
            for node_id in component_ids
        }
        pipeline_edges = [
            PipelineEdge(
                id=stable_id("pipeline-edge", project_id, from_id, to_id),
                from_component_id=from_id,
                to_component_id=to_id,
                relation_types=tuple(sorted(edge_relations[(from_id, to_id)])),
                workflow_ids=tuple(sorted(edge_workflows[(from_id, to_id)])),
                cyclic=(
                    component_scc_by_id.get(from_id) == component_scc_by_id.get(to_id)
                    and from_id in cyclic_components
                ),
            )
            for from_id, to_id in sorted(component_pairs)
        ]
        workflow_models.sort(key=lambda item: (item.name.casefold(), item.trigger.path, item.id))
        outcome_models.sort(key=lambda item: (item.workflow_id, item.kind, item.label.casefold(), item.id))
        warnings = ["Dynamic dispatch and runtime-only paths are not inferred."]
        if used_fallback:
            warnings.append("No explicit entrypoint was found; call-graph roots are marked as inferred triggers.")
        if observed_terminals:
            warnings.append(
                "Observed terminal nodes mark the end of available static evidence, not guaranteed runtime completion."
            )
        return ProjectPipeline(
            project=project_payload,
            summary=(
                f"{project_name} exposes {len(workflow_models)} deterministic project workflows "
                f"across {len(components)} shared components."
            ),
            basis=_basis(
                project_id,
                used_fallback=used_fallback,
                nodes=len(traversed_node_ids),
                raw_steps=sum(len(traversal.steps) for traversal in traversals),
            ),
            workflows=workflow_models,
            components=components,
            edges=pipeline_edges,
            outcomes=outcome_models,
            warnings=warnings,
        )


def _is_pipeline_node(node: MemoryNode) -> bool:
    """Return whether a graph node may enter any part of the pipeline projection."""

    return node.type.casefold() != "test" and not _is_test_path(_node_path(node))


def _basis(project_id: str, *, used_fallback: bool, nodes: int, raw_steps: int) -> dict[str, object]:
    return {
        "mode": "deterministic-code-graph",
        "projection": "project-pipeline-v1",
        "project_id": project_id,
        "persisted": False,
        "llm_required": False,
        "all_workflows": True,
        "fallback_entrypoints": used_fallback,
        "traversed_symbols": nodes,
        "raw_flow_steps": raw_steps,
    }


def _flow_adjacency(
    edges: Sequence[MemoryEdge],
    participants: dict[str, MemoryNode],
) -> tuple[dict[str, list[_FlowStep]], set[str]]:
    adjacency: dict[str, list[_FlowStep]] = defaultdict(list)
    handled_by_endpoint: set[str] = set()
    for edge in edges:
        if edge.type not in PIPELINE_EDGE_TYPES:
            continue
        source = participants.get(edge.from_id)
        target = participants.get(edge.to_id)
        if source is None or target is None:
            continue
        if edge.type == "HANDLES_ROUTE" and target.type == "Endpoint":
            adjacency[target.id].append(_FlowStep(target.id, source.id, edge.type))
            handled_by_endpoint.add(source.id)
            continue
        adjacency[source.id].append(_FlowStep(source.id, target.id, edge.type))
    for node_id in list(adjacency):
        adjacency[node_id] = sorted(
            set(adjacency[node_id]),
            key=lambda item: (item.relation, item.to_id, item.from_id),
        )
    return dict(adjacency), handled_by_endpoint


def _outcome_edges(
    edges: Sequence[MemoryEdge],
    node_by_id: dict[str, MemoryNode],
) -> dict[str, list[tuple[str, MemoryNode]]]:
    effects: dict[str, list[tuple[str, MemoryNode]]] = defaultdict(list)
    for edge in edges:
        if edge.type not in OUTCOME_EDGE_TYPES:
            continue
        target = node_by_id.get(edge.to_id)
        if target is not None:
            effects[edge.from_id].append((edge.type, target))
    for node_id in list(effects):
        effects[node_id].sort(key=lambda item: (item[0], _node_label(item[1]).casefold(), item[1].id))
    return dict(effects)


def _entrypoints(
    participants: dict[str, MemoryNode],
    adjacency: dict[str, list[_FlowStep]],
    handled_by_endpoint: set[str],
) -> tuple[list[tuple[MemoryNode, str, bool]], bool]:
    explicit: list[tuple[MemoryNode, str, bool]] = []
    endpoint_handlers = {
        step.to_id
        for node_id, steps in adjacency.items()
        if participants.get(node_id) is not None and participants[node_id].type == "Endpoint"
        for step in steps
    }
    for node in participants.values():
        reason = _workflow_trigger_reason(node)
        if reason is None or not adjacency.get(node.id):
            continue
        if node.id in handled_by_endpoint and node.id in endpoint_handlers:
            continue
        explicit.append((node, reason, False))
    if explicit:
        return sorted(explicit, key=lambda item: _node_sort_key(item[0])), False

    incoming: Counter[str] = Counter(
        step.to_id
        for steps in adjacency.values()
        for step in steps
    )
    public_roots = [
        node
        for node in participants.values()
        if node.type in {"Endpoint", "Function", "Method"}
        and adjacency.get(node.id)
        and incoming[node.id] == 0
        and _is_public_owner(node)
    ]
    if not public_roots:
        public_roots = [
            node
            for node in participants.values()
            if node.type in {"Endpoint", "Function", "Method"}
            and adjacency.get(node.id)
            and _is_public_owner(node)
        ]
        public_roots.sort(key=lambda node: (-len(adjacency[node.id]), *_node_sort_key(node)))
        public_roots = public_roots[:1]
    return [
        (node, "inferred call-graph root", True)
        for node in sorted(public_roots, key=_node_sort_key)
    ], bool(public_roots)


def _traverse(
    anchor: MemoryNode,
    *,
    reason: str,
    inferred: bool,
    adjacency: dict[str, list[_FlowStep]],
) -> _Traversal:
    depth_by_node = {anchor.id: 0}
    queue = deque([anchor.id])
    steps: set[_FlowStep] = set()
    while queue:
        source_id = queue.popleft()
        source_depth = depth_by_node[source_id]
        for step in adjacency.get(source_id, []):
            steps.add(step)
            target_depth = source_depth + 1
            previous = depth_by_node.get(step.to_id)
            if previous is None or target_depth < previous:
                depth_by_node[step.to_id] = target_depth
                queue.append(step.to_id)
    return _Traversal(
        anchor=anchor,
        reason=reason,
        inferred=inferred,
        depth_by_node=depth_by_node,
        steps=sorted(steps, key=lambda item: (item.from_id, item.to_id, item.relation)),
    )


def _components(
    project_id: str,
    traversals: Sequence[_Traversal],
    participants: dict[str, MemoryNode],
    component_key_by_node: dict[str, str],
    workflow_id_by_anchor: dict[str, str],
    entry_ids: set[str],
    degree_by_id: dict[str, int],
) -> tuple[list[PipelineComponent], dict[str, str]]:
    groups: dict[str, _CapabilityGroup] = {}
    workflow_ids_by_key: dict[str, set[str]] = defaultdict(set)
    for traversal in traversals:
        workflow_id = workflow_id_by_anchor[traversal.anchor.id]
        for node_id in traversal.depth_by_node:
            node = participants.get(node_id)
            key = component_key_by_node.get(node_id)
            if node is None or not key:
                continue
            group = groups.setdefault(key, _CapabilityGroup(key))
            group.nodes[node.id] = node
            path = _node_path(node)
            if path:
                group.paths[path] += 1
            workflow_ids_by_key[key].add(workflow_id)

    component_id_by_key = {
        key: stable_id("pipeline-component", project_id, key)
        for key in groups
    }
    components = []
    for key, group in sorted(groups.items()):
        symbols = sorted(
            (
                _pipeline_symbol(node, entrypoint=node.id in entry_ids)
                for node in group.nodes.values()
            ),
            key=lambda item: (
                not item.entrypoint,
                item.private,
                item.path.casefold(),
                item.line_start if item.line_start is not None else 10**9,
                item.label.casefold(),
                item.node_id,
            ),
        )
        components.append(
            PipelineComponent(
                id=component_id_by_key[key],
                key=key,
                name=_component_name(key, group, degree_by_id),
                layer=_infer_layer(key, group.paths),
                paths=sorted(group.paths),
                symbols=symbols,
                workflow_ids=sorted(workflow_ids_by_key[key]),
            )
        )
    components.sort(key=lambda item: (item.layer, item.name.casefold(), item.id))
    return components, component_id_by_key


def _workflow_outcomes(
    traversal: _Traversal,
    *,
    workflow_id: str,
    participants: dict[str, MemoryNode],
    node_by_id: dict[str, MemoryNode],
    adjacency: dict[str, list[_FlowStep]],
    effects: dict[str, list[tuple[str, MemoryNode]]],
    component_key_by_node: dict[str, str],
    component_id_by_key: dict[str, str],
) -> tuple[list[PipelineOutcome], bool]:
    outcomes: dict[str, PipelineOutcome] = {}
    observed_terminal = False
    reachable = set(traversal.depth_by_node)
    for node_id in sorted(reachable):
        source = participants.get(node_id)
        if source is None:
            continue
        component_id = component_id_by_key.get(component_key_by_node.get(node_id, ""))
        if component_id is None:
            continue
        node_effects = effects.get(node_id, [])
        for relation, target in node_effects:
            label = _node_label(target)
            outcome_id = stable_id("pipeline-outcome", workflow_id, component_id, relation, label)
            outcomes[outcome_id] = PipelineOutcome(
                id=outcome_id,
                workflow_id=workflow_id,
                component_id=component_id,
                kind=relation,
                label=label,
                symbol_id=source.id,
            )
        outgoing = [step for step in adjacency.get(node_id, []) if step.to_id in reachable]
        if not outgoing and not node_effects:
            label = _humanize_symbol(_node_label(source))
            outcome_id = stable_id("pipeline-outcome", workflow_id, component_id, "observed-end", source.id)
            outcomes[outcome_id] = PipelineOutcome(
                id=outcome_id,
                workflow_id=workflow_id,
                component_id=component_id,
                kind="OBSERVED_END",
                label=label,
                symbol_id=source.id,
                observed_terminal=True,
            )
            observed_terminal = True
    return sorted(outcomes.values(), key=lambda item: (item.kind, item.label.casefold(), item.id)), observed_terminal


def _component_key(node: MemoryNode, dominant_root: str | None) -> str:
    path = _node_path(node).replace("\\", "/").strip("/")
    if path:
        module_path = str(PurePosixPath(path).with_suffix(""))
        module_path = module_path.removesuffix("/__init__")
        return f"module:{module_path.casefold()}"
    path_key = _capability_key(path, dominant_root)
    if path_key:
        return f"capability:{path_key}"
    return f"unlocated-{node.type.casefold()}"


def _component_name(key: str, group: _CapabilityGroup, degree_by_id: dict[str, int]) -> str:
    paths = [path for path, count in group.paths.items() for _ in range(count)]
    if paths:
        path = PurePosixPath(Counter(paths).most_common(1)[0][0].replace("\\", "/"))
        stem = path.stem
        if stem.casefold() in GENERIC_MODULE_NAMES and path.parent.name:
            stem = f"{path.parent.name} {stem}"
        return _humanize_symbol(stem)
    return _capability_name(group, degree_by_id)


def _resolved_import_steps(
    active_nodes: Sequence[MemoryNode],
    participants: dict[str, MemoryNode],
    source_ids: Iterable[str],
) -> list[_FlowStep]:
    imports_by_artifact: dict[str, list[MemoryNode]] = defaultdict(list)
    imports_by_source_and_name: dict[tuple[str, str], list[MemoryNode]] = defaultdict(list)
    fragments_by_artifact: dict[str, list[MemoryNode]] = defaultdict(list)
    targets_by_path: dict[str, list[MemoryNode]] = defaultdict(list)
    targets_by_name: dict[str, list[MemoryNode]] = defaultdict(list)
    for node in active_nodes:
        artifact_id = str(node.properties.get("artifact_id") or "")
        if node.type == "Import" and artifact_id:
            imports_by_artifact[artifact_id].append(node)
            source_file = str(node.properties.get("source_file") or "").replace("\\", "/").casefold()
            import_name = str(node.properties.get("name") or "").rsplit(".", 1)[-1].casefold()
            if source_file and import_name:
                imports_by_source_and_name[(source_file, import_name)].append(node)
        elif node.type == "SourceFragment" and artifact_id:
            fragments_by_artifact[artifact_id].append(node)
    for target in participants.values():
        path = _node_path(target).replace("\\", "/").casefold()
        name = str(target.properties.get("name") or _node_label(target)).rsplit(".", 1)[-1].casefold()
        if path:
            targets_by_path[path].append(target)
        if name:
            targets_by_name[name].append(target)

    steps: set[_FlowStep] = set()
    for source_id in sorted(set(source_ids)):
        source = participants.get(source_id)
        if source is None:
            continue
        artifact_id = str(source.properties.get("artifact_id") or "")
        if not artifact_id:
            continue
        source_text = "\n".join(
            str(fragment.text or "")
            for fragment in fragments_by_artifact.get(artifact_id, [])
            if fragment.properties.get("symbol_id") == source.id
        )
        source_text = _implementation_body_text(source, source_text)
        if not source_text:
            continue
        for import_node in sorted(imports_by_artifact.get(artifact_id, []), key=lambda item: item.id):
            imported_name = str(import_node.properties.get("name") or "").rsplit(".", 1)[-1]
            reference_name = str(import_node.properties.get("alias") or imported_name)
            if not imported_name or not _source_references_name(source_text, reference_name):
                continue
            resolved_path = str(import_node.properties.get("resolved_relative_path") or "").replace("\\", "/")
            module_hint = str(import_node.properties.get("module") or "")
            resolved_paths: set[str] = set()
            pending_paths = deque([resolved_path]) if resolved_path else deque()
            while pending_paths:
                candidate_path = pending_paths.popleft().replace("\\", "/")
                folded_path = candidate_path.casefold()
                if not candidate_path or folded_path in resolved_paths:
                    continue
                resolved_paths.add(folded_path)
                for reexport in imports_by_source_and_name.get(
                    (folded_path, imported_name.casefold()),
                    [],
                ):
                    reexport_path = str(reexport.properties.get("resolved_relative_path") or "")
                    if reexport_path:
                        pending_paths.append(reexport_path)
            exact_path_targets = [
                target
                for candidate_path in sorted(resolved_paths)
                for target in targets_by_path.get(candidate_path, [])
                if str(target.properties.get("name") or _node_label(target)).rsplit(".", 1)[-1].casefold()
                == imported_name.casefold()
            ]
            candidates = exact_path_targets or targets_by_name.get(imported_name.casefold(), [])
            for target in candidates:
                target_name = str(target.properties.get("name") or _node_label(target)).rsplit(".", 1)[-1]
                if target.id == source.id or target_name.casefold() != imported_name.casefold():
                    continue
                target_path = _node_path(target).replace("\\", "/")
                path_is_exact = target_path.casefold() in resolved_paths
                if not path_is_exact and module_hint and not _module_hint_matches_path(module_hint, target_path):
                    continue
                steps.add(_FlowStep(source.id, target.id, "IMPORTS_FROM"))
    return sorted(steps, key=lambda item: (item.from_id, item.to_id, item.relation))


def _merge_flow_steps(adjacency: dict[str, list[_FlowStep]], steps: Iterable[_FlowStep]) -> int:
    added = 0
    for step in steps:
        existing = adjacency.setdefault(step.from_id, [])
        if step in existing:
            continue
        existing.append(step)
        existing.sort(key=lambda item: (item.relation, item.to_id, item.from_id))
        added += 1
    return added


def _pipeline_symbol(node: MemoryNode, *, entrypoint: bool) -> PipelineSymbol:
    label = _node_label(node)
    short_name = label.rsplit(".", 1)[-1]
    return PipelineSymbol(
        node_id=node.id,
        node_type=node.type,
        label=label,
        path=_node_path(node),
        line_start=_int_or_none(node.properties.get("line_start") or node.properties.get("start_line")),
        line_end=_int_or_none(node.properties.get("line_end") or node.properties.get("end_line")),
        private=short_name.startswith("_"),
        entrypoint=entrypoint,
    )


def _degree_by_id(edges: Sequence[MemoryEdge]) -> dict[str, int]:
    degree: Counter[str] = Counter()
    for edge in edges:
        degree[edge.from_id] += 1
        degree[edge.to_id] += 1
    return dict(degree)


def _node_sort_key(node: MemoryNode) -> tuple[str, int, str, str]:
    return (
        _node_path(node).casefold(),
        _int_or_none(node.properties.get("line_start") or node.properties.get("start_line")) or 0,
        _node_label(node).casefold(),
        node.id,
    )


def _int_or_none(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _strongly_connected_components(
    node_ids: Iterable[str],
    edges: Iterable[tuple[str, str]],
) -> list[set[str]]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    nodes = set(node_ids)
    for source, target in edges:
        nodes.add(source)
        nodes.add(target)
        adjacency[source].append(target)
    for source in list(adjacency):
        adjacency[source] = sorted(set(adjacency[source]))

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[set[str]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for target_id in adjacency.get(node_id, []):
            if target_id not in indices:
                visit(target_id)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[target_id])
            elif target_id in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[target_id])
        if lowlinks[node_id] != indices[node_id]:
            return
        component: set[str] = set()
        while stack:
            target_id = stack.pop()
            on_stack.remove(target_id)
            component.add(target_id)
            if target_id == node_id:
                break
        components.append(component)

    for node_id in sorted(nodes):
        if node_id not in indices:
            visit(node_id)
    return components
