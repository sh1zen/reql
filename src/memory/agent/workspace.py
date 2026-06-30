"""Project-local working memory graph for coding agents."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import posixpath
import time
from typing import Any, Iterable
from uuid import uuid4

from api.memory_graph import MemoryGraph
from memory.config import REQLConfig
from memory.domain.exceptions import StorageError
from memory.domain.ids import stable_id
from memory.domain.models import MemoryEdge, MemoryNode
from memory.domain.timeutils import parse_dt, utcnow_iso
from memory.storage import BlockGraphStore


AGENT_STORAGE_FILE = "agent.reql"
AGENT_BUS_STORAGE_FILE = "agent-bus.reql"
AGENT_SCOPE_DIR = "agents"
DEFAULT_AGENT_ID = "master"
BUS_NODE_ID = "agent:bus"
WORKSPACE_NODE_ID = "agent:workspace"
AGENT_LOCK_TIMEOUT_SECONDS = 2.0
AGENT_READ_LOCK_TIMEOUT_SECONDS = 10.0
AGENT_LOCK_RETRY_ATTEMPTS = 3
AGENT_LOCK_RETRY_DELAY_SECONDS = 0.25
AGENT_NODE_TYPES = {"note", "task", "decision", "finding", "file", "symbol", "risk", "plan", "session"}
AGENT_RELATIONS = {
    "depends_on",
    "blocks",
    "implements",
    "touches",
    "explains",
    "derived_from",
    "related_to",
    "replaces",
    "conflicts_with",
}
SYMBOL_TYPES = {
    "Class",
    "Function",
    "Method",
    "Interface",
    "Struct",
    "Enum",
    "Trait",
    "Module",
    "Variable",
    "Constant",
}
FILE_TYPES = {"SourceArtifact", "Artifact", "Document", "File"}
FRAGMENT_TYPES = {"SourceFragment"}
STATIC_FINDING_TYPES = {"StaticAnalysisFinding"}


@dataclass(frozen=True, slots=True)
class AgentWorkspacePaths:
    standard_storage: Path
    agent_storage: Path
    bus_storage: Path


class AgentWorkspace:
    """Service facade for a separate agent working graph.

    The standard REQL graph is only read during derivation. All agent-created
    operational memory is persisted in ``agent_storage``.
    """

    def __init__(
        self,
        standard_storage: str | Path,
        *,
        agent_id: str | None = None,
        agent_storage: str | Path | None = None,
        bus_storage: str | Path | None = None,
        config: REQLConfig | None = None,
    ) -> None:
        standard_path = Path(standard_storage).expanduser().resolve(strict=False)
        resolved_bus_storage = (
            Path(bus_storage).expanduser().resolve(strict=False)
            if bus_storage is not None
            else self.default_bus_storage(standard_path)
        )
        self.agent_id = self._normalize_agent_id(
            agent_id or self._read_current_agent_id(resolved_bus_storage) or DEFAULT_AGENT_ID
        )
        self.paths = AgentWorkspacePaths(
            standard_storage=standard_path,
            agent_storage=Path(agent_storage).expanduser().resolve(strict=False)
            if agent_storage is not None
            else self.agent_storage_for(standard_path, self.agent_id),
            bus_storage=resolved_bus_storage,
        )
        self.config = config

    @staticmethod
    def default_agent_storage(standard_storage: str | Path) -> Path:
        path = Path(standard_storage).expanduser().resolve(strict=False)
        return path.with_name(AGENT_STORAGE_FILE)

    @staticmethod
    def default_bus_storage(standard_storage: str | Path) -> Path:
        path = Path(standard_storage).expanduser().resolve(strict=False)
        return path.with_name(AGENT_BUS_STORAGE_FILE)

    @classmethod
    def new_agent_id(cls) -> str:
        return f"agent:{uuid4().hex[:12]}"

    @classmethod
    def agent_storage_for(cls, standard_storage: str | Path, agent_id: str) -> Path:
        normalized = cls._normalize_agent_id(agent_id)
        if normalized == DEFAULT_AGENT_ID:
            return cls.default_agent_storage(standard_storage)
        path = Path(standard_storage).expanduser().resolve(strict=False)
        return path.with_name(AGENT_SCOPE_DIR) / f"{cls._safe_agent_file_stem(normalized)}.reql"

    def exists(self) -> bool:
        return self.paths.agent_storage.exists() and self.paths.agent_storage.stat().st_size > 0

    def init(self) -> dict[str, Any]:
        result = self._recreate()
        self._register_agent(status="active")
        return result

    def reset(self) -> dict[str, Any]:
        result = self._recreate()
        self._register_agent(status="active")
        return result

    def sync(self) -> dict[str, Any]:
        if not self.exists():
            raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
        standard = MemoryGraph.open(self.paths.standard_storage, config=self.config, read_only=True)
        agent = self._open_agent()
        try:
            standard_nodes = standard.store.all_nodes()
            standard_edges = standard.store.all_edges()
            derived_nodes, derived_edges = self._derive_standard_graph(standard_nodes, standard_edges)
            derived_nodes_by_id = {node.id: node for node in derived_nodes}
            derived_edges_by_id = {edge.id: edge for edge in derived_edges}
            synced_at = utcnow_iso()
            workspace = agent.get_node(WORKSPACE_NODE_ID)
            initialized_at = (
                workspace.properties.get("initialized_at")
                if workspace is not None
                else synced_at
            )
            workspace_props = dict(workspace.properties) if workspace is not None else {}
            workspace_props.update(
                {
                    "format": "reql-agent-workspace-v1",
                    "source": "system",
                    "agent_id": self.agent_id,
                    "standard_storage": str(self.paths.standard_storage),
                    "agent_storage": str(self.paths.agent_storage),
                    "bus_storage": str(self.paths.bus_storage),
                    "initialized_at": initialized_at,
                    "synced_at": synced_at,
                    "derived_node_count": len(derived_nodes),
                    "derived_relation_count": len(derived_edges),
                }
            )
            workspace_node = MemoryNode(
                id=WORKSPACE_NODE_ID,
                type="AgentWorkspace",
                label="Agent Workspace",
                text="Project-local working memory for coding agents.",
                canonical_key=WORKSPACE_NODE_ID,
                properties=workspace_props,
                status="active",
                created_at=workspace.created_at if workspace is not None else synced_at,
                updated_at=synced_at,
            )

            existing_nodes = agent.store.all_nodes()
            existing_edges = agent.store.all_edges()
            existing_derived_nodes = {
                node.id: node
                for node in existing_nodes
                if node.id != WORKSPACE_NODE_ID and node.properties.get("source") == "standard"
            }
            existing_derived_edges = {
                edge.id: edge
                for edge in existing_edges
                if edge.properties.get("source") == "standard"
            }
            stale_node_ids = sorted(set(existing_derived_nodes).difference(derived_nodes_by_id))
            stale_edge_ids = sorted(set(existing_derived_edges).difference(derived_edges_by_id))
            changed_node_ids = sorted(
                node_id
                for node_id, node in derived_nodes_by_id.items()
                if node_id in existing_derived_nodes and not self._derived_nodes_equivalent(existing_derived_nodes[node_id], node)
            )
            changed_edge_ids = sorted(
                edge_id
                for edge_id, edge in derived_edges_by_id.items()
                if edge_id in existing_derived_edges and not self._derived_edges_equivalent(existing_derived_edges[edge_id], edge)
            )
            new_node_ids = sorted(set(derived_nodes_by_id).difference(existing_derived_nodes))
            new_edge_ids = sorted(set(derived_edges_by_id).difference(existing_derived_edges))
            nodes_to_upsert = [derived_nodes_by_id[node_id] for node_id in [*new_node_ids, *changed_node_ids]]
            edges_to_upsert = [derived_edges_by_id[edge_id] for edge_id in [*new_edge_ids, *changed_edge_ids]]
            preserved_agent_nodes = sum(
                1
                for node in existing_nodes
                if node.id != WORKSPACE_NODE_ID and node.properties.get("source") != "standard"
            )
            preserved_agent_edges = sum(1 for edge in existing_edges if edge.properties.get("source") != "standard")

            with agent.store.transaction():
                for edge_id in stale_edge_ids:
                    agent.store.remove_edge(edge_id)
                for edge_id in changed_edge_ids:
                    agent.store.remove_edge(edge_id)
                for node_id in stale_node_ids:
                    agent.store.remove_node(node_id)
                for node_id in changed_node_ids:
                    agent.store.remove_node(node_id)
                agent.store.batch_upsert_nodes([workspace_node, *nodes_to_upsert])
                agent.store.batch_upsert_edges(edges_to_upsert)
            return {
                "synced": True,
                "agent_id": self.agent_id,
                "standard_storage": str(self.paths.standard_storage),
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": initialized_at,
                "synced_at": synced_at,
                "derived_nodes": len(derived_nodes),
                "derived_relations": len(derived_edges),
                "removed_derived_nodes": len(stale_node_ids),
                "removed_derived_relations": len(stale_edge_ids),
                "new_derived_nodes": len(new_node_ids),
                "new_derived_relations": len(new_edge_ids),
                "updated_derived_nodes": len(changed_node_ids),
                "updated_derived_relations": len(changed_edge_ids),
                "preserved_agent_nodes": preserved_agent_nodes,
                "preserved_agent_relations": preserved_agent_edges,
            }
        finally:
            agent.close()
            standard.close()

    def status(self) -> dict[str, Any]:
        if not self.exists():
            return {
                "exists": False,
                "agent_id": self.agent_id,
                "standard_storage": str(self.paths.standard_storage),
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": None,
                "nodes": 0,
                "relations": 0,
                "derived_nodes": 0,
                "agent_nodes": 0,
            }
        graph = self._open_agent(read_only=True)
        try:
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            nodes = graph.store.all_nodes()
            edges = graph.store.all_edges()
            derived_nodes = [node for node in nodes if node.properties.get("source") == "standard"]
            agent_nodes = [
                node
                for node in nodes
                if node.id != WORKSPACE_NODE_ID and node.properties.get("source") != "standard"
            ]
            return {
                "exists": True,
                "agent_id": self.agent_id,
                "standard_storage": str(self.paths.standard_storage),
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": workspace.properties.get("initialized_at") if workspace else None,
                "current_session_id": workspace.properties.get("current_session_id") if workspace else None,
                "current_session_title": workspace.properties.get("current_session_title") if workspace else None,
                "nodes": len(nodes),
                "relations": len(edges),
                "derived_nodes": len(derived_nodes),
                "agent_nodes": len(agent_nodes),
                "metadata": dict(workspace.properties) if workspace else {},
            }
        finally:
            graph.close()

    def add_note(self, text: str) -> dict[str, Any]:
        return self.add_node("note", text)

    def start_session(self, title: str) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("Agent session title must not be empty")
        graph = self._require_agent()
        try:
            with graph.store.transaction():
                now = utcnow_iso()
                workspace = graph.get_node(WORKSPACE_NODE_ID)
                if workspace is None:
                    raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
                workspace_props = dict(workspace.properties)
                previous_id = str(workspace_props.get("current_session_id") or "")
                if previous_id:
                    previous = graph.get_node(previous_id)
                    if previous is not None and previous.type == "session" and previous.status == "active":
                        previous_props = dict(previous.properties)
                        previous_props["ended_at"] = now
                        previous_props["is_current"] = False
                        graph.store.update_node_fields(previous.id, status="closed", properties=previous_props)
                session_id = stable_id("agent:session", now, title)
                session = MemoryNode(
                    id=session_id,
                    type="session",
                    label=title,
                    text=title,
                    canonical_key=stable_id("agent-session-key", now, title),
                    properties={
                        "content": title,
                        "title": title,
                        "metadata": {},
                        "source": "agent",
                        "session_id": session_id,
                        "session_title": title,
                        "started_at": now,
                        "is_current": True,
                    },
                    status="active",
                    created_at=now,
                    updated_at=now,
                    salience=0.6,
                    confidence=1.0,
                )
                stored, created = graph.add_node(session)
                workspace_props.update(
                    {
                        "current_session_id": stored.id,
                        "current_session_title": title,
                        "current_session_started_at": now,
                    }
                )
                graph.store.update_node_fields(workspace.id, properties=workspace_props)
            return {"created": created, "session": self._node_payload(stored)}
        finally:
            graph.close()

    def add_task(self, description: str) -> dict[str, Any]:
        return self.add_node("task", description, status="open")

    def complete_task(self, node_id: str) -> dict[str, Any]:
        graph = self._require_agent()
        try:
            return self._complete_task_in_graph(graph, node_id)
        finally:
            graph.close()

    def add_decision(self, text: str) -> dict[str, Any]:
        return self.add_node("decision", text)

    def add_finding(self, text: str) -> dict[str, Any]:
        return self.add_node("finding", text)

    def add_node(
        self,
        node_type: str,
        content: str,
        *,
        title: str | None = None,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        graph = self._require_agent()
        try:
            return self._create_agent_node(graph, node_type, content, title=title, status=status, metadata=metadata)
        finally:
            graph.close()

    def link(self, from_id: str, to_id: str, relation: str) -> dict[str, Any]:
        graph = self._require_agent()
        try:
            return self._link_in_graph(graph, from_id, to_id, relation)
        finally:
            graph.close()

    def link_many(self, from_id: str, to_ids: Iterable[str], relation: str) -> dict[str, Any]:
        relation = relation.strip().casefold()
        if relation not in AGENT_RELATIONS:
            raise ValueError(f"Unsupported agent relation: {relation}")
        target_ids = [str(to_id).strip() for to_id in to_ids if str(to_id).strip()]
        if not target_ids:
            raise ValueError("At least one link target is required")
        graph = self._require_agent()
        try:
            left = graph.get_node(from_id)
            if left is None:
                raise ValueError(f"Link source not found in agent graph: {from_id}")
            targets_by_id = {node.id: node for node in graph.store.get_nodes(target_ids)}
            for to_id in target_ids:
                if to_id not in targets_by_id:
                    raise ValueError(f"Link target not found in agent graph: {to_id}")
            session_props = self._current_session_properties(graph)
            edges = [self._agent_edge(from_id, to_id, relation, session_props=session_props) for to_id in target_ids]
            with graph.store.transaction():
                results = graph.store.batch_upsert_edges(edges)
            relations = [self._edge_payload(edge) for edge, _ in results]
            return {
                "created": sum(1 for _, created in results if created),
                "updated": sum(1 for _, created in results if not created),
                "relations": relations,
            }
        finally:
            graph.close()

    def link_task(
        self,
        *,
        task_id: str | None = None,
        file_path: str | None = None,
        relation: str = "touches",
    ) -> dict[str, Any]:
        if not file_path:
            raise ValueError("A file path is required")
        graph = self._require_agent()
        try:
            with graph.store.transaction():
                task = self._resolve_open_task_for_link(graph, task_id)
                target = self._resolve_file_node_by_path(graph, file_path)
                linked = self._link_in_graph(graph, task.id, target.id, relation)
                return {
                    **linked,
                    "task": self._node_payload(task, include_metadata=False),
                    "target": self._node_payload(target, include_metadata=False),
                }
        finally:
            graph.close()

    def batch(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        if not operations:
            raise ValueError("Batch must contain at least one operation")
        graph = self._require_agent()
        aliases: dict[str, str] = {}
        results: list[dict[str, Any]] = []
        try:
            with graph.store.transaction():
                for index, operation in enumerate(operations):
                    if not isinstance(operation, dict):
                        raise ValueError(f"Batch operation {index} must be an object")
                    result = self._run_batch_operation(graph, operation, aliases)
                    alias = str(operation.get("as") or "").strip()
                    if alias:
                        item_id = self._batch_result_id(result)
                        if item_id is None:
                            raise ValueError(f"Batch operation {index} cannot be assigned alias {alias!r}")
                        aliases[alias] = item_id
                    results.append(result)
        finally:
            graph.close()
        return {
            "operations": len(operations),
            "results": results,
            "aliases": dict(aliases),
        }

    def search(
        self,
        query: str,
        *,
        node_type: str | None = None,
        status: str | None = None,
        limit: int = 20,
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        graph = self._require_agent(read_only=True)
        try:
            node_types = {node_type} if node_type else None
            matches = graph.store.lexical_search(query, top_k=max(1, limit * 3), node_types=node_types, include_archived=True)
            items: list[dict[str, Any]] = []
            for node, score in matches:
                if node.id == WORKSPACE_NODE_ID:
                    continue
                if status and node.status != status:
                    continue
                items.append({"score": score, "node": self._node_payload(node, include_metadata=include_metadata)})
                if len(items) >= limit:
                    break
            return {"query": query, "results": items}
        finally:
            graph.close()

    def show(self, item_id: str) -> dict[str, Any]:
        graph = self._require_agent(read_only=True)
        try:
            node = graph.get_node(item_id)
            if node is not None:
                outgoing = [self._edge_payload(edge) for edge in graph.store.get_edges(from_id=item_id, limit=100)]
                incoming = [self._edge_payload(edge) for edge in graph.store.get_edges(to_id=item_id, limit=100)]
                return {"kind": "node", "node": self._node_payload(node), "outgoing": outgoing, "incoming": incoming}
            edge = graph.store.get_edge(item_id)
            if edge is not None:
                return {"kind": "relation", "relation": self._edge_payload(edge)}
            raise ValueError(f"Agent item not found: {item_id}")
        finally:
            graph.close()

    def list_items(
        self,
        *,
        node_type: str | None = None,
        status: str | None = None,
        relation: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        graph = self._require_agent()
        try:
            since_dt = parse_dt(since) if since else None
            nodes: list[dict[str, Any]] = []
            for node in graph.store.all_nodes():
                if node.id == WORKSPACE_NODE_ID:
                    continue
                if node_type and node.type != node_type:
                    continue
                if status and node.status != status:
                    continue
                if since_dt and (parse_dt(node.updated_at) or parse_dt(node.created_at)) < since_dt:
                    continue
                nodes.append(self._node_payload(node))
            nodes.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")), reverse=True)
            listed_node_ids = {str(item["id"]) for item in nodes[:limit]}
            has_node_filter = bool(node_type or status)

            edges: list[dict[str, Any]] = []
            for edge in graph.store.all_edges():
                if relation and edge.type != relation:
                    continue
                if not relation and edge.properties.get("source") != "agent":
                    continue
                if has_node_filter and listed_node_ids and edge.from_id not in listed_node_ids and edge.to_id not in listed_node_ids:
                    continue
                if since_dt and parse_dt(edge.updated_at) and parse_dt(edge.updated_at) < since_dt:
                    continue
                edges.append(self._edge_payload(edge))
            edges.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")), reverse=True)
            return {"nodes": nodes[:limit], "relations": edges[:limit]}
        finally:
            graph.close()

    def map(
        self,
        *,
        task_id: str | None = None,
        since: str | None = None,
        session: str | None = None,
        include_completed: bool = False,
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        graph = self._require_agent(read_only=True)
        try:
            since_dt = parse_dt(since) if since else None
            nodes = [node for node in graph.store.all_nodes() if node.id != WORKSPACE_NODE_ID]
            agent_nodes = [node for node in nodes if node.properties.get("source") != "standard"]
            edges = graph.store.all_edges()
            agent_edges = [
                edge
                for edge in edges
                if edge.properties.get("source") == "agent" and edge.type in AGENT_RELATIONS
            ]
            filters: dict[str, Any] = {}
            if session:
                session_id = self._resolve_session_selector(graph, session)
                filters["session"] = session_id
                by_id = {node.id: node for node in nodes}
                agent_edges = [edge for edge in agent_edges if edge.properties.get("session_id") == session_id]
                session_agent_ids = {
                    endpoint_id
                    for edge in agent_edges
                    for endpoint_id in (edge.from_id, edge.to_id)
                    if (endpoint := by_id.get(endpoint_id)) is not None and endpoint.properties.get("source") != "standard"
                }
                agent_nodes = [
                    node
                    for node in agent_nodes
                    if node.id == session_id or node.properties.get("session_id") == session_id or node.id in session_agent_ids
                ]
            if task_id:
                task = graph.get_node(task_id)
                if task is None or task.id == WORKSPACE_NODE_ID or task.properties.get("source") == "standard":
                    raise ValueError(f"Agent task not found: {task_id}")
                if task.type != "task":
                    raise ValueError(f"Agent node is not a task: {task_id}")
                filters["task"] = task_id
                by_id = {node.id: node for node in nodes}
                focus_ids = {task_id}
                changed = True
                while changed:
                    changed = False
                    for edge in agent_edges:
                        if edge.from_id not in focus_ids and edge.to_id not in focus_ids:
                            continue
                        for endpoint_id in (edge.from_id, edge.to_id):
                            endpoint = by_id.get(endpoint_id)
                            if endpoint is None or endpoint.properties.get("source") == "standard":
                                continue
                            if endpoint_id not in focus_ids:
                                focus_ids.add(endpoint_id)
                                changed = True
                agent_nodes = [node for node in agent_nodes if node.id in focus_ids]
                relevant_edges = [
                    edge
                    for edge in agent_edges
                    if edge.from_id in focus_ids or edge.to_id in focus_ids
                ]
            else:
                relevant_edges = list(agent_edges)
            if since_dt:
                filters["since"] = since
                recent_agent_ids = {
                    node.id
                    for node in agent_nodes
                    if self._node_is_since(node, since_dt)
                }
                if task_id:
                    recent_agent_ids.add(task_id)
                    agent_nodes = [
                        node
                        for node in agent_nodes
                        if node.id == task_id or self._node_is_since(node, since_dt)
                    ]
                else:
                    agent_nodes = [node for node in agent_nodes if node.id in recent_agent_ids]
                relevant_edges = [
                    edge
                    for edge in relevant_edges
                    if self._edge_is_since(edge, since_dt)
                    or edge.from_id in recent_agent_ids
                    or edge.to_id in recent_agent_ids
                ]
            tasks = [node for node in agent_nodes if node.type == "task" and node.status != "done"]
            completed_tasks = [node for node in agent_nodes if node.type == "task" and node.status == "done"]
            decisions = [node for node in agent_nodes if node.type == "decision"]
            endpoint_ids = {endpoint_id for edge in relevant_edges for endpoint_id in (edge.from_id, edge.to_id)}
            endpoint_nodes = {node.id: node for node in graph.store.get_nodes(sorted(endpoint_ids))}
            file_nodes_by_path = {
                path: node
                for node in nodes
                if self._map_node_kind(node) == "file"
                if (path := self._node_file_path(node))
            }
            files_by_key: dict[str, dict[str, Any]] = {}
            symbols_by_key: dict[str, dict[str, Any]] = {}
            for node in endpoint_nodes.values():
                kind = self._map_node_kind(node)
                if kind == "file":
                    files_by_key[node.id] = self._node_payload(node, include_metadata=include_metadata)
                    continue
                if kind == "symbol":
                    symbols_by_key[node.id] = self._node_payload(node, include_metadata=include_metadata)
                elif kind not in {"fragment", "static_finding"}:
                    continue
                path = self._node_file_path(node)
                if not path:
                    continue
                file_node = file_nodes_by_path.get(path)
                if file_node is not None:
                    files_by_key[file_node.id] = self._node_payload(file_node, include_metadata=include_metadata)
                else:
                    files_by_key[path] = self._related_file_payload(path, node, include_metadata=include_metadata)
            visible_node_ids = {
                node.id
                for node in tasks
                if node.type == "task" and node.status != "done"
            }
            if include_completed:
                filters["completed"] = True
                visible_node_ids.update(node.id for node in completed_tasks)
            visible_node_ids.update(node.id for node in decisions)
            visible_node_ids.update(files_by_key)
            visible_node_ids.update(symbols_by_key)
            essential_edges = [
                edge
                for edge in relevant_edges
                if edge.from_id in visible_node_ids and edge.to_id in visible_node_ids
            ]
            payload = {
                "open_tasks": [self._node_payload(node, include_metadata=include_metadata) for node in sorted(tasks, key=lambda item: item.updated_at, reverse=True)[:20]],
                "decisions": [self._node_payload(node, include_metadata=include_metadata) for node in sorted(decisions, key=lambda item: item.updated_at, reverse=True)[:20]],
                "files": sorted(files_by_key.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)[:20],
                "symbols": sorted(symbols_by_key.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)[:20],
                "relations": [self._edge_payload(edge, include_metadata=include_metadata) for edge in sorted(essential_edges, key=lambda item: item.updated_at, reverse=True)[:40]],
            }
            if include_completed:
                payload["completed_tasks"] = [
                    self._node_payload(node, include_metadata=include_metadata)
                    for node in sorted(completed_tasks, key=lambda item: item.updated_at, reverse=True)[:20]
                ]
            if filters:
                payload["filters"] = filters
            return payload
        finally:
            graph.close()

    def export(self, *, include_metadata: bool = False) -> dict[str, Any]:
        if not include_metadata:
            return {"format": "reql-agent-workspace-v1", **self.map(include_metadata=False)}
        graph = self._require_agent(read_only=True)
        try:
            payload = graph.export_json()
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            return {
                "format": "reql-agent-workspace-v1",
                "agent_id": self.agent_id,
                "standard_storage": str(self.paths.standard_storage),
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": workspace.properties.get("initialized_at") if workspace else None,
                "nodes": [self._node_payload(MemoryNode.from_dict(item), include_metadata=True) for item in payload["nodes"]],
                "relations": [self._edge_payload(MemoryEdge.from_dict(item), include_metadata=True) for item in payload["edges"]],
            }
        finally:
            graph.close()

    def bus(self, *, limit: int = 50, include_payloads: bool = False) -> dict[str, Any]:
        if not self.paths.bus_storage.exists() or self.paths.bus_storage.stat().st_size == 0:
            return {
                "format": "reql-agent-bus-v1",
                "bus_storage": str(self.paths.bus_storage),
                "current_agent_id": None,
                "agents": [],
                "messages": [],
                "handoffs": [],
            }
        graph = self._open_bus(read_only=True)
        try:
            bus_node = graph.get_node(BUS_NODE_ID)
            nodes = [node for node in graph.store.all_nodes() if node.id != BUS_NODE_ID]
            agents = [node for node in nodes if node.type == "agent"]
            messages = [node for node in nodes if node.type == "bus_message"]
            handoffs = [node for node in nodes if node.type == "handoff"]
            return {
                "format": "reql-agent-bus-v1",
                "bus_storage": str(self.paths.bus_storage),
                "current_agent_id": bus_node.properties.get("current_agent_id") if bus_node else None,
                "agents": [self._bus_node_payload(node) for node in sorted(agents, key=lambda item: item.updated_at, reverse=True)[:limit]],
                "messages": [self._bus_node_payload(node) for node in sorted(messages, key=lambda item: item.updated_at, reverse=True)[:limit]],
                "handoffs": [
                    self._bus_node_payload(node, include_payload=include_payloads)
                    for node in sorted(handoffs, key=lambda item: item.updated_at, reverse=True)[:limit]
                ],
            }
        finally:
            graph.close()

    def publish(self, text: str, *, kind: str = "note", target: str = "all") -> dict[str, Any]:
        content = text.strip()
        if not content:
            raise ValueError("Agent bus message must not be empty")
        kind = kind.strip().casefold() or "note"
        target = target.strip() or "all"
        graph = self._ensure_bus()
        try:
            with graph.store.transaction():
                now = utcnow_iso()
                bus_node = self._bus_workspace_node(graph, now)
                message = MemoryNode(
                    id=stable_id("agent-bus-message", now, self.agent_id, target, content),
                    type="bus_message",
                    label=self._title_from_content(content),
                    text=content,
                    canonical_key=stable_id("agent-bus-message-key", now, self.agent_id, target, content),
                    properties={
                        "source": "bus",
                        "kind": kind,
                        "agent_id": self.agent_id,
                        "target_agent_id": target,
                        "content": content,
                        "title": self._title_from_content(content),
                    },
                    status="active",
                    created_at=now,
                    updated_at=now,
                    salience=0.6,
                    confidence=1.0,
                )
                stored, created = graph.add_node(message)
                graph.store.update_node_fields(bus_node.id, updated_at=now, properties=bus_node.properties)
            return {"created": created, "message": self._bus_node_payload(stored)}
        finally:
            graph.close()

    def handoff(self, summary: str | None = None, *, target: str = DEFAULT_AGENT_ID) -> dict[str, Any]:
        target = target.strip() or DEFAULT_AGENT_ID
        summary_text = (summary or "").strip()
        try:
            snapshot = self.map(session="current")
        except ValueError:
            snapshot = self.map()
        if not summary_text:
            summary_text = f"Handoff from {self.agent_id}"
        graph = self._ensure_bus()
        try:
            with graph.store.transaction():
                now = utcnow_iso()
                self._bus_workspace_node(graph, now)
                node = MemoryNode(
                    id=stable_id("agent-handoff", now, self.agent_id, target, summary_text),
                    type="handoff",
                    label=self._title_from_content(summary_text),
                    text=summary_text,
                    canonical_key=stable_id("agent-handoff-key", now, self.agent_id, target, summary_text),
                    properties={
                        "source": "bus",
                        "agent_id": self.agent_id,
                        "target_agent_id": target,
                        "content": summary_text,
                        "title": self._title_from_content(summary_text),
                        "payload": snapshot,
                    },
                    status="active",
                    created_at=now,
                    updated_at=now,
                    salience=0.8,
                    confidence=1.0,
                )
                stored, created = graph.add_node(node)
                self._register_agent_in_graph(graph, status="completed", updated_at=now)
            return {"created": created, "handoff": self._bus_node_payload(stored)}
        finally:
            graph.close()

    def _add_node_in_graph(self, graph: MemoryGraph, node: MemoryNode) -> tuple[MemoryNode, bool]:
        return graph.add_node(node)

    def _complete_task_in_graph(self, graph: MemoryGraph, node_id: str) -> dict[str, Any]:
        node = graph.get_node(node_id)
        if node is None or node.id == WORKSPACE_NODE_ID:
            raise ValueError(f"Agent node not found: {node_id}")
        if node.type != "task":
            raise ValueError(f"Agent node is not a task: {node_id}")
        props = dict(node.properties)
        props["completed_at"] = utcnow_iso()
        updated = graph.store.update_node_fields(node.id, status="done", properties=props)
        if updated is None:
            raise ValueError(f"Agent node not found: {node_id}")
        return {"task": self._node_payload(updated)}

    def _resolve_open_task_for_link(self, graph: MemoryGraph, task_id: str | None) -> MemoryNode:
        if task_id:
            node = graph.get_node(task_id)
            if node is None or node.id == WORKSPACE_NODE_ID or node.properties.get("source") == "standard":
                raise ValueError(f"Agent task not found: {task_id}")
            if node.type != "task":
                raise ValueError(f"Agent node is not a task: {task_id}")
            if node.status == "done":
                raise ValueError(f"Agent task is already done: {task_id}")
            return node
        nodes = [
            node
            for node in graph.store.all_nodes()
            if node.id != WORKSPACE_NODE_ID
            and node.type == "task"
            and node.status != "done"
            and node.properties.get("source") != "standard"
        ]
        workspace = graph.get_node(WORKSPACE_NODE_ID)
        session_id = str(workspace.properties.get("current_session_id") or "").strip() if workspace is not None else ""
        if session_id:
            session_nodes = [node for node in nodes if node.properties.get("session_id") == session_id]
            if session_nodes:
                nodes = session_nodes
        if not nodes:
            raise ValueError("No open agent task found. Pass --task TASK_ID or create one with `reql agent task add ...`.")
        return sorted(nodes, key=lambda item: (item.updated_at, item.id), reverse=True)[0]

    def _resolve_file_node_by_path(self, graph: MemoryGraph, file_path: str) -> MemoryNode:
        lookup_keys = self._path_lookup_keys(file_path)
        if not lookup_keys:
            raise ValueError("File path must not be empty")
        matches = [
            node
            for node in graph.store.all_nodes()
            if self._map_node_kind(node) == "file" and lookup_keys.intersection(self._node_path_lookup_keys(node))
        ]
        if not matches:
            raise ValueError(f"File target not found in agent graph for path: {file_path}. Run `reql agent sync` after compiling new files.")
        matches.sort(key=lambda item: (self._file_node_match_priority(item), item.id))
        best = matches[0]
        tied = [node for node in matches if self._file_node_match_priority(node) == self._file_node_match_priority(best)]
        if len(tied) > 1:
            ids = ", ".join(node.id for node in tied[:5])
            raise ValueError(f"File path is ambiguous: {file_path} matches {ids}")
        return best

    def _file_node_match_priority(self, node: MemoryNode) -> int:
        standard_type = str(node.properties.get("standard_type") or node.type)
        priorities = {
            "File": 0,
            "SourceArtifact": 1,
            "Artifact": 2,
            "Document": 3,
        }
        return priorities.get(standard_type, 10)

    def _link_in_graph(self, graph: MemoryGraph, from_id: str, to_id: str, relation: str) -> dict[str, Any]:
        relation = relation.strip().casefold()
        if relation not in AGENT_RELATIONS:
            raise ValueError(f"Unsupported agent relation: {relation}")
        left = graph.get_node(from_id)
        right = graph.get_node(to_id)
        if left is None:
            raise ValueError(f"Link source not found in agent graph: {from_id}")
        if right is None:
            raise ValueError(f"Link target not found in agent graph: {to_id}")
        stored, created = graph.add_edge(self._agent_edge(from_id, to_id, relation, session_props=self._current_session_properties(graph)))
        return {"created": created, "relation": self._edge_payload(stored)}

    def _agent_edge(
        self,
        from_id: str,
        to_id: str,
        relation: str,
        *,
        session_props: dict[str, Any] | None = None,
    ) -> MemoryEdge:
        properties = {"source": "agent"}
        properties.update(session_props or {})
        return MemoryEdge(
            id=stable_id("agent-edge", from_id, relation, to_id),
            from_id=from_id,
            to_id=to_id,
            type=relation,
            origin="manual",
            properties=properties,
        )

    def _run_batch_operation(
        self,
        graph: MemoryGraph,
        operation: dict[str, Any],
        aliases: dict[str, str],
    ) -> dict[str, Any]:
        op = str(operation.get("op") or operation.get("action") or "").strip().casefold().replace("_", "-")
        if op in {"add", "note.add", "note-add"}:
            return self._batch_add_node(graph, "note", str(operation.get("text") or operation.get("content") or ""), operation)
        if op in {"task.add", "task-add"}:
            return self._batch_add_node(graph, "task", str(operation.get("description") or operation.get("text") or operation.get("content") or ""), operation, status="open")
        if op in {"decision.add", "decision-add"}:
            return self._batch_add_node(graph, "decision", str(operation.get("decision") or operation.get("text") or operation.get("content") or ""), operation)
        if op in {"finding.add", "finding-add"}:
            return self._batch_add_node(graph, "finding", str(operation.get("observation") or operation.get("text") or operation.get("content") or ""), operation)
        if op in {"task.done", "task-done", "done"}:
            node_id = self._resolve_batch_ref(str(operation.get("id") or operation.get("task_id") or ""), aliases)
            result = self._complete_task_in_graph(graph, node_id)
            return {"op": "task.done", **result}
        if op == "link":
            from_id = self._resolve_batch_ref(str(operation.get("from_id") or operation.get("from") or operation.get("source") or ""), aliases)
            to_id = self._resolve_batch_ref(str(operation.get("to_id") or operation.get("to") or operation.get("target") or ""), aliases)
            relation = str(operation.get("relation") or "")
            result = self._link_in_graph(graph, from_id, to_id, relation)
            return {"op": "link", **result}
        if op in {"link-many", "link.many"}:
            from_id = self._resolve_batch_ref(str(operation.get("from_id") or operation.get("from") or operation.get("source") or ""), aliases)
            raw_targets = operation.get("to_ids", operation.get("to", operation.get("targets", [])))
            if isinstance(raw_targets, str):
                target_ids = [raw_targets]
            elif isinstance(raw_targets, list):
                target_ids = [str(item) for item in raw_targets]
            else:
                raise ValueError("link-many targets must be a string or list")
            relation = str(operation.get("relation") or "")
            resolved_targets = [self._resolve_batch_ref(target_id, aliases) for target_id in target_ids]
            result = self._link_many_in_graph(graph, from_id, resolved_targets, relation)
            return {"op": "link-many", **result}
        raise ValueError(f"Unsupported batch operation: {op or '<missing>'}")

    def _batch_add_node(
        self,
        graph: MemoryGraph,
        node_type: str,
        content: str,
        operation: dict[str, Any],
        *,
        status: str = "active",
    ) -> dict[str, Any]:
        result = self._create_agent_node(
            graph,
            node_type,
            content,
            title=str(operation["title"]) if "title" in operation else None,
            status=str(operation.get("status") or status),
            metadata=operation.get("metadata") if isinstance(operation.get("metadata"), dict) else None,
        )
        return {"op": f"{node_type}.add" if node_type != "note" else "add", **result}

    def _create_agent_node(
        self,
        graph: MemoryGraph,
        node_type: str,
        content: str,
        *,
        title: str | None = None,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        node_type = node_type.strip().casefold()
        if node_type not in AGENT_NODE_TYPES:
            raise ValueError(f"Unsupported agent node type: {node_type}")
        content = content.strip()
        if not content:
            raise ValueError("Agent node content must not be empty")
        created_at = utcnow_iso()
        session_props = self._current_session_properties(graph)
        node = MemoryNode(
            id=stable_id(f"agent:{node_type}", created_at, content),
            type=node_type,
            label=title or self._title_from_content(content),
            text=content,
            canonical_key=stable_id("agent-key", node_type, created_at, content),
            properties={
                "content": content,
                "title": title or self._title_from_content(content),
                "metadata": dict(metadata or {}),
                "source": "agent",
                **session_props,
            },
            status=status,
            created_at=created_at,
            updated_at=created_at,
            salience=0.6,
            confidence=1.0,
        )
        stored, created = self._add_node_in_graph(graph, node)
        return {"created": created, "node": self._node_payload(stored)}

    def _link_many_in_graph(self, graph: MemoryGraph, from_id: str, to_ids: list[str], relation: str) -> dict[str, Any]:
        relation = relation.strip().casefold()
        if relation not in AGENT_RELATIONS:
            raise ValueError(f"Unsupported agent relation: {relation}")
        target_ids = [str(to_id).strip() for to_id in to_ids if str(to_id).strip()]
        if not target_ids:
            raise ValueError("At least one link target is required")
        if graph.get_node(from_id) is None:
            raise ValueError(f"Link source not found in agent graph: {from_id}")
        targets_by_id = {node.id: node for node in graph.store.get_nodes(target_ids)}
        for to_id in target_ids:
            if to_id not in targets_by_id:
                raise ValueError(f"Link target not found in agent graph: {to_id}")
        session_props = self._current_session_properties(graph)
        results = graph.store.batch_upsert_edges([self._agent_edge(from_id, to_id, relation, session_props=session_props) for to_id in target_ids])
        return {
            "created": sum(1 for _, created in results if created),
            "updated": sum(1 for _, created in results if not created),
            "relations": [self._edge_payload(edge) for edge, _ in results],
        }

    def _resolve_batch_ref(self, value: str, aliases: dict[str, str]) -> str:
        item = value.strip()
        if not item:
            raise ValueError("Batch reference must not be empty")
        if item.startswith("$"):
            alias = item[1:]
            if alias not in aliases:
                raise ValueError(f"Unknown batch alias: {item}")
            return aliases[alias]
        return item

    def _batch_result_id(self, result: dict[str, Any]) -> str | None:
        node = result.get("node") or result.get("task")
        if isinstance(node, dict) and node.get("id"):
            return str(node["id"])
        relation = result.get("relation")
        if isinstance(relation, dict) and relation.get("id"):
            return str(relation["id"])
        relations = result.get("relations")
        if isinstance(relations, list) and len(relations) == 1 and isinstance(relations[0], dict) and relations[0].get("id"):
            return str(relations[0]["id"])
        return None

    def _recreate(self) -> dict[str, Any]:
        self._remove_agent_store()
        standard = MemoryGraph.open(self.paths.standard_storage, config=self.config, read_only=True)
        agent = self._open_agent()
        try:
            standard_nodes = standard.store.all_nodes()
            standard_edges = standard.store.all_edges()
            derived_nodes, derived_edges = self._derive_standard_graph(standard_nodes, standard_edges)
            initialized_at = utcnow_iso()
            workspace = MemoryNode(
                id=WORKSPACE_NODE_ID,
                type="AgentWorkspace",
                label="Agent Workspace",
                text="Project-local working memory for coding agents.",
                canonical_key=WORKSPACE_NODE_ID,
                properties={
                    "format": "reql-agent-workspace-v1",
                    "source": "system",
                    "agent_id": self.agent_id,
                    "standard_storage": str(self.paths.standard_storage),
                    "agent_storage": str(self.paths.agent_storage),
                    "bus_storage": str(self.paths.bus_storage),
                    "initialized_at": initialized_at,
                    "derived_node_count": len(derived_nodes),
                    "derived_relation_count": len(derived_edges),
                },
                status="active",
                created_at=initialized_at,
                updated_at=initialized_at,
            )
            with agent.store.transaction():
                agent.store.batch_upsert_nodes([workspace, *derived_nodes])
                agent.store.batch_upsert_edges(derived_edges)
            return {
                "initialized": True,
                "agent_id": self.agent_id,
                "standard_storage": str(self.paths.standard_storage),
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": initialized_at,
                "derived_nodes": len(derived_nodes),
                "derived_relations": len(derived_edges),
            }
        finally:
            agent.close()
            standard.close()

    def _remove_agent_store(self) -> None:
        base = self.paths.agent_storage
        for path in self._agent_store_files(base):
            if path.exists():
                path.unlink()

    @classmethod
    def _normalize_agent_id(cls, agent_id: str) -> str:
        value = str(agent_id or "").strip()
        if not value:
            raise ValueError("Agent id must not be empty")
        return value

    @classmethod
    def _safe_agent_file_stem(cls, agent_id: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in agent_id)
        return safe.strip("._") or "agent"

    @classmethod
    def _read_current_agent_id(cls, bus_storage: Path) -> str | None:
        if not bus_storage.exists() or bus_storage.stat().st_size == 0:
            return None
        try:
            store = BlockGraphStore(bus_storage, read_only=True, lock_timeout_seconds=AGENT_READ_LOCK_TIMEOUT_SECONDS)
            try:
                graph = MemoryGraph(store)
                bus_node = graph.get_node(BUS_NODE_ID)
                if bus_node is None:
                    return None
                value = str(bus_node.properties.get("current_agent_id") or "").strip()
                return value or None
            finally:
                store.close()
        except StorageError as exc:
            if "locked" in str(exc).casefold():
                raise ValueError(
                    f"Agent bus is busy; could not read current agent id from {bus_storage}. "
                    "Retry the command, or pass `--agent AGENT_ID`/`REQL_AGENT_ID` explicitly."
                ) from exc
            return None

    def _register_agent(self, *, status: str) -> None:
        graph = self._ensure_bus()
        try:
            with graph.store.transaction():
                self._register_agent_in_graph(graph, status=status)
        finally:
            graph.close()

    def _register_agent_in_graph(self, graph: MemoryGraph, *, status: str, updated_at: str | None = None) -> MemoryNode:
        now = updated_at or utcnow_iso()
        bus_node = self._bus_workspace_node(graph, now)
        bus_props = dict(bus_node.properties)
        bus_props.update(
            {
                "current_agent_id": self.agent_id,
                "standard_storage": str(self.paths.standard_storage),
                "updated_at": now,
            }
        )
        graph.store.update_node_fields(bus_node.id, updated_at=now, properties=bus_props)
        node_id = stable_id("agent-identity", self.agent_id)
        existing = graph.get_node(node_id)
        props = dict(existing.properties) if existing is not None else {}
        props.update(
            {
                "source": "bus",
                "agent_id": self.agent_id,
                "agent_storage": str(self.paths.agent_storage),
                "standard_storage": str(self.paths.standard_storage),
                "role": DEFAULT_AGENT_ID if self.agent_id == DEFAULT_AGENT_ID else "worker",
                "title": self.agent_id,
                "content": self.agent_id,
                "last_seen_at": now,
            }
        )
        node = MemoryNode(
            id=node_id,
            type="agent",
            label=self.agent_id,
            text=self.agent_id,
            canonical_key=stable_id("agent-identity-key", self.agent_id),
            properties=props,
            status=status,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            salience=0.7,
            confidence=1.0,
        )
        stored, _ = graph.add_node(node)
        return stored

    def _derive_standard_graph(
        self,
        standard_nodes: Iterable[MemoryNode],
        standard_edges: Iterable[MemoryEdge],
    ) -> tuple[list[MemoryNode], list[MemoryEdge]]:
        derived_nodes = [self._derived_node(node) for node in standard_nodes]
        derived_ids = {node.id for node in derived_nodes}
        derived_edges = [
            self._derived_edge(edge)
            for edge in standard_edges
            if edge.from_id in derived_ids and edge.to_id in derived_ids
        ]
        return derived_nodes, derived_edges

    @staticmethod
    def _derived_nodes_equivalent(left: MemoryNode, right: MemoryNode) -> bool:
        return _normalized_derived_node_payload(left) == _normalized_derived_node_payload(right)

    @staticmethod
    def _derived_edges_equivalent(left: MemoryEdge, right: MemoryEdge) -> bool:
        return _normalized_derived_edge_payload(left) == _normalized_derived_edge_payload(right)

    def _agent_store_files(self, base: Path) -> Iterable[Path]:
        yield base
        yield base.with_name(f"{base.name}.wal")
        yield base.with_name(f"{base.name}.lock")
        yield base.with_suffix(base.suffix + ".lock")

    def _require_agent(self, *, read_only: bool = False) -> MemoryGraph:
        if not self.exists():
            raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
        return self._open_agent(read_only=read_only)

    def _open_agent(self, *, read_only: bool = False) -> MemoryGraph:
        self.paths.agent_storage.parent.mkdir(parents=True, exist_ok=True)
        last_error: StorageError | None = None
        for attempt in range(AGENT_LOCK_RETRY_ATTEMPTS):
            try:
                lock_timeout = AGENT_READ_LOCK_TIMEOUT_SECONDS if read_only else AGENT_LOCK_TIMEOUT_SECONDS
                store = BlockGraphStore(
                    self.paths.agent_storage,
                    read_only=read_only,
                    lock_timeout_seconds=lock_timeout,
                )
                try:
                    return MemoryGraph(store, config=self.config)
                except Exception:
                    store.close()
                    raise
            except StorageError as exc:
                if "locked" not in str(exc).casefold():
                    raise
                last_error = exc
                if attempt + 1 < AGENT_LOCK_RETRY_ATTEMPTS:
                    time.sleep(AGENT_LOCK_RETRY_DELAY_SECONDS)
        mode = "read" if read_only else "write"
        raise ValueError(
            f"Agent workspace is busy; could not acquire {mode} access to {self.paths.agent_storage}. "
            "Retry the command, or avoid running multiple `reql agent` commands in parallel."
        ) from last_error

    def _ensure_bus(self) -> MemoryGraph:
        graph = self._open_bus()
        try:
            if graph.get_node(BUS_NODE_ID) is None:
                with graph.store.transaction():
                    self._bus_workspace_node(graph, utcnow_iso())
            return graph
        except Exception:
            graph.close()
            raise

    def _open_bus(self, *, read_only: bool = False) -> MemoryGraph:
        self.paths.bus_storage.parent.mkdir(parents=True, exist_ok=True)
        last_error: StorageError | None = None
        for attempt in range(AGENT_LOCK_RETRY_ATTEMPTS):
            try:
                lock_timeout = AGENT_READ_LOCK_TIMEOUT_SECONDS if read_only else AGENT_LOCK_TIMEOUT_SECONDS
                store = BlockGraphStore(
                    self.paths.bus_storage,
                    read_only=read_only,
                    lock_timeout_seconds=lock_timeout,
                )
                try:
                    return MemoryGraph(store, config=self.config)
                except Exception:
                    store.close()
                    raise
            except StorageError as exc:
                if "locked" not in str(exc).casefold():
                    raise
                last_error = exc
                if attempt + 1 < AGENT_LOCK_RETRY_ATTEMPTS:
                    time.sleep(AGENT_LOCK_RETRY_DELAY_SECONDS)
        mode = "read" if read_only else "write"
        raise ValueError(
            f"Agent bus is busy; could not acquire {mode} access to {self.paths.bus_storage}. "
            "Retry the command, or avoid running multiple `reql agent` bus writes in parallel."
        ) from last_error

    def _bus_workspace_node(self, graph: MemoryGraph, now: str) -> MemoryNode:
        existing = graph.get_node(BUS_NODE_ID)
        props = dict(existing.properties) if existing is not None else {}
        props.update(
            {
                "format": "reql-agent-bus-v1",
                "source": "system",
                "bus_storage": str(self.paths.bus_storage),
                "standard_storage": str(self.paths.standard_storage),
            }
        )
        node = MemoryNode(
            id=BUS_NODE_ID,
            type="AgentBus",
            label="Agent Bus",
            text="Project-local shared bus for agent handoffs and shared messages.",
            canonical_key=BUS_NODE_ID,
            properties=props,
            status="active",
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            salience=0.5,
            confidence=1.0,
        )
        stored, _ = graph.add_node(node)
        return stored

    def _derived_node(self, node: MemoryNode) -> MemoryNode:
        node_type = self._agent_type_for_standard_node(node)
        props = {
            "source": "standard",
            "standard_id": node.id,
            "standard_type": node.type,
            "standard_status": node.status,
            "derived_from_storage": str(self.paths.standard_storage),
            "metadata": dict(node.properties),
        }
        title = node.label or node.properties.get("name") or node.properties.get("relative_path") or node.id
        props["title"] = str(title)
        if node.text:
            props["content"] = node.text
        return MemoryNode(
            id=node.id,
            type=node_type,
            label=str(title),
            text=node.text,
            canonical_key=stable_id("agent-standard-key", node.id),
            properties=props,
            status=node.status,
            created_at=node.created_at,
            updated_at=node.updated_at,
            salience=node.salience,
            confidence=node.confidence,
            stability=node.stability,
            utility=node.utility,
        )

    def _derived_edge(self, edge: MemoryEdge) -> MemoryEdge:
        props = dict(edge.properties)
        props.update(
            {
                "source": "standard",
                "standard_id": edge.id,
                "standard_type": edge.type,
                "derived_from_storage": str(self.paths.standard_storage),
            }
        )
        return MemoryEdge(
            id=edge.id,
            from_id=edge.from_id,
            to_id=edge.to_id,
            type=edge.type,
            weight=edge.weight,
            confidence=edge.confidence,
            polarity=edge.polarity,
            origin=edge.origin,
            properties=props,
            created_at=edge.created_at,
            updated_at=edge.updated_at,
        )

    def _agent_type_for_standard_node(self, node: MemoryNode) -> str:
        if node.type in SYMBOL_TYPES:
            return "symbol"
        if node.type in FILE_TYPES or node.properties.get("relative_path") or node.properties.get("path"):
            return "file"
        if node.type.casefold() in AGENT_NODE_TYPES:
            return node.type.casefold()
        return "note"

    def _node_payload(self, node: MemoryNode, *, include_metadata: bool = True) -> dict[str, Any]:
        if not include_metadata:
            return self._compact_node_payload(node)
        return {
            "id": node.id,
            "type": node.type,
            "title": node.properties.get("title") or node.label,
            "content": node.properties.get("content") or node.text or node.label,
            "status": node.status,
            "created_at": node.created_at,
            "updated_at": node.updated_at,
            "metadata": dict(node.properties.get("metadata") or {}),
            "source": node.properties.get("source"),
            "standard_id": node.properties.get("standard_id"),
            "standard_type": node.properties.get("standard_type"),
            "session_id": node.properties.get("session_id"),
            "session_title": node.properties.get("session_title"),
        }

    def _edge_payload(self, edge: MemoryEdge, *, include_metadata: bool = True) -> dict[str, Any]:
        if not include_metadata:
            return {
                "id": edge.id,
                "from_id": edge.from_id,
                "to_id": edge.to_id,
                "relation": edge.type,
            }
        return {
            "id": edge.id,
            "from_id": edge.from_id,
            "to_id": edge.to_id,
            "relation": edge.type,
            "created_at": edge.created_at,
            "updated_at": edge.updated_at,
            "metadata": {key: value for key, value in edge.properties.items() if key not in {"source"}},
            "source": edge.properties.get("source"),
        }

    def _compact_node_payload(self, node: MemoryNode) -> dict[str, Any]:
        title = str(node.properties.get("title") or node.label or node.id)
        content = str(node.properties.get("content") or node.text or title)
        payload: dict[str, Any] = {
            "id": node.id,
            "type": node.type,
            "status": node.status,
            "title": title,
        }
        kind = self._map_node_kind(node)
        if kind == "file":
            path = self._node_file_path(node)
            if path:
                payload["path"] = path
            return payload
        if kind == "symbol":
            metadata = node.properties.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            qualified_name = metadata.get("qualified_name") or node.properties.get("qualified_name")
            if qualified_name:
                payload["qualified_name"] = str(qualified_name)
            path = self._node_file_path(node)
            if path:
                payload["path"] = path
            line_start = metadata.get("line_start") or node.properties.get("line_start")
            line_end = metadata.get("line_end") or node.properties.get("line_end")
            if line_start is not None:
                payload["line_start"] = line_start
            if line_end is not None:
                payload["line_end"] = line_end
            return payload
        if content and content != title:
            payload["content"] = content
        return payload

    def _bus_node_payload(self, node: MemoryNode, *, include_payload: bool = True) -> dict[str, Any]:
        metadata = {
            key: value
            for key, value in node.properties.items()
            if key
            not in {
                "source",
                "content",
                "title",
                "agent_id",
                "target_agent_id",
                "agent_storage",
                "standard_storage",
                "payload",
            }
        }
        payload: dict[str, Any] = {
            "id": node.id,
            "type": node.type,
            "title": node.properties.get("title") or node.label,
            "content": node.properties.get("content") or node.text or node.label,
            "status": node.status,
            "created_at": node.created_at,
            "updated_at": node.updated_at,
            "agent_id": node.properties.get("agent_id"),
            "target_agent_id": node.properties.get("target_agent_id"),
            "agent_storage": node.properties.get("agent_storage"),
            "standard_storage": node.properties.get("standard_storage"),
            "source": node.properties.get("source"),
            "metadata": metadata,
        }
        if "payload" in node.properties and include_payload:
            payload["payload"] = node.properties["payload"]
        return payload

    def _node_is_since(self, node: MemoryNode, since_dt: Any) -> bool:
        value = parse_dt(node.updated_at) or parse_dt(node.created_at)
        return value is not None and value >= since_dt

    def _edge_is_since(self, edge: MemoryEdge, since_dt: Any) -> bool:
        value = parse_dt(edge.updated_at) or parse_dt(edge.created_at)
        return value is not None and value >= since_dt

    def _current_session_properties(self, graph: MemoryGraph) -> dict[str, Any]:
        workspace = graph.get_node(WORKSPACE_NODE_ID)
        if workspace is None:
            return {}
        session_id = str(workspace.properties.get("current_session_id") or "").strip()
        if not session_id:
            return {}
        session = graph.get_node(session_id)
        if session is None or session.type != "session" or session.status != "active":
            return {}
        return {
            "session_id": session.id,
            "session_title": session.properties.get("title") or session.label,
        }

    def _resolve_session_selector(self, graph: MemoryGraph, selector: str) -> str:
        value = selector.strip()
        if not value:
            raise ValueError("Agent session selector must not be empty")
        if value.casefold() == "current":
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            session_id = str(workspace.properties.get("current_session_id") or "").strip() if workspace else ""
            if not session_id:
                raise ValueError("No current agent session. Run `reql agent session start \"...\"` first.")
            return session_id
        session = graph.get_node(value)
        if session is None or session.type != "session":
            raise ValueError(f"Agent session not found: {selector}")
        return session.id

    def _node_file_path(self, node: MemoryNode) -> str | None:
        metadata = node.properties.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        value = (
            metadata.get("relative_path")
            or metadata.get("path")
            or node.properties.get("relative_path")
            or node.properties.get("path")
        )
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _node_path_lookup_keys(self, node: MemoryNode) -> set[str]:
        metadata = node.properties.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        values = [
            metadata.get("relative_path"),
            metadata.get("path"),
            node.properties.get("relative_path"),
            node.properties.get("path"),
        ]
        keys: set[str] = set()
        for value in values:
            if value is not None:
                keys.update(self._path_lookup_keys(str(value)))
        return keys

    def _path_lookup_keys(self, value: str) -> set[str]:
        raw = value.strip()
        if not raw:
            return set()
        slash_path = raw.replace("\\", "/")
        normalized = posixpath.normpath(slash_path)
        keys = {normalized}
        if normalized.startswith("./"):
            keys.add(normalized[2:])
        elif not normalized.startswith("../"):
            keys.add(f"./{normalized}")
        try:
            resolved = Path(raw).expanduser().resolve(strict=False)
        except OSError:
            resolved = None
        if resolved is not None:
            keys.add(str(resolved).replace("\\", "/"))
        return {key for key in keys if key and key != "."}

    def _map_node_kind(self, node: MemoryNode) -> str:
        standard_type = str(node.properties.get("standard_type") or node.type)
        if node.properties.get("source") != "standard":
            return "agent"
        if standard_type in FILE_TYPES:
            return "file"
        if standard_type in SYMBOL_TYPES:
            return "symbol"
        if standard_type in FRAGMENT_TYPES:
            return "fragment"
        if standard_type in STATIC_FINDING_TYPES:
            return "static_finding"
        return "other"

    def _related_file_payload(self, path: str, node: MemoryNode, *, include_metadata: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": f"file:{path}",
            "type": "file",
            "status": node.status,
            "title": path,
            "path": path,
        }
        if not include_metadata:
            return payload
        payload.update({
            "content": path,
            "created_at": node.created_at,
            "updated_at": node.updated_at,
            "metadata": {"relative_path": path, "related_node_id": node.id, "related_node_type": node.type},
            "source": "standard",
            "standard_id": node.properties.get("standard_id") or node.id,
            "standard_type": node.properties.get("standard_type") or node.type,
        })
        return payload

    def _title_from_content(self, content: str) -> str:
        compact = " ".join(content.split())
        return compact[:80] if len(compact) > 80 else compact


def _normalized_derived_node_payload(node: MemoryNode) -> dict[str, Any]:
    payload = node.to_dict()
    payload.pop("updated_at", None)
    payload["properties"] = _normalized_derived_properties(node.properties)
    return payload


def _normalized_derived_edge_payload(edge: MemoryEdge) -> dict[str, Any]:
    payload = edge.to_dict()
    payload.pop("updated_at", None)
    payload["properties"] = _normalized_derived_properties(edge.properties)
    return payload


def _normalized_derived_properties(properties: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in properties.items() if key not in {"created_at", "updated_at"}}
