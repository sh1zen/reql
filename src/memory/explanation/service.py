"""Derive a business-oriented repository view from deterministic code facts."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from typing import Any, Iterable, Sequence

from ..domain.ids import stable_id
from ..domain.models import MemoryNode
from ..storage.graph_store import GraphStore
from .models import (
    ArchitectureLayer,
    BusinessCapability,
    BusinessWorkflow,
    ChangeGuide,
    CodeEvidence,
    RepositoryExplanation,
    WorkflowParticipant,
)


ACTIVE_STATUSES = {"active"}
CAPABILITY_NODE_TYPES = {"Module", "Class", "Interface", "Function", "Endpoint", "Schema"}
OWNER_NODE_TYPES = {"Class", "Interface", "Function", "Method", "Endpoint", "Schema"}
FLOW_NODE_TYPES = OWNER_NODE_TYPES | {"Module"}
WORKFLOW_PARTICIPANT_TYPES = {"Class", "Interface", "Function", "Method", "Endpoint"}
WORKFLOW_IMPLEMENTATION_EDGE_TYPES = {
    "CALLS",
    "HANDLES_ROUTE",
    "INSTANTIATES",
    "WRAPS",
}
WORKFLOW_OUTPUT_EDGE_TYPES = {"EMITS", "WRITES"}
WORKFLOW_MAX_HOPS = 4
DEPENDENCY_EDGE_TYPES = {
    "CALLS",
    "DEPENDS_ON",
    "HANDLES_ROUTE",
    "IMPLEMENTS",
    "IMPORTS",
    "IMPORTS_FROM",
    "INSTANTIATES",
    "USES",
    "WRAPS",
}
SOURCE_ROOTS = {"app", "apps", "lib", "main", "packages", "pkg", "source", "sources", "src"}
TEST_ROOTS = {"spec", "specs", "test", "tests"}
GENERIC_CAPABILITY_NAMES = {
    "application",
    "common",
    "core",
    "domain",
    "infrastructure",
    "internal",
    "service",
    "services",
    "shared",
    "support",
    "util",
    "utils",
}
GENERIC_OWNER_NAMES = {
    "add",
    "create",
    "current",
    "delete",
    "execute",
    "exists",
    "get",
    "handle",
    "list",
    "read",
    "run",
    "set",
    "status",
    "update",
    "write",
}
LAYER_ORDER = ("interface", "application", "domain", "core", "infrastructure")
LAYER_PURPOSES = {
    "interface": "exposes commands, APIs, routes, protocols, or user-facing adapters",
    "application": "coordinates use cases and repository workflows",
    "domain": "defines business entities, policies, schemas, and rules",
    "core": "implements the repository's central deterministic behavior",
    "infrastructure": "provides storage, configuration, persistence, caching, and external adapters",
}
LAYER_TOKENS = {
    "interface": {"api", "cli", "controller", "endpoint", "http", "mcp", "presentation", "route", "routes", "ui", "view", "web"},
    "application": {"application", "command", "commands", "handler", "handlers", "job", "jobs", "orchestrator", "service", "services", "usecase", "usecases", "workflow", "workflows"},
    "domain": {"domain", "entities", "entity", "model", "models", "policies", "policy", "rule", "rules", "schema", "schemas", "value"},
    "infrastructure": {"adapter", "adapters", "cache", "config", "database", "db", "infrastructure", "migration", "migrations", "persistence", "repository", "storage"},
}
ENTRY_ROLE_TOKENS = {"command", "controller", "endpoint", "entrypoint", "handler", "public-api", "route"}
ENTRY_NAMES = {"_main", "execute", "handle", "main", "run"}
NON_WORKFLOW_NAMES = {
    "__init__",
    "__new__",
    "setUp",
    "setUpClass",
    "tearDown",
    "tearDownClass",
}
INVARIANT_TOKENS = {
    "always",
    "atomic",
    "authorize",
    "authorized",
    "ensure",
    "invariant",
    "lock",
    "must",
    "never",
    "only",
    "permission",
    "require",
    "required",
    "transaction",
    "transactional",
    "valid",
    "validate",
}
WORKFLOW_GENERIC_TOKENS = {
    "api",
    "application",
    "async",
    "class",
    "core",
    "def",
    "function",
    "handler",
    "main",
    "method",
    "module",
    "run",
    "service",
}
SYMBOL_SUFFIXES = (
    "Controller",
    "Coordinator",
    "Engine",
    "Handler",
    "Manager",
    "Repository",
    "Service",
    "Store",
    "UseCase",
    "Workflow",
)
SEMANTIC_WORKFLOW_SUFFIXES = SYMBOL_SUFFIXES + (
    "Adapter",
    "Builder",
    "Client",
    "Gateway",
    "Policy",
    "Rule",
    "Validator",
)
WORD_RE = re.compile(r"[A-Za-z0-9]+")
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(slots=True)
class _CapabilityGroup:
    key: str
    nodes: dict[str, MemoryNode] = field(default_factory=dict)
    paths: Counter[str] = field(default_factory=Counter)
    score: float = 0.0
    focus_score: float = 0.0


@dataclass(slots=True)
class _WorkflowCandidate:
    anchor: MemoryNode
    members: dict[str, MemoryNode]
    relations: dict[str, set[str]]
    predecessors: dict[str, set[str]]
    documentation: list[CodeEvidence]
    focus_score: int
    score: float


class RepositoryExplanationService:
    """Compute a read-only business projection over a compiled project graph."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def explain(
        self,
        project: MemoryNode,
        *,
        focus: str | None = None,
        max_capabilities: int = 12,
        max_workflows: int = 8,
    ) -> RepositoryExplanation:
        if project.type != "Project":
            raise ValueError("Repository explanation requires a Project node")
        if max_capabilities < 1:
            raise ValueError("max_capabilities must be at least 1")
        if max_workflows < 0:
            raise ValueError("max_workflows must not be negative")

        project_id = project.id
        artifacts = self._project_nodes(project_id, "SourceArtifact")
        code_artifacts = [node for node in artifacts if str(node.properties.get("artifact_type") or "") == "code"]
        code_artifact_ids = {node.id for node in code_artifacts}
        documentation_nodes = [
            node
            for node in self._project_nodes(project_id, "SourceFragment")
            if node.properties.get("artifact_id") not in code_artifact_ids
            and not _is_test_path(_node_path(node))
        ]
        test_paths = sorted(
            {
                path
                for node in code_artifacts
                if (path := _node_path(node)) and _is_test_path(path)
            }
        )
        modules = [
            node
            for node in self._project_nodes(project_id, "Module")
            if (path := _node_path(node)) and not _is_test_path(path)
        ]
        capability_nodes = self._capability_nodes(project_id, modules)
        paths = [_node_path(node) for node in modules]
        dominant_root = _dominant_source_root([path for path in paths if path])
        groups, node_groups = self._group_nodes(capability_nodes, dominant_root)

        top_nodes = self.store.top_nodes_by_degree(
            limit=None,
            node_types=FLOW_NODE_TYPES,
            statuses=ACTIVE_STATUSES,
            project_id=project_id,
            include_global_project=False,
        )
        degree_by_id = {node.id: degree for node, degree, _ in top_nodes}
        for node, degree, weighted_degree in top_nodes:
            path = _node_path(node)
            if not path or _is_test_path(path):
                continue
            key = _capability_key(path, dominant_root)
            if not key:
                continue
            group = groups.setdefault(key, _CapabilityGroup(key))
            group.nodes[node.id] = node
            group.paths[path] += 1
            group.score += 0.25 + float(degree) + float(weighted_degree)
            node_groups[node.id] = key

        focus_matches = self._focus_matches(project_id, focus)
        for node, score in focus_matches:
            key = _capability_key(_node_path(node), dominant_root)
            if key and key in groups:
                groups[key].focus_score += score
                groups[key].score += score * 8.0

        dependencies = self._capability_dependencies(
            node_groups,
            dominant_root=dominant_root,
            known_groups=set(groups),
        )
        capabilities = self._build_capabilities(
            project_id,
            groups,
            dependencies,
            degree_by_id,
            test_paths,
            max_capabilities=max_capabilities,
        )
        selected_ids = {capability.id for capability in capabilities}
        layers = self._build_layers(capabilities)
        workflows = self._build_workflows(
            project_id,
            top_nodes,
            node_groups=node_groups,
            documentation_nodes=documentation_nodes,
            focus=focus,
            max_workflows=max_workflows,
        )
        change_guide = self._build_change_guide(
            focus,
            focus_matches,
            capabilities,
            dominant_root,
            selected_ids,
        )
        languages = Counter(
            str(node.properties.get("language") or "unknown")
            for node in code_artifacts
        )
        artifact_types = Counter(str(node.properties.get("artifact_type") or "unknown") for node in artifacts)
        project_name = str(project.properties.get("name") or project.label or project.id)
        summary = (
            f"{project_name} contains {len(capabilities)} inferred business capabilities "
            f"across {len(layers)} architectural layers. The explanation is derived from "
            f"{len(modules)} code modules and {len(capability_nodes)} high-signal code symbols."
        )
        return RepositoryExplanation(
            project={
                "id": project.id,
                "name": project_name,
                "root_path": project.properties.get("root_path") or project.canonical_key or project.text,
            },
            summary=summary,
            basis={
                "mode": "deterministic-code-graph",
                "persisted": False,
                "llm_required": False,
                "workflow_projection": "semantic-multi-evidence",
                "focus": focus,
                "artifacts": len(artifacts),
                "code_modules": len(modules),
                "high_signal_symbols": len(capability_nodes),
                "artifact_types": dict(sorted(artifact_types.items())),
                "languages": dict(sorted(languages.items())),
            },
            layers=layers,
            capabilities=capabilities,
            workflows=workflows,
            change_guide=change_guide,
        )

    def _project_nodes(self, project_id: str, node_type: str) -> list[MemoryNode]:
        return self.store.find_nodes_by_property(
            "project_id",
            project_id,
            type_=node_type,
            status="active",
            limit=None,
            clone=False,
        )

    def _capability_nodes(self, project_id: str, modules: Sequence[MemoryNode]) -> list[MemoryNode]:
        nodes: dict[str, MemoryNode] = {node.id: node for node in modules}
        for node_type in CAPABILITY_NODE_TYPES - {"Module"}:
            for node in self._project_nodes(project_id, node_type):
                path = _node_path(node)
                if path and not _is_test_path(path):
                    nodes[node.id] = node
        return list(nodes.values())

    def _group_nodes(
        self,
        nodes: Sequence[MemoryNode],
        dominant_root: str | None,
    ) -> tuple[dict[str, _CapabilityGroup], dict[str, str]]:
        groups: dict[str, _CapabilityGroup] = {}
        node_groups: dict[str, str] = {}
        for node in nodes:
            path = _node_path(node)
            key = _capability_key(path, dominant_root)
            if not key:
                continue
            group = groups.setdefault(key, _CapabilityGroup(key))
            group.nodes[node.id] = node
            group.paths[path] += 1
            group.score += 1.0 if node.type == "Module" else 0.4
            node_groups[node.id] = key
        return groups, node_groups

    def _focus_matches(self, project_id: str, focus: str | None) -> list[tuple[MemoryNode, float]]:
        if not focus or not focus.strip():
            return []
        results = self.store.lexical_search(
            focus,
            top_k=None,
            node_types=CAPABILITY_NODE_TYPES | {"Method", "Test", "SourceArtifact"},
            include_archived=False,
        )
        matches = []
        for node, score in results:
            if node.properties.get("project_id") != project_id:
                continue
            if not _node_path(node):
                continue
            matches.append((node, float(score)))
        return matches

    def _capability_dependencies(
        self,
        node_groups: dict[str, str],
        *,
        dominant_root: str | None,
        known_groups: set[str],
    ) -> Counter[tuple[str, str]]:
        node_ids = list(node_groups)
        if not node_ids:
            return Counter()
        edges = self.store.incident_edges(
            node_ids,
            edge_types=DEPENDENCY_EDGE_TYPES,
            limit=None,
            clone=False,
        )
        dependencies: Counter[tuple[str, str]] = Counter()
        for edge in edges:
            source_group = node_groups.get(edge.from_id)
            target_group = node_groups.get(edge.to_id)
            if source_group and not target_group:
                target = self.store.get_node(edge.to_id, clone=False)
                if target is not None:
                    resolved_path = str(target.properties.get("resolved_relative_path") or "")
                    resolved_group = _capability_key(resolved_path, dominant_root)
                    if resolved_group in known_groups:
                        target_group = resolved_group
            if source_group and target_group and source_group != target_group:
                dependencies[(source_group, target_group)] += 1
        return dependencies

    def _build_capabilities(
        self,
        project_id: str,
        groups: dict[str, _CapabilityGroup],
        dependencies: Counter[tuple[str, str]],
        degree_by_id: dict[str, int],
        test_paths: Sequence[str],
        *,
        max_capabilities: int,
    ) -> list[BusinessCapability]:
        ranked_groups = sorted(
            groups.values(),
            key=lambda item: (-item.focus_score, -item.score, item.key),
        )[:max_capabilities]
        names = {group.key: _capability_name(group, degree_by_id) for group in ranked_groups}
        capabilities = []
        for group in ranked_groups:
            ranked_owners = sorted(
                (
                    node
                    for node in group.nodes.values()
                    if node.type in OWNER_NODE_TYPES and _is_public_owner(node)
                ),
                key=lambda node: _owner_rank(node, degree_by_id),
            )
            if not ranked_owners:
                ranked_owners = sorted(
                    (node for node in group.nodes.values() if node.type in OWNER_NODE_TYPES),
                    key=lambda node: _owner_rank(node, degree_by_id),
                )
            owners = _dedupe_evidence(
                _evidence(node, reason="high-signal owner in this capability")
                for node in ranked_owners
            )[:4]
            responsibilities = _responsibilities(ranked_owners, fallback=group.key)
            layer = _infer_layer(group.key, group.paths)
            dependency_names = [
                names[target]
                for (source, target), _count in sorted(
                    dependencies.items(),
                    key=lambda item: (-item[1], item[0][1]),
                )
                if source == group.key and target in names
            ][:5]
            primary_paths = [
                path
                for path, _count in sorted(
                    group.paths.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ][:5]
            associated_tests = _associated_tests(group.key, responsibilities, test_paths)
            purpose = _capability_purpose(names[group.key], responsibilities, dependency_names)
            capabilities.append(
                BusinessCapability(
                    id=stable_id("business-capability", project_id, group.key),
                    name=names[group.key],
                    layer=layer,
                    purpose=purpose,
                    responsibilities=responsibilities,
                    primary_paths=primary_paths,
                    dependencies=list(dict.fromkeys(dependency_names)),
                    owners=owners,
                    tests=associated_tests,
                    score=group.score,
                )
            )
        return capabilities

    def _build_layers(self, capabilities: Sequence[BusinessCapability]) -> list[ArchitectureLayer]:
        layers = []
        for layer_name in LAYER_ORDER:
            members = [item for item in capabilities if item.layer == layer_name]
            if not members:
                continue
            layers.append(
                ArchitectureLayer(
                    name=layer_name,
                    purpose=LAYER_PURPOSES[layer_name],
                    capability_ids=[item.id for item in members],
                    paths=sorted({path for item in members for path in item.primary_paths}),
                )
            )
        return layers

    def _build_workflows(
        self,
        project_id: str,
        top_nodes: Sequence[tuple[MemoryNode, int, float]],
        *,
        node_groups: dict[str, str],
        documentation_nodes: Sequence[MemoryNode],
        focus: str | None,
        max_workflows: int,
    ) -> list[BusinessWorkflow]:
        if max_workflows == 0:
            return []
        focus_tokens = _tokens(focus or "")
        candidates: list[_WorkflowCandidate] = []
        for node, degree, weighted_degree in top_nodes:
            trigger_reason = _workflow_trigger_reason(node)
            if trigger_reason is None:
                continue
            members, relations, predecessors = self._workflow_implementation_nodes(
                node,
                project_id,
            )
            if len(members) < 2 or not relations:
                continue
            concept_tokens = _workflow_concept_tokens(node)
            documentation = _workflow_documentation_evidence(
                documentation_nodes,
                concept_tokens,
            )
            documentation.extend(
                _workflow_docstring_evidence(
                    members.values(),
                    concept_tokens,
                )
            )
            documentation = _dedupe_evidence(documentation)
            all_text = " ".join(
                f"{_node_label(member)} {_node_path(member)}"
                for member in members.values()
            )
            focus_score = len(focus_tokens & _tokens(all_text))
            relation_kinds = {kind for kinds in relations.values() for kind in kinds}
            paths = {_node_path(member) for member in members.values() if _node_path(member)}
            groups = {node_groups[node_id] for node_id in members if node_id in node_groups}
            score = (
                focus_score * 20.0
                + len(members) * 3.0
                + len(relation_kinds) * 2.0
                + len(documentation) * 2.0
                + min(3, max(0, len(paths) - 1))
                + min(3, max(0, len(groups) - 1))
                + min(20.0, float(degree) + float(weighted_degree))
            )
            candidates.append(
                _WorkflowCandidate(
                    anchor=node,
                    members=members,
                    relations=relations,
                    predecessors=predecessors,
                    documentation=documentation,
                    focus_score=focus_score,
                    score=score,
                )
            )

        candidates.sort(
            key=lambda item: (
                -item.focus_score,
                -item.score,
                -len(item.members),
                _node_label(item.anchor).casefold(),
                item.anchor.id,
            )
        )
        selected: list[_WorkflowCandidate] = []
        for candidate in candidates:
            if any(_workflow_candidates_overlap(candidate, existing) for existing in selected):
                continue
            selected.append(candidate)
            if len(selected) >= max_workflows:
                break

        workflows = []
        for candidate in selected:
            anchor = candidate.anchor
            trigger_reason = _workflow_trigger_reason(anchor) or "explicit workflow trigger"
            ordered_members = [anchor] + sorted(
                (node for node_id, node in candidate.members.items() if node_id != anchor.id),
                key=lambda node: (
                    _workflow_role(node, is_trigger=False),
                    _node_label(node).casefold(),
                    node.id,
                ),
            )
            participants = []
            for member in ordered_members:
                is_trigger = member.id == anchor.id
                if is_trigger:
                    reason = trigger_reason
                else:
                    kinds = ", ".join(sorted(candidate.relations.get(member.id, set())))
                    sources = sorted(
                        {
                            _humanize_symbol(_node_label(candidate.members[source_id]))
                            for source_id in candidate.predecessors.get(member.id, set())
                            if source_id in candidate.members
                        }
                    )
                    source_suffix = f" from {', '.join(sources)}" if sources else ""
                    reason = f"implemented_by supported by {kinds}{source_suffix}"
                    anchor_group = node_groups.get(anchor.id)
                    member_group = node_groups.get(member.id)
                    if anchor_group and member_group and anchor_group != member_group:
                        reason += f"; crosses capability boundary {anchor_group} -> {member_group}"
                participants.append(
                    WorkflowParticipant(
                        relation="implemented_by",
                        role=_workflow_role(member, is_trigger=is_trigger),
                        target=_evidence(member, reason=reason),
                    )
                )

            name = _humanize_symbol(_node_label(anchor))
            inputs = _workflow_inputs(anchor)
            outputs = self._workflow_outputs(anchor)
            invariant_nodes = [anchor] + [
                member
                for member in candidate.members.values()
                if member.id != anchor.id
                and _workflow_role(member, is_trigger=False) == "policy"
            ]
            invariants = _workflow_invariants(invariant_nodes)
            evidence = _dedupe_evidence(
                candidate.documentation
                + [participant.target for participant in participants]
            )
            member_ids = sorted(candidate.members)
            workflows.append(
                BusinessWorkflow(
                    id=stable_id("business-workflow", project_id, anchor.id, *member_ids),
                    name=name,
                    intent=_workflow_intent(anchor, ordered_members),
                    trigger=name,
                    inputs=inputs,
                    outputs=outputs,
                    invariants=invariants,
                    participants=participants,
                    evidence=evidence,
                )
            )
        return workflows

    def _workflow_implementation_nodes(
        self,
        anchor: MemoryNode,
        project_id: str,
    ) -> tuple[dict[str, MemoryNode], dict[str, set[str]], dict[str, set[str]]]:
        members = {anchor.id: anchor}
        relations: dict[str, set[str]] = defaultdict(set)
        predecessors: dict[str, set[str]] = defaultdict(set)
        visited_depth = {anchor.id: 0}
        queue: deque[tuple[MemoryNode, int]] = deque([(anchor, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= WORKFLOW_MAX_HOPS:
                continue
            edges = self.store.get_edges(
                from_id=current.id,
                type_=sorted(WORKFLOW_IMPLEMENTATION_EDGE_TYPES),
                limit=None,
                clone=False,
            )
            for edge in sorted(edges, key=lambda item: (item.type, item.to_id, item.id)):
                target = self.store.get_node(edge.to_id, clone=False)
                if not _is_workflow_participant(target, project_id):
                    continue
                assert target is not None
                target_depth = depth + 1
                if not _workflow_target_is_semantic(
                    anchor,
                    target,
                    depth=target_depth,
                ):
                    continue
                members[target.id] = target
                relations[target.id].add(edge.type)
                predecessors[target.id].add(current.id)
                previous_depth = visited_depth.get(target.id)
                if previous_depth is None or target_depth < previous_depth:
                    visited_depth[target.id] = target_depth
                    queue.append((target, target_depth))
        self._add_import_participants(
            anchor,
            project_id,
            members,
            relations,
            predecessors,
        )
        return members, dict(relations), dict(predecessors)

    def _add_import_participants(
        self,
        anchor: MemoryNode,
        project_id: str,
        members: dict[str, MemoryNode],
        relations: dict[str, set[str]],
        predecessors: dict[str, set[str]],
    ) -> None:
        """Add exact imported symbols as corroborated workflow implementers."""

        processed_sources: set[str] = set()
        queue: deque[tuple[MemoryNode, int]] = deque(
            (member, 0) for member in members.values()
        )
        while queue:
            source, depth = queue.popleft()
            if depth >= WORKFLOW_MAX_HOPS:
                continue
            artifact_id = str(source.properties.get("artifact_id") or "")
            if not artifact_id or source.id in processed_sources:
                continue
            processed_sources.add(source.id)
            source_text = self._workflow_source_text(source)
            if not source_text:
                continue
            imports = self.store.find_nodes_by_property(
                "artifact_id",
                artifact_id,
                type_="Import",
                status="active",
                limit=None,
                clone=False,
            )
            for import_node in imports:
                resolved_path = str(import_node.properties.get("resolved_relative_path") or "")
                imported_name = str(import_node.properties.get("name") or "").rsplit(".", 1)[-1]
                module_hint = str(import_node.properties.get("module") or "")
                if not imported_name:
                    continue
                reference_name = str(import_node.properties.get("alias") or imported_name)
                if not _source_references_name(source_text, reference_name):
                    continue
                if resolved_path:
                    targets = self.store.find_nodes_by_property(
                        "relative_path",
                        resolved_path,
                        status="active",
                        limit=None,
                        clone=False,
                    )
                else:
                    targets = self.store.find_nodes_by_property(
                        "name",
                        imported_name,
                        status="active",
                        limit=None,
                        clone=False,
                    )
                for target in targets:
                    if not _is_workflow_participant(target, project_id):
                        continue
                    assert target is not None
                    target_name = _node_label(target).rsplit(".", 1)[-1]
                    if target_name.casefold() != imported_name.casefold():
                        continue
                    if module_hint and not _module_hint_matches_path(module_hint, _node_path(target)):
                        continue
                    if not _workflow_target_is_semantic(anchor, target, depth=2):
                        continue
                    is_new = target.id not in members
                    members[target.id] = target
                    relations[target.id].add("IMPORTS_FROM")
                    predecessors[target.id].add(source.id)
                    if (
                        is_new
                        and _workflow_role(target, is_trigger=False)
                        in {"orchestration", "infrastructure", "policy"}
                    ):
                        queue.append((target, depth + 1))

    def _workflow_source_text(self, node: MemoryNode) -> str:
        artifact_id = str(node.properties.get("artifact_id") or "")
        if not artifact_id:
            return ""
        fragments = self.store.find_nodes_by_property(
            "artifact_id",
            artifact_id,
            type_="SourceFragment",
            status="active",
            limit=None,
            clone=False,
        )
        source_text = "\n".join(
            str(fragment.text or "")
            for fragment in fragments
            if fragment.properties.get("symbol_id") == node.id
        )
        return _implementation_body_text(node, source_text)

    def _workflow_outputs(self, anchor: MemoryNode) -> list[str]:
        outputs = []
        return_type = str(anchor.properties.get("returns") or "").strip()
        if return_type:
            outputs.append(return_type)
        edges = self.store.get_edges(
            from_id=anchor.id,
            type_=sorted(WORKFLOW_OUTPUT_EDGE_TYPES),
            limit=None,
            clone=False,
        )
        for edge in edges:
            target = self.store.get_node(edge.to_id, clone=False)
            if target is None or _is_test_path(_node_path(target)):
                continue
            outputs.append(f"{_humanize_symbol(edge.type)} {_humanize_symbol(_node_label(target))}")
        return list(dict.fromkeys(output for output in outputs if output))

    def _build_change_guide(
        self,
        focus: str | None,
        focus_matches: Sequence[tuple[MemoryNode, float]],
        capabilities: Sequence[BusinessCapability],
        dominant_root: str | None,
        selected_ids: set[str],
    ) -> ChangeGuide:
        capabilities_by_key = {
            _capability_key(path, dominant_root): capability
            for capability in capabilities
            for path in capability.primary_paths
        }
        capability_ids = []
        start_here = []
        if focus:
            for node, score in focus_matches:
                if _is_test_path(_node_path(node)):
                    continue
                key = _capability_key(_node_path(node), dominant_root)
                capability = capabilities_by_key.get(key)
                if capability and capability.id in selected_ids and capability.id not in capability_ids:
                    capability_ids.append(capability.id)
                start_here.append(_evidence(node, reason=f"lexical match for '{focus}'", score=score))
                if len(start_here) >= 8:
                    break
            rationale = (
                f"Start with the code facts ranked for '{focus}', then follow the owning capability "
                "and its declared verification paths before editing."
            )
        else:
            for capability in capabilities[:3]:
                capability_ids.append(capability.id)
                start_here.extend(capability.owners[:1])
            rationale = (
                "No focus was supplied, so the guide starts from the highest-signal capability owners. "
                "Pass a focus phrase to rank change-specific files and symbols."
            )
        verify_with = list(
            dict.fromkeys(
                test
                for capability in capabilities
                if not capability_ids or capability.id in capability_ids
                for test in capability.tests
            )
        )[:12]
        return ChangeGuide(
            focus=focus,
            rationale=rationale,
            capability_ids=capability_ids,
            start_here=_dedupe_evidence(start_here)[:8],
            verify_with=verify_with,
        )


def _node_path(node: MemoryNode) -> str:
    value = (
        node.properties.get("relative_path")
        or node.properties.get("source_file")
        or node.properties.get("path")
        or ""
    )
    return str(value).replace("\\", "/").lstrip("./")


def _node_label(node: MemoryNode) -> str:
    value = node.properties.get("name") or node.properties.get("qualified_name") or node.label or node.text or node.id
    return str(value).splitlines()[0]


def _path_parts(path: str) -> list[str]:
    if not path:
        return []
    parts = [part for part in PurePosixPath(path).parts if part not in {"", "."}]
    while parts and parts[0].casefold() in SOURCE_ROOTS:
        parts.pop(0)
    if not parts:
        return []
    filename = parts[-1]
    stem = filename
    while "." in stem:
        next_stem = stem.rsplit(".", 1)[0]
        if not next_stem:
            break
        stem = next_stem
    if stem.casefold() in {"__init__", "index", "mod"} and len(parts) > 1:
        parts.pop()
    else:
        parts[-1] = stem
    return [part for part in parts if part]


def _dominant_source_root(paths: Sequence[str]) -> str | None:
    roots = Counter(parts[0].casefold() for path in paths if (parts := _path_parts(path)))
    if not roots:
        return None
    root, count = roots.most_common(1)[0]
    required = max(3, int(len(paths) * 0.55))
    return root if count >= required else None


def _capability_key(path: str, dominant_root: str | None) -> str | None:
    parts = _path_parts(path)
    if not parts:
        return None
    folded = [part.casefold() for part in parts]
    if folded[0] in TEST_ROOTS or _is_test_path(path):
        return None
    if dominant_root and folded[0] == dominant_root and len(parts) > 1:
        return folded[1]
    return folded[0]


def _is_test_path(path: str) -> bool:
    parts = [part.casefold() for part in PurePosixPath(path.replace("\\", "/")).parts]
    filename = parts[-1] if parts else ""
    return bool(
        any(part in TEST_ROOTS for part in parts[:-1])
        or filename.startswith("test_")
        or filename.endswith("_test.py")
        or ".spec." in filename
        or ".test." in filename
    )


def _is_public_owner(node: MemoryNode) -> bool:
    name = _node_label(node).rsplit(".", 1)[-1]
    if node.type == "Endpoint":
        return True
    return not name.startswith("_")


def _capability_name(group: _CapabilityGroup, degree_by_id: dict[str, int]) -> str:
    fallback = _humanize_symbol(group.key)
    if group.key not in GENERIC_CAPABILITY_NAMES:
        return fallback
    owners = sorted(
        (node for node in group.nodes.values() if node.type in OWNER_NODE_TYPES and _is_public_owner(node)),
        key=lambda node: _owner_rank(node, degree_by_id),
    )
    concepts = []
    for node in owners:
        concept = _owner_concept(_node_label(node))
        if concept and concept.casefold() not in {item.casefold() for item in concepts}:
            concepts.append(concept)
        if len(concepts) >= 3:
            break
    return " / ".join(concepts) if concepts else fallback


def _owner_concept(value: str) -> str:
    short = value.rsplit(".", 1)[-1]
    for suffix in SYMBOL_SUFFIXES:
        if short.endswith(suffix) and len(short) > len(suffix):
            short = short[: -len(suffix)]
            break
    return _humanize_symbol(short)


def _responsibilities(nodes: Sequence[MemoryNode], *, fallback: str) -> list[str]:
    values = []
    for node in nodes:
        value = _humanize_symbol(_node_label(node).rsplit(".", 1)[-1])
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
        if len(values) >= 5:
            break
    return values or [_humanize_symbol(fallback)]


def _infer_layer(key: str, paths: Counter[str]) -> str:
    parent_paths = " ".join(str(PurePosixPath(path).parent) for path in paths)
    tokens = _tokens(f"{key} {parent_paths}")
    for layer in ("interface", "application", "domain", "infrastructure"):
        if tokens & LAYER_TOKENS[layer]:
            return layer
    return "core"


def _associated_tests(key: str, responsibilities: Sequence[str], test_paths: Sequence[str]) -> list[str]:
    match_tokens = _tokens(f"{key} {' '.join(responsibilities)}") - GENERIC_CAPABILITY_NAMES
    if not match_tokens:
        return []
    scored = []
    for path in test_paths:
        overlap = len(match_tokens & _tokens(path))
        if overlap:
            scored.append((overlap, path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _score, path in scored[:6]]


def _capability_purpose(name: str, responsibilities: Sequence[str], dependencies: Sequence[str]) -> str:
    details = ", ".join(responsibilities[:4]) if responsibilities else name.casefold()
    purpose = f"Owns {name.casefold()} behavior through {details}."
    if dependencies:
        purpose += f" Collaborates with {', '.join(dependencies[:3])}."
    return purpose


def _workflow_trigger_reason(node: MemoryNode) -> str | None:
    """Return why a node is a workflow trigger, without using focus as admission."""

    if node.type not in {"Endpoint", "Function", "Method"}:
        return None
    path = _node_path(node)
    if _is_test_path(path) or not _is_public_owner(node):
        return None
    short_name = _node_label(node).rsplit(".", 1)[-1]
    if short_name in NON_WORKFLOW_NAMES:
        return None
    if node.type == "Endpoint":
        return "explicit endpoint trigger"
    path_tokens = _tokens(_node_path(node))
    role_values = []
    for property_name in ("roles", "semantic_roles"):
        value = node.properties.get(property_name) or []
        if isinstance(value, str):
            role_values.append(value)
        else:
            role_values.extend(str(item) for item in value)
    role_tokens = _tokens(" ".join(role_values))
    if role_tokens & ENTRY_ROLE_TOKENS:
        return "explicit entrypoint role"
    if path_tokens & LAYER_TOKENS["interface"]:
        return "interface boundary convention"
    if short_name in ENTRY_NAMES:
        return "process entrypoint convention"
    return None


def _is_workflow_participant(node: MemoryNode | None, project_id: str) -> bool:
    if node is None or node.type not in WORKFLOW_PARTICIPANT_TYPES:
        return False
    if node.status not in ACTIVE_STATUSES or node.properties.get("project_id") != project_id:
        return False
    if _is_test_path(_node_path(node)) or not _is_public_owner(node):
        return False
    short_name = _node_label(node).rsplit(".", 1)[-1]
    return short_name not in NON_WORKFLOW_NAMES


def _workflow_target_is_semantic(
    anchor: MemoryNode,
    target: MemoryNode,
    *,
    depth: int,
) -> bool:
    if depth <= 1:
        return True
    anchor_tokens = _workflow_concept_tokens(anchor)
    target_tokens = _tokens(_node_label(target).rsplit(".", 1)[-1])
    if anchor_tokens & target_tokens:
        return True
    short_name = _node_label(target).rsplit(".", 1)[-1]
    return target.type in {"Class", "Interface"} and short_name.endswith(
        SEMANTIC_WORKFLOW_SUFFIXES
    )


def _module_hint_matches_path(module_hint: str, path: str) -> bool:
    normalized_hint = module_hint.replace(".", "/").strip("/").casefold()
    normalized_path = path.replace("\\", "/").strip("/").casefold()
    suffix = PurePosixPath(normalized_path).suffix
    if suffix:
        normalized_path = normalized_path[: -len(suffix)]
    if normalized_path.endswith("/__init__"):
        normalized_path = normalized_path[: -len("/__init__")]
    return bool(
        normalized_hint
        and (
            normalized_path == normalized_hint
            or normalized_path.endswith(f"/{normalized_hint}")
        )
    )


def _source_references_name(source_text: str, name: str) -> bool:
    if not source_text or not name:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
        source_text,
    ) is not None


def _implementation_body_text(node: MemoryNode, source_text: str) -> str:
    if node.type not in {"Class", "Function", "Method"}:
        return source_text
    lines = source_text.splitlines()
    if not lines:
        return ""
    signature_started = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not signature_started and (
            stripped.startswith(("def ", "async def ", "class "))
            or stripped.startswith("@")
        ):
            signature_started = True
        if signature_started and stripped.endswith(":"):
            return "\n".join(lines[index + 1 :])
    return source_text


def _workflow_concept_tokens(node: MemoryNode) -> set[str]:
    tokens = _tokens(_node_label(node).rsplit(".", 1)[-1]) - WORKFLOW_GENERIC_TOKENS
    if tokens:
        return tokens
    return _tokens(f"{_node_label(node)} {_node_path(node)}") - WORKFLOW_GENERIC_TOKENS


def _workflow_documentation_evidence(
    documentation_nodes: Sequence[MemoryNode],
    concept_tokens: set[str],
) -> list[CodeEvidence]:
    if not concept_tokens:
        return []
    required_overlap = 1 if len(concept_tokens) == 1 else 2
    ranked = []
    for node in documentation_nodes:
        searchable = " ".join(
            (
                _node_label(node),
                str(node.text or ""),
                str(node.properties.get("section_path") or ""),
                _node_path(node),
            )
        )
        overlap = len(concept_tokens & _tokens(searchable))
        if overlap < required_overlap:
            continue
        score = overlap / max(1, len(concept_tokens))
        ranked.append(
            (
                -score,
                _node_path(node),
                node.id,
                _evidence(
                    node,
                    reason="documentation corroborates workflow intent",
                    score=score,
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return _dedupe_evidence(item[3] for item in ranked)[:3]


def _workflow_docstring_evidence(
    participants: Iterable[MemoryNode],
    concept_tokens: set[str],
) -> list[CodeEvidence]:
    evidence = []
    for node in participants:
        text = str(node.text or "").strip()
        if not text:
            continue
        searchable_tokens = _tokens(f"{_node_label(node)} {text}")
        if concept_tokens and not concept_tokens & searchable_tokens:
            continue
        evidence.append(
            _evidence(
                node,
                reason="docstring corroborates workflow intent",
            )
        )
    return _dedupe_evidence(evidence)


def _workflow_candidates_overlap(
    left: _WorkflowCandidate,
    right: _WorkflowCandidate,
) -> bool:
    left_ids = set(left.members)
    right_ids = set(right.members)
    shared = len(left_ids & right_ids)
    if not shared:
        return False
    containment = shared / min(len(left_ids), len(right_ids))
    if containment >= 0.75:
        return True
    left_tokens = _workflow_concept_tokens(left.anchor)
    right_tokens = _workflow_concept_tokens(right.anchor)
    token_union = left_tokens | right_tokens
    token_overlap = len(left_tokens & right_tokens) / max(1, len(token_union))
    return token_overlap >= 0.5 and shared / max(len(left_ids), len(right_ids)) >= 0.5


def _workflow_role(node: MemoryNode, *, is_trigger: bool) -> str:
    if is_trigger:
        return "trigger"
    concept = _node_label(node).rsplit(".", 1)[-1].casefold()
    if node.type == "Endpoint":
        return "interface"
    if concept.endswith(("repository", "store", "gateway", "adapter", "client")):
        return "infrastructure"
    if concept.endswith(("service", "usecase", "workflow", "handler", "coordinator", "manager")):
        return "orchestration"
    if concept.endswith(("policy", "rule", "validator")):
        return "policy"
    return "participant"


def _workflow_inputs(anchor: MemoryNode) -> list[str]:
    values = anchor.properties.get("args") or []
    if isinstance(values, str):
        values = [values]
    inputs = []
    for value in values:
        normalized = str(value).strip()
        name = normalized.split(":", 1)[0].split("=", 1)[0].strip().lstrip("*")
        if normalized and name not in {"self", "cls"}:
            inputs.append(normalized)
    return list(dict.fromkeys(inputs))


def _workflow_invariants(nodes: Iterable[MemoryNode]) -> list[str]:
    invariants = []
    for node in nodes:
        text = str(node.text or "").strip()
        for sentence in _sentences(text):
            if _tokens(sentence) & INVARIANT_TOKENS:
                invariants.append(sentence)
        decorators = node.properties.get("decorators") or []
        if isinstance(decorators, str):
            decorators = [decorators]
        for decorator in decorators:
            value = str(decorator).strip()
            if value and _tokens(value) & INVARIANT_TOKENS:
                invariants.append(f"Enforced by {value}")
    return list(dict.fromkeys(invariants))[:5]


def _workflow_intent(anchor: MemoryNode, participants: Sequence[MemoryNode]) -> str:
    for sentence in _sentences(str(anchor.text or "")):
        if len(_tokens(sentence)) >= 4:
            return sentence
    name = _humanize_symbol(_node_label(anchor))
    collaborators = []
    for participant in participants:
        if participant.id == anchor.id:
            continue
        concept = _owner_concept(_node_label(participant))
        if concept and concept.casefold() not in {item.casefold() for item in collaborators}:
            collaborators.append(concept)
        if len(collaborators) >= 4:
            break
    if collaborators:
        return f"Implements {name.casefold()} through {', '.join(collaborators)}."
    return f"Implements {name.casefold()} from an explicit repository boundary."


def _sentences(text: str) -> list[str]:
    values = []
    for item in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
        sentence = " ".join(item.split()).strip()
        if 4 <= len(sentence) <= 280:
            values.append(sentence)
    return values


def _evidence(
    node: MemoryNode,
    *,
    reason: str,
    score: float | None = None,
) -> CodeEvidence:
    line_start = _int_or_none(node.properties.get("line_start") or node.properties.get("start_line"))
    line_end = _int_or_none(node.properties.get("line_end") or node.properties.get("end_line"))
    return CodeEvidence(
        node_id=node.id,
        node_type=node.type,
        label=_node_label(node),
        path=_node_path(node),
        line_start=line_start,
        line_end=line_end,
        reason=reason,
        score=score,
    )


def _dedupe_evidence(items: Iterable[CodeEvidence]) -> list[CodeEvidence]:
    result = []
    seen: set[tuple[str, str, int | None]] = set()
    for item in items:
        key = (item.label, item.path, item.line_start)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _owner_rank(node: MemoryNode, degree_by_id: dict[str, int]) -> tuple[bool, int, str, str]:
    short_name = _node_label(node).rsplit(".", 1)[-1].casefold()
    return (
        short_name in GENERIC_OWNER_NAMES,
        -degree_by_id.get(node.id, 0),
        _node_label(node).casefold(),
        node.id,
    )


def _tokens(value: str) -> set[str]:
    expanded = CAMEL_RE.sub(" ", value.replace("_", " ").replace("-", " "))
    return {match.group(0).casefold() for match in WORD_RE.finditer(expanded)}


def _humanize_symbol(value: str) -> str:
    short = value.rsplit(".", 1)[-1]
    expanded = CAMEL_RE.sub(" ", short.replace("_", " ").replace("-", " "))
    words = [match.group(0) for match in WORD_RE.finditer(expanded)]
    if not words:
        return short
    return " ".join(words).strip().title()


def _int_or_none(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
