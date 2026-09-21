"""Project-local working memory graph for coding agents."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import time
from typing import Any, Iterable
from uuid import uuid4

from api.memory_graph import MemoryGraph
from memory.config import REQLConfig
from memory.domain.exceptions import StorageError
from memory.domain.ids import stable_id
from memory.domain.models import MemoryEdge, MemoryNode
from memory.domain.timeutils import parse_dt, utcnow_iso
from memory.storage import BlockGraphStore, StoreLease


AGENT_STORAGE_FILE = "agent.reql"
AGENT_BUS_STORAGE_FILE = "agent-bus.reql"
AGENT_SCOPE_DIR = "agents"
DEFAULT_AGENT_ID = "master"
DEFAULT_ACTIVITY_SCOPE = "__default__"
BUS_NODE_ID = "agent:bus"
WORKSPACE_NODE_ID = "agent:workspace"
AGENT_LOCK_TIMEOUT_SECONDS = 2.0
AGENT_READ_LOCK_TIMEOUT_SECONDS = 10.0
AGENT_LOCK_RETRY_ATTEMPTS = 3
AGENT_LOCK_RETRY_DELAY_SECONDS = 0.25
DASHBOARD_DEFAULT_LIMIT = 5
DASHBOARD_MAX_LIMIT = 20
DASHBOARD_POST_MAX_CHARS = 240
AGENT_NODE_TYPES = {"note", "task", "decision", "finding", "risk", "plan", "session"}
LEARNED_NODE_TYPES = ("decision", "finding", "note", "risk", "plan")
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


def _agent_lock_wait_budget(*, read_only: bool) -> float:
    lock_timeout = AGENT_READ_LOCK_TIMEOUT_SECONDS if read_only else AGENT_LOCK_TIMEOUT_SECONDS
    retry_delays = max(0, AGENT_LOCK_RETRY_ATTEMPTS - 1) * AGENT_LOCK_RETRY_DELAY_SECONDS
    return max(0.0, AGENT_LOCK_RETRY_ATTEMPTS * lock_timeout + retry_delays)


@dataclass(frozen=True, slots=True)
class AgentWorkspacePaths:
    standard_storage: Path
    agent_storage: Path
    bus_storage: Path


class AgentWorkspace:
    """Service facade for isolated operational memory owned by one agent."""

    def __init__(
        self,
        standard_storage: str | Path,
        *,
        agent_id: str | None = None,
        agent_storage: str | Path | None = None,
        bus_storage: str | Path | None = None,
        activity_id: str | None = None,
        config: REQLConfig | None = None,
    ) -> None:
        standard_path = Path(standard_storage).expanduser().resolve(strict=False)
        resolved_bus_storage = (
            Path(bus_storage).expanduser().resolve(strict=False)
            if bus_storage is not None
            else self.default_bus_storage(standard_path)
        )
        self.activity_id = self._normalize_activity_id(
            activity_id or os.environ.get("REQL_AGENT_ACTIVITY_ID") or os.environ.get("CODEX_THREAD_ID")
        )
        self.agent_id, self.selection_source, self.concurrency_safe = self._resolve_agent_identity(
            standard_path,
            resolved_bus_storage,
            agent_id,
            self.activity_id,
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
        init_lease = self.paths.agent_storage.with_name(f"{self.paths.agent_storage.name}.init")
        with StoreLease(init_lease, timeout_seconds=AGENT_READ_LOCK_TIMEOUT_SECONDS):
            already_initialized = self.exists()
            result = self._migrate_operational_store() if already_initialized else self._recreate(remove_existing=False)
        result["already_initialized"] = already_initialized
        self._register_agent(status="active")
        return result

    def reset(self) -> dict[str, Any]:
        result = self._recreate()
        self._register_agent(status="active")
        return result

    def _migrate_operational_store(self) -> dict[str, Any]:
        """Remove legacy canonical graph copies while preserving agent-owned memory."""

        if not self.exists():
            raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
        agent = self._open_agent()
        try:
            existing_nodes = agent.store.all_nodes()
            existing_edges = agent.store.all_edges()
            canonical_ids = {
                node.id for node in existing_nodes if node.properties.get("source") == "standard"
            }
            removed_edge_ids = {
                edge.id
                for edge in existing_edges
                if edge.properties.get("source") == "standard"
                or edge.from_id in canonical_ids
                or edge.to_id in canonical_ids
            }
            workspace = agent.get_node(WORKSPACE_NODE_ID)
            if (
                workspace is not None
                and workspace.properties.get("format") == "reql-agent-memory-v1"
                and not canonical_ids
                and not removed_edge_ids
            ):
                return {
                    "initialized": True,
                    "agent_id": self.agent_id,
                    "activity_id": self.activity_id,
                    "selection_source": self.selection_source,
                    "concurrency_safe": self.concurrency_safe,
                    "agent_storage": str(self.paths.agent_storage),
                    "bus_storage": str(self.paths.bus_storage),
                    "initialized_at": workspace.properties.get("initialized_at"),
                    "removed_canonical_items": 0,
                    "removed_canonical_relationships": 0,
                    "preserved_agent_nodes": sum(
                        1 for node in existing_nodes if node.id != WORKSPACE_NODE_ID
                    ),
                    "preserved_agent_relations": len(existing_edges),
                }
            migrated_at = utcnow_iso()
            initialized_at = workspace.properties.get("initialized_at") if workspace is not None else migrated_at
            workspace_props = dict(workspace.properties) if workspace is not None else {}
            for key in (
                "standard_storage",
                "synced_at",
                "derived_node_count",
                "derived_relation_count",
            ):
                workspace_props.pop(key, None)
            workspace_props.setdefault("current_session_ids", {})
            workspace_props.update(
                {
                    "format": "reql-agent-memory-v1",
                    "source": "system",
                    "agent_id": self.agent_id,
                    "agent_storage": str(self.paths.agent_storage),
                    "bus_storage": str(self.paths.bus_storage),
                    "initialized_at": initialized_at,
                    "migrated_at": migrated_at,
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
                created_at=workspace.created_at if workspace is not None else migrated_at,
                updated_at=migrated_at,
            )
            preserved_agent_nodes = sum(
                1
                for node in existing_nodes
                if node.id != WORKSPACE_NODE_ID and node.properties.get("source") != "standard"
            )
            preserved_agent_edges = sum(
                1
                for edge in existing_edges
                if edge.properties.get("source") != "standard" and edge.id not in removed_edge_ids
            )

            with agent.store.transaction():
                for edge_id in sorted(removed_edge_ids):
                    agent.store.remove_edge(edge_id)
                for node_id in sorted(canonical_ids):
                    agent.store.remove_node(node_id)
                agent.store.batch_upsert_nodes([workspace_node])
            return {
                "initialized": True,
                "agent_id": self.agent_id,
                "activity_id": self.activity_id,
                "selection_source": self.selection_source,
                "concurrency_safe": self.concurrency_safe,
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": initialized_at,
                "removed_canonical_items": len(canonical_ids),
                "removed_canonical_relationships": len(removed_edge_ids),
                "preserved_agent_nodes": preserved_agent_nodes,
                "preserved_agent_relations": preserved_agent_edges,
            }
        finally:
            agent.close()

    def status(self) -> dict[str, Any]:
        if not self.exists():
            return {
                "exists": False,
                "agent_id": self.agent_id,
                "activity_id": self.activity_id,
                "selection_source": self.selection_source,
                "concurrency_safe": self.concurrency_safe,
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": None,
                "nodes": 0,
                "relations": 0,
                "agent_nodes": 0,
            }
        self._migrate_operational_store()
        graph = self._open_agent(read_only=True)
        try:
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            nodes = graph.store.all_nodes()
            edges = graph.store.all_edges()
            agent_nodes = [
                node
                for node in nodes
                if node.id != WORKSPACE_NODE_ID and node.properties.get("source") != "standard"
            ]
            session_status = self._current_session_status_payload(nodes, workspace)
            return {
                "exists": True,
                "agent_id": self.agent_id,
                "activity_id": self.activity_id,
                "selection_source": self.selection_source,
                "concurrency_safe": self.concurrency_safe,
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": workspace.properties.get("initialized_at") if workspace else None,
                **session_status,
                "nodes": len(nodes),
                "relations": len(edges),
                "agent_nodes": len(agent_nodes),
                "metadata": dict(workspace.properties) if workspace else {},
            }
        finally:
            graph.close()

    def _current_session_status_payload(self, nodes: list[MemoryNode], workspace: MemoryNode | None) -> dict[str, Any]:
        session_id = self._current_session_id(workspace) if workspace is not None else ""
        if not session_id:
            return {
                "current_session_id": None,
                "current_session_title": None,
                "current_session_started_at": None,
                "current_session_open_tasks": 0,
                "current_session_is_idle": True,
            }
        session = next((node for node in nodes if node.id == session_id and node.type == "session"), None)
        session_title = session.properties.get("title") if session is not None else None
        started_at = session.properties.get("started_at") if session is not None else None
        open_tasks = [
            node
            for node in nodes
            if node.type == "task"
            and node.status != "done"
            and node.properties.get("session_id") == session_id
            and node.properties.get("source") != "standard"
        ]
        return {
            "current_session_id": session_id,
            "current_session_title": session_title,
            "current_session_started_at": started_at,
            "current_session_open_tasks": len(open_tasks),
            "current_session_is_idle": not open_tasks,
        }

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
                previous_id = self._current_session_id(workspace)
                if previous_id:
                    previous = graph.get_node(previous_id)
                    if previous is not None and previous.type == "session" and previous.status == "active":
                        previous_props = dict(previous.properties)
                        previous_props["ended_at"] = now
                        previous_props["is_current"] = False
                        graph.store.update_node_fields(previous.id, status="closed", properties=previous_props)
                session_id = stable_id("agent:session", self.activity_id or "", now, title)
                session = MemoryNode(
                    id=session_id,
                    type="session",
                    label=title,
                    text=title,
                    canonical_key=stable_id("agent-session-key", self.activity_id or "", now, title),
                    properties={
                        "content": title,
                        "title": title,
                        "metadata": {},
                        "source": "agent",
                        "session_id": session_id,
                        "session_title": title,
                        "activity_id": self.activity_id,
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
                current_by_activity = dict(workspace_props.get("current_session_ids") or {})
                current_by_activity[self._session_scope_key()] = stored.id
                workspace_props["current_session_ids"] = current_by_activity
                graph.store.update_node_fields(workspace.id, properties=workspace_props)
            result = {"created": created, "session": self._node_payload(stored)}
        finally:
            graph.close()
        self._register_agent(status="active")
        return result

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
            if left is None or left.properties.get("source") != "agent":
                raise ValueError(f"Link source not found in agent memory: {from_id}")
            targets_by_id = {node.id: node for node in graph.store.get_nodes(target_ids)}
            for to_id in target_ids:
                target = targets_by_id.get(to_id)
                if target is None or target.properties.get("source") != "agent":
                    raise ValueError(f"Link target not found in agent memory: {to_id}")
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
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            agent_nodes = [
                node
                for node in graph.store.all_nodes()
                if node.id != WORKSPACE_NODE_ID and node.properties.get("source") != "standard"
            ]
            agent_edges = [
                edge
                for edge in graph.store.all_edges()
                if edge.properties.get("source") == "agent" and edge.type in AGENT_RELATIONS
            ]
            filters: dict[str, Any] = {}
            if session:
                session_id = self._resolve_session_selector(graph, session)
                filters["session"] = session_id
                by_id = {node.id: node for node in agent_nodes}
                agent_edges = [edge for edge in agent_edges if edge.properties.get("session_id") == session_id]
                session_agent_ids = {
                    endpoint_id
                    for edge in agent_edges
                    for endpoint_id in (edge.from_id, edge.to_id)
                    if endpoint_id in by_id
                }
                agent_nodes = [
                    node
                    for node in agent_nodes
                    if node.id == session_id or node.properties.get("session_id") == session_id or node.id in session_agent_ids
                ]
            if task_id:
                task = graph.get_node(task_id)
                if task is None or task.id == WORKSPACE_NODE_ID or task.properties.get("source") != "agent":
                    raise ValueError(f"Agent task not found: {task_id}")
                if task.type != "task":
                    raise ValueError(f"Agent node is not a task: {task_id}")
                filters["task"] = task_id
                by_id = {node.id: node for node in agent_nodes}
                focus_ids = {task_id}
                changed = True
                while changed:
                    changed = False
                    for edge in agent_edges:
                        if edge.from_id not in focus_ids and edge.to_id not in focus_ids:
                            continue
                        for endpoint_id in (edge.from_id, edge.to_id):
                            endpoint = by_id.get(endpoint_id)
                            if endpoint is None:
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
                relevant_edges = agent_edges
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
            visible_node_ids = {node.id for node in tasks}
            if include_completed:
                filters["completed"] = True
                visible_node_ids.update(node.id for node in completed_tasks)
            visible_node_ids.update(node.id for node in decisions)
            visible_node_ids.update(
                node.id for node in agent_nodes if node.type in LEARNED_NODE_TYPES or node.type == "session"
            )
            essential_edges = [
                edge
                for edge in relevant_edges
                if edge.from_id in visible_node_ids and edge.to_id in visible_node_ids
            ]
            payload = {
                "open_tasks": [self._node_payload(node, include_metadata=include_metadata) for node in sorted(tasks, key=lambda item: item.updated_at, reverse=True)[:20]],
                "decisions": [self._node_payload(node, include_metadata=include_metadata) for node in sorted(decisions, key=lambda item: item.updated_at, reverse=True)[:20]],
                "relations": [self._edge_payload(edge, include_metadata=include_metadata) for edge in sorted(essential_edges, key=lambda item: item.updated_at, reverse=True)[:40]],
            }
            if include_completed:
                payload["completed_tasks"] = [
                    self._node_payload(node, include_metadata=include_metadata)
                    for node in sorted(completed_tasks, key=lambda item: item.updated_at, reverse=True)[:20]
                ]
            selected_session_id = str(filters.get("session") or "")
            payload["context_format"] = "reql-agent-context-v3"
            payload["context"] = {
                "learned": {
                    f"{node_type}s": [
                        self._node_payload(node, include_metadata=include_metadata)
                        for node in sorted(
                            (item for item in agent_nodes if item.type == node_type),
                            key=lambda item: item.updated_at,
                            reverse=True,
                        )[:10]
                    ]
                    for node_type in LEARNED_NODE_TYPES
                },
                "sessions": self._session_context_payload(
                    agent_nodes,
                    workspace,
                    include_metadata=include_metadata,
                    selected_session_id=selected_session_id or None,
                ),
            }
            if filters:
                payload["filters"] = filters
            return payload
        finally:
            graph.close()

    def export(self, *, include_metadata: bool = False) -> dict[str, Any]:
        if not include_metadata:
            return {"format": "reql-agent-memory-v1", **self.map(include_metadata=False)}
        graph = self._require_agent(read_only=True)
        try:
            payload = graph.export_json()
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            return {
                "format": "reql-agent-memory-v1",
                "agent_id": self.agent_id,
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

    def overview(self, *, limit: int = 50) -> dict[str, Any]:
        """Return compact, independently versioned views of registered agents."""

        bus = self.bus(limit=limit, include_payloads=False)
        agents: list[dict[str, Any]] = []
        for identity in bus.get("agents", []):
            agent_id = str(identity.get("agent_id") or identity.get("title") or "").strip()
            storage = identity.get("agent_storage")
            if not agent_id or not storage:
                continue
            candidate = AgentWorkspace(
                self.paths.standard_storage,
                agent_id=agent_id,
                agent_storage=str(storage),
                bus_storage=self.paths.bus_storage,
                config=self.config,
            )
            try:
                status = candidate.status()
                graph = candidate._open_agent(read_only=True)
                try:
                    nodes = graph.store.all_nodes()
                    agents.append(
                        {
                            "agent_id": agent_id,
                            "status": identity.get("status") or "active",
                            "current_session": {
                                "id": status.get("current_session_id"),
                                "title": status.get("current_session_title"),
                                "open_tasks": status.get("current_session_open_tasks", 0),
                            },
                            "open_tasks": [
                                candidate._compact_node_payload(node)
                                for node in sorted(nodes, key=lambda item: item.updated_at, reverse=True)
                                if node.type == "task" and node.status == "open"
                            ][:10],
                            "recent_decisions": [
                                candidate._compact_node_payload(node)
                                for node in sorted(nodes, key=lambda item: item.updated_at, reverse=True)
                                if node.type == "decision"
                            ][:5],
                        }
                    )
                finally:
                    graph.close()
            except (StorageError, ValueError, OSError) as exc:
                agents.append({"agent_id": agent_id, "status": "busy", "error": str(exc)})
        return {
            "format": "reql-agent-overview-v1",
            "observed_at": utcnow_iso(),
            "agents": agents,
        }

    def dashboard(
        self,
        *,
        post: str | None = None,
        kind: str = "status",
        target: str = "all",
        limit: int = DASHBOARD_DEFAULT_LIMIT,
        include_agent_details: bool = False,
    ) -> dict[str, Any]:
        """Read intra/inter-session coordination state and optionally post one update."""

        if limit < 1 or limit > DASHBOARD_MAX_LIMIT:
            raise ValueError(f"Agent dashboard limit must be between 1 and {DASHBOARD_MAX_LIMIT}")
        posted = None
        if post is not None:
            content = post.strip()
            if not content:
                raise ValueError("Agent dashboard post must not be empty")
            if len(content) > DASHBOARD_POST_MAX_CHARS:
                raise ValueError(
                    f"Agent dashboard posts are limited to {DASHBOARD_POST_MAX_CHARS} characters; "
                    "use `reql agent note add`, `decision add`, `finding add`, or `handoff` for durable detail"
                )
            posted = self.publish(content, kind=kind, target=target)["message"]

        private = self._dashboard_private_state(limit=limit)
        bus = self.bus(limit=limit * 4, include_payloads=False)
        relevant_targets = {"all", DEFAULT_AGENT_ID, self.agent_id}

        def relevant(item: dict[str, Any]) -> bool:
            return (
                str(item.get("target_agent_id") or "all") in relevant_targets
                or str(item.get("agent_id") or "") == self.agent_id
            )

        messages = [self._compact_bus_payload(item) for item in bus.get("messages", []) if relevant(item)][:limit]
        handoffs = [self._compact_bus_payload(item) for item in bus.get("handoffs", []) if relevant(item)][:limit]
        agents = [
            {
                "agent_id": item.get("agent_id"),
                "status": item.get("status") or "active",
                "activity_id": (item.get("metadata") or {}).get("activity_id"),
            }
            for item in bus.get("agents", [])[:limit]
        ]
        working_agents = [item for item in agents if item.get("status") == "active"]
        finished_agents = [item for item in agents if item.get("status") != "active"]
        is_active = any(item.get("agent_id") == self.agent_id for item in working_agents)
        payload: dict[str, Any] = {
            "format": "reql-agent-dashboard-v1",
            "agent_id": self.agent_id,
            "initialized": private["initialized"],
            "session": private["session"],
            "previous_session": private["previous_session"],
            "work": private["work"],
            "memory": private["memory"],
            "working_agents": working_agents,
            "finished_agents": finished_agents,
            "messages": messages,
            "handoffs": handoffs,
            "drilldowns": self._dashboard_drilldowns(private, handoffs, is_active=is_active),
        }
        if include_agent_details:
            payload["agent_details"] = self.overview(limit=limit)["agents"]
        if posted is not None:
            payload["posted"] = self._compact_bus_payload(posted)
        return payload

    def finish(self, summary: str | None = None) -> dict[str, Any]:
        """Publish final context, close the current session, and leave the active roster."""

        if not self.exists():
            raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
        handoff = self.handoff(summary or f"Finished work for {self.agent_id}", target="all")
        graph = self._require_agent()
        closed_session = None
        try:
            with graph.store.transaction():
                workspace = graph.get_node(WORKSPACE_NODE_ID)
                if workspace is None:
                    raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
                session_id = self._current_session_id(workspace)
                if session_id:
                    session = graph.get_node(session_id)
                    if session is not None and session.type == "session":
                        now = utcnow_iso()
                        properties = dict(session.properties)
                        properties.update({"ended_at": now, "is_current": False})
                        graph.store.update_node_fields(
                            session.id,
                            status="completed",
                            updated_at=now,
                            properties=properties,
                        )
                        closed_session = session.id
                    workspace_properties = dict(workspace.properties)
                    current_by_activity = dict(workspace_properties.get("current_session_ids") or {})
                    current_by_activity.pop(self._session_scope_key(), None)
                    workspace_properties["current_session_ids"] = current_by_activity
                    graph.store.update_node_fields(workspace.id, properties=workspace_properties)
        finally:
            graph.close()
        self._register_agent(status="completed")
        return {
            "agent_id": self.agent_id,
            "status": "completed",
            "closed_session_id": closed_session,
            "handoff": handoff["handoff"],
        }

    def _dashboard_private_state(self, *, limit: int) -> dict[str, Any]:
        """Build the bounded private-memory slice shown on the dashboard."""

        if not self.exists():
            return {
                "initialized": False,
                "session": None,
                "previous_session": None,
                "work": [],
                "memory": [],
            }
        graph = self._require_agent(read_only=True)
        try:
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            nodes = [
                node
                for node in graph.store.all_nodes()
                if node.id != WORKSPACE_NODE_ID and node.properties.get("source") == "agent"
            ]
            ordered = sorted(nodes, key=lambda item: item.updated_at, reverse=True)
            sessions = self._session_context_payload(
                nodes,
                workspace,
                include_metadata=False,
                selected_session_id=None,
            )
            return {
                "initialized": True,
                "session": sessions.get("current"),
                "previous_session": (sessions.get("previous") or [None])[0],
                "work": [
                    self._compact_node_payload(node)
                    for node in ordered
                    if node.type == "task" and node.status != "done"
                ][:limit],
                "memory": [
                    self._compact_node_payload(node)
                    for node in ordered
                    if node.type in {"decision", "finding", "risk", "plan"}
                ][:limit],
            }
        finally:
            graph.close()

    @staticmethod
    def _compact_bus_payload(item: dict[str, Any]) -> dict[str, Any]:
        """Remove timestamps and storage metadata from one dashboard bus signal."""

        return {
            "id": item.get("id"),
            "kind": (item.get("metadata") or {}).get("kind") or item.get("type"),
            "from": item.get("agent_id"),
            "to": item.get("target_agent_id"),
            "text": item.get("content") or item.get("title"),
        }

    def _dashboard_drilldowns(
        self,
        private: dict[str, Any],
        handoffs: list[dict[str, Any]],
        *,
        is_active: bool,
    ) -> list[dict[str, str]]:
        """Point an agent at deeper commands without expanding them eagerly."""

        if not private["initialized"]:
            return [{"reason": "initialize private memory", "command": "reql agent init"}]
        commands: list[dict[str, str]] = []
        work = private.get("work") or []
        if work:
            commands.append(
                {"reason": "inspect active task", "command": f"reql agent show {work[0]['id']} --json"}
            )
        if private.get("session") is not None:
            commands.append(
                {"reason": "recover current plan", "command": "reql agent map --session current --json"}
            )
        elif private.get("previous_session") is not None:
            commands.append(
                {"reason": "recover previous session", "command": "reql agent map --json"}
            )
        if handoffs:
            commands.append(
                {"reason": "open handoff payloads", "command": "reql agent bus --include-payloads --json"}
            )
        commands.append(
            {
                "reason": "query canonical code facts",
                "command": 'reql query_context --query "<task terms>" --code',
            }
        )
        if is_active:
            commands.append(
                {
                    "reason": "leave working roster when done",
                    "command": 'reql agent finish "<compact outcome>"',
                }
            )
        return commands[:5]

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

    def _link_in_graph(self, graph: MemoryGraph, from_id: str, to_id: str, relation: str) -> dict[str, Any]:
        relation = relation.strip().casefold()
        if relation not in AGENT_RELATIONS:
            raise ValueError(f"Unsupported agent relation: {relation}")
        left = graph.get_node(from_id)
        right = graph.get_node(to_id)
        if left is None or left.properties.get("source") != "agent":
            raise ValueError(f"Link source not found in agent memory: {from_id}")
        if right is None or right.properties.get("source") != "agent":
            raise ValueError(f"Link target not found in agent memory: {to_id}")
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
        if op in {"note.add", "note-add"}:
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
        stored, created = graph.add_node(node)
        return {"created": created, "node": self._node_payload(stored)}

    def _link_many_in_graph(self, graph: MemoryGraph, from_id: str, to_ids: list[str], relation: str) -> dict[str, Any]:
        relation = relation.strip().casefold()
        if relation not in AGENT_RELATIONS:
            raise ValueError(f"Unsupported agent relation: {relation}")
        target_ids = [str(to_id).strip() for to_id in to_ids if str(to_id).strip()]
        if not target_ids:
            raise ValueError("At least one link target is required")
        source = graph.get_node(from_id)
        if source is None or source.properties.get("source") != "agent":
            raise ValueError(f"Link source not found in agent memory: {from_id}")
        targets_by_id = {node.id: node for node in graph.store.get_nodes(target_ids)}
        for to_id in target_ids:
            target = targets_by_id.get(to_id)
            if target is None or target.properties.get("source") != "agent":
                raise ValueError(f"Link target not found in agent memory: {to_id}")
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

    def _recreate(self, *, remove_existing: bool = True) -> dict[str, Any]:
        if remove_existing:
            self._remove_agent_store()
        agent = self._open_agent()
        try:
            initialized_at = utcnow_iso()
            workspace = MemoryNode(
                id=WORKSPACE_NODE_ID,
                type="AgentWorkspace",
                label="Agent Workspace",
                text="Project-local working memory for coding agents.",
                canonical_key=WORKSPACE_NODE_ID,
                properties={
                    "format": "reql-agent-memory-v1",
                    "source": "system",
                    "agent_id": self.agent_id,
                    "agent_storage": str(self.paths.agent_storage),
                    "bus_storage": str(self.paths.bus_storage),
                    "initialized_at": initialized_at,
                    "current_session_ids": {},
                },
                status="active",
                created_at=initialized_at,
                updated_at=initialized_at,
            )
            with agent.store.transaction():
                agent.store.batch_upsert_nodes([workspace])
            return {
                "initialized": True,
                "agent_id": self.agent_id,
                "agent_storage": str(self.paths.agent_storage),
                "bus_storage": str(self.paths.bus_storage),
                "initialized_at": initialized_at,
            }
        finally:
            agent.close()

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

    @staticmethod
    def _normalize_activity_id(activity_id: str | None) -> str | None:
        value = str(activity_id or "").strip()
        return value[:200] or None

    @classmethod
    def _resolve_agent_identity(
        cls,
        standard_storage: Path,
        bus_storage: Path,
        explicit_agent_id: str | None,
        activity_id: str | None,
    ) -> tuple[str, str, bool]:
        if explicit_agent_id:
            return cls._normalize_agent_id(explicit_agent_id), "explicit", True
        if activity_id:
            derived = stable_id("agent-activity", str(standard_storage).casefold(), activity_id)
            return f"agent:{derived.split(':')[-1][:16]}", "activity", True
        registered = cls._registered_agent_ids(bus_storage)
        if len(registered) > 1:
            raise ValueError(
                "Multiple REQL agents are registered for this project, so implicit selection is ambiguous. "
                "Provide --activity/REQL_AGENT_ACTIVITY_ID or --agent/REQL_AGENT_ID."
            )
        if len(registered) == 1:
            return registered[0], "legacy-single-agent", True
        return DEFAULT_AGENT_ID, "legacy-default", False

    @classmethod
    def _registered_agent_ids(cls, bus_storage: Path) -> list[str]:
        if not bus_storage.exists() or bus_storage.stat().st_size == 0:
            return []
        graph = MemoryGraph.open(bus_storage, read_only=True, snapshot=True)
        try:
            return sorted(
                {
                    str(node.properties.get("agent_id") or node.label).strip()
                    for node in graph.store.all_nodes()
                    if node.type == "agent" and node.status == "active"
                    if str(node.properties.get("agent_id") or node.label).strip()
                }
            )
        except StorageError:
            return []
        finally:
            graph.close()

    @classmethod
    def _safe_agent_file_stem(cls, agent_id: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in agent_id)
        return safe.strip("._") or "agent"

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
        bus_props.update({"updated_at": now})
        bus_props.pop("standard_storage", None)
        bus_props.pop("current_agent_id", None)
        graph.store.update_node_fields(bus_node.id, updated_at=now, properties=bus_props)
        node_id = stable_id("agent-identity", self.agent_id)
        existing = graph.get_node(node_id)
        props = dict(existing.properties) if existing is not None else {}
        props.pop("standard_storage", None)
        props.update(
            {
                "source": "bus",
                "agent_id": self.agent_id,
                "agent_storage": str(self.paths.agent_storage),
                "role": DEFAULT_AGENT_ID if self.agent_id == DEFAULT_AGENT_ID else "worker",
                "title": self.agent_id,
                "content": self.agent_id,
                "last_seen_at": now,
                "activity_id": self.activity_id,
                "selection_source": self.selection_source,
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

    def _agent_store_files(self, base: Path) -> Iterable[Path]:
        yield base
        yield base.with_name(f"{base.name}.wal")
        yield base.with_name(f"{base.name}.lock")
        yield base.with_suffix(base.suffix + ".lock")

    def _require_agent(self, *, read_only: bool = False) -> MemoryGraph:
        if not self.exists():
            raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
        self._migrate_operational_store()
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
            f"The lock wait budget of {_agent_lock_wait_budget(read_only=read_only):.2f}s was exhausted. "
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
            f"The lock wait budget of {_agent_lock_wait_budget(read_only=read_only):.2f}s was exhausted. "
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
            }
        )
        props.pop("standard_storage", None)
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
        if content and content != title:
            payload["content"] = content
        if node.properties.get("session_id"):
            payload["session_id"] = node.properties["session_id"]
        if node.properties.get("session_title"):
            payload["session_title"] = node.properties["session_title"]
        return payload

    def _session_context_payload(
        self,
        agent_nodes: list[MemoryNode],
        workspace: MemoryNode | None,
        *,
        include_metadata: bool,
        selected_session_id: str | None,
    ) -> dict[str, Any]:
        sessions = [node for node in agent_nodes if node.type == "session"]
        if selected_session_id:
            sessions = [node for node in sessions if node.id == selected_session_id]
        current_session_id = self._current_session_id(workspace)

        def summary(session: MemoryNode) -> dict[str, Any]:
            items = [
                node
                for node in agent_nodes
                if node.id != session.id and node.properties.get("session_id") == session.id
            ]
            highlights = [
                node
                for node in items
                if node.type in {*LEARNED_NODE_TYPES, "task"}
                and (node.type != "task" or node.status == "done")
            ]
            payload: dict[str, Any] = {
                "id": session.id,
                "title": session.properties.get("title") or session.label,
                "status": session.status,
                "started_at": session.properties.get("started_at") or session.created_at,
                "ended_at": session.properties.get("ended_at"),
                "open_task_count": sum(1 for node in items if node.type == "task" and node.status != "done"),
                "completed_task_count": sum(1 for node in items if node.type == "task" and node.status == "done"),
                "highlights": [
                    self._node_payload(node, include_metadata=include_metadata)
                    for node in sorted(highlights, key=lambda item: item.updated_at, reverse=True)[:6]
                ],
            }
            return payload

        current = next((node for node in sessions if node.id == current_session_id), None)
        previous = [node for node in sessions if node.id != current_session_id]
        previous.sort(
            key=lambda item: str(item.properties.get("started_at") or item.created_at or ""),
            reverse=True,
        )
        return {
            "current": summary(current) if current is not None else None,
            "previous": [summary(node) for node in previous[:5]],
        }

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
        session_id = self._current_session_id(workspace)
        if not session_id:
            return {}
        session = graph.get_node(session_id)
        if session is None or session.type != "session" or session.status != "active":
            return {}
        return {
            "session_id": session.id,
            "session_title": session.properties.get("title") or session.label,
            **({"activity_id": self.activity_id} if self.activity_id else {}),
        }

    def _current_session_id(self, workspace: MemoryNode | None) -> str:
        if workspace is None:
            return ""
        current_by_activity = workspace.properties.get("current_session_ids")
        if not isinstance(current_by_activity, dict):
            return ""
        return str(current_by_activity.get(self._session_scope_key()) or "").strip()

    def _session_scope_key(self) -> str:
        return self.activity_id or DEFAULT_ACTIVITY_SCOPE

    def _resolve_session_selector(self, graph: MemoryGraph, selector: str) -> str:
        value = selector.strip()
        if not value:
            raise ValueError("Agent session selector must not be empty")
        if value.casefold() == "current":
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            session_id = self._current_session_id(workspace)
            if not session_id:
                raise ValueError("No current agent session. Run `reql agent session start \"...\"` first.")
            return session_id
        session = graph.get_node(value)
        if session is None or session.type != "session":
            raise ValueError(f"Agent session not found: {selector}")
        return session.id

    def _title_from_content(self, content: str) -> str:
        return " ".join(content.split())[:80]
