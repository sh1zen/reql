"""Project-local working memory graph for coding agents."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from api.memory_graph import MemoryGraph
from memory.config import REQLConfig, default_config
from memory.domain.exceptions import StorageError
from memory.domain.ids import stable_id
from memory.domain.models import MemoryNode
from memory.domain.timeutils import parse_dt, utcnow_iso
from memory.storage import BlockGraphStore, StoreLease, inspect_store_locks

AGENT_STORAGE_FILE = "agent.reql"
AGENT_DASHBOARD_STORAGE_FILE = "agent-dashboard.reql"
AGENT_SCOPE_DIR = "agents"
DEFAULT_AGENT_ID = "master"
DEFAULT_ACTIVITY_SCOPE = "__default__"
PUBLIC_DASHBOARD_NODE_ID = "agent:public-dashboard"
WORKSPACE_NODE_ID = "agent:workspace"
AGENT_LOCK_TIMEOUT_SECONDS = 2.0
AGENT_READ_LOCK_TIMEOUT_SECONDS = 10.0
AGENT_LOCK_RETRY_ATTEMPTS = 3
AGENT_LOCK_RETRY_DELAY_SECONDS = 0.25
DASHBOARD_DEFAULT_LIMIT = 5
DASHBOARD_MAX_LIMIT = 20
DASHBOARD_POST_MAX_CHARS = 240
AGENT_NODE_TYPES = {"private_note", "external_note", "task", "session"}
LEARNED_NODE_TYPES = ("private_note", "external_note")


class AgentIdentitySelectionError(ValueError):
    """Raised when a private workspace cannot be selected safely."""

    def __init__(self, agent_ids: Iterable[str]) -> None:
        self.agent_ids = tuple(sorted({str(agent_id).strip() for agent_id in agent_ids if str(agent_id).strip()}))
        super().__init__(
            "Multiple REQL agents are registered for this project, so implicit selection is ambiguous. "
            "Provide --activity/REQL_AGENT_ACTIVITY_ID or --agent/REQL_AGENT_ID."
        )


def _agent_lock_wait_budget(*, read_only: bool) -> float:
    lock_timeout = AGENT_READ_LOCK_TIMEOUT_SECONDS if read_only else AGENT_LOCK_TIMEOUT_SECONDS
    retry_delays = max(0, AGENT_LOCK_RETRY_ATTEMPTS - 1) * AGENT_LOCK_RETRY_DELAY_SECONDS
    return max(0.0, AGENT_LOCK_RETRY_ATTEMPTS * lock_timeout + retry_delays)


@dataclass(frozen=True, slots=True)
class AgentWorkspacePaths:
    standard_storage: Path
    agent_storage: Path
    dashboard_storage: Path


class AgentWorkspace:
    """Own one private dashboard and coordinate through the public dashboard."""

    def __init__(
        self,
        standard_storage: str | Path,
        *,
        agent_id: str | None = None,
        agent_storage: str | Path | None = None,
        dashboard_storage: str | Path | None = None,
        activity_id: str | None = None,
        config: REQLConfig | None = None,
    ) -> None:
        standard_path = Path(standard_storage).expanduser().resolve(strict=False)
        resolved_dashboard_storage = (
            Path(dashboard_storage).expanduser().resolve(strict=False)
            if dashboard_storage is not None
            else self.default_dashboard_storage(standard_path)
        )
        self.activity_id = self._normalize_activity_id(
            activity_id or os.environ.get("REQL_AGENT_ACTIVITY_ID") or os.environ.get("CODEX_THREAD_ID")
        )
        self.agent_id, self.selection_source, self.concurrency_safe = self._resolve_agent_identity(
            standard_path,
            resolved_dashboard_storage,
            agent_id,
            self.activity_id,
        )
        self.paths = AgentWorkspacePaths(
            standard_storage=standard_path,
            agent_storage=Path(agent_storage).expanduser().resolve(strict=False)
            if agent_storage is not None
            else self.agent_storage_for(standard_path, self.agent_id),
            dashboard_storage=resolved_dashboard_storage,
        )
        self.config = config or default_config()

    @staticmethod
    def default_agent_storage(standard_storage: str | Path) -> Path:
        path = Path(standard_storage).expanduser().resolve(strict=False)
        return path.with_name(AGENT_STORAGE_FILE)

    @staticmethod
    def default_dashboard_storage(standard_storage: str | Path) -> Path:
        path = Path(standard_storage).expanduser().resolve(strict=False)
        return path.with_name(AGENT_DASHBOARD_STORAGE_FILE)

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

    @classmethod
    def read_public_dashboard(
        cls,
        standard_storage: str | Path,
        *,
        dashboard_storage: str | Path | None = None,
        limit: int = 50,
        config: REQLConfig | None = None,
    ) -> dict[str, Any]:
        """Read the public dashboard without selecting a private agent."""

        reader = cls(
            standard_storage,
            agent_id=DEFAULT_AGENT_ID,
            dashboard_storage=dashboard_storage,
            config=config,
        )
        return reader.public_dashboard(limit=limit)

    def init(self, name: str | None = None) -> dict[str, Any]:
        """Initialize this workspace and optionally begin a named current session."""

        session_name = name.strip() if name is not None else None
        if session_name == "":
            raise ValueError("Agent session name must not be empty")
        init_lease = self.paths.agent_storage.with_name(f"{self.paths.agent_storage.name}.init")
        with StoreLease(init_lease, timeout_seconds=AGENT_READ_LOCK_TIMEOUT_SECONDS):
            already_initialized = self.exists()
            result = self._initialized_workspace() if already_initialized else self._recreate(remove_existing=False)
        result["already_initialized"] = already_initialized
        self._register_agent(status="active")
        if not self._active_session_id():
            result["session"] = self.start_session(session_name or "Agent session")["session"]
        return result

    def reset(self) -> dict[str, Any]:
        result = self._recreate()
        self._register_agent(status="active")
        return result

    def _initialized_workspace(self) -> dict[str, Any]:
        """Describe an existing operational store without rewriting it."""

        agent = self._open_agent(read_only=True)
        try:
            workspace = agent.get_node(WORKSPACE_NODE_ID)
            if workspace is None or workspace.properties.get("format") != "reql-agent-memory-v1":
                raise ValueError("Agent workspace format is unsupported. Run `reql agent reset` to recreate it.")
            return {
                "initialized": True,
                "agent_id": self.agent_id,
                "activity_id": self.activity_id,
                "selection_source": self.selection_source,
                "concurrency_safe": self.concurrency_safe,
                "agent_storage": str(self.paths.agent_storage),
                "dashboard_storage": str(self.paths.dashboard_storage),
                "initialized_at": workspace.properties.get("initialized_at"),
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
                "dashboard_storage": str(self.paths.dashboard_storage),
                "initialized_at": None,
                "nodes": 0,
                "agent_nodes": 0,
            }
        graph = self._open_agent(read_only=True)
        try:
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            nodes = graph.store.all_nodes()
            agent_nodes = [node for node in nodes if node.id != WORKSPACE_NODE_ID]
            session_status = self._current_session_status_payload(nodes, workspace)
            return {
                "exists": True,
                "agent_id": self.agent_id,
                "activity_id": self.activity_id,
                "selection_source": self.selection_source,
                "concurrency_safe": self.concurrency_safe,
                "agent_storage": str(self.paths.agent_storage),
                "dashboard_storage": str(self.paths.dashboard_storage),
                "initialized_at": workspace.properties.get("initialized_at") if workspace else None,
                **session_status,
                "nodes": len(nodes),
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
        ]
        return {
            "current_session_id": session_id,
            "current_session_title": session_title,
            "current_session_started_at": started_at,
            "current_session_open_tasks": len(open_tasks),
            "current_session_is_idle": not open_tasks,
        }

    def add_note(self, text: str) -> dict[str, Any]:
        """Add private working memory to this agent's dashboard."""
        return self.add_node("private_note", text)

    def add_external_note(self, text: str, *, sender_agent_id: str) -> dict[str, Any]:
        """Store a note sent by another agent."""
        return self.add_node("external_note", text, metadata={"sender_agent_id": sender_agent_id})

    def send_note(self, target_agent_id: str, text: str) -> dict[str, Any]:
        target = self._normalize_agent_id(target_agent_id)
        recipient = AgentWorkspace(
            self.paths.standard_storage,
            agent_id=target,
            dashboard_storage=self.paths.dashboard_storage,
            config=self.config,
        )
        if not recipient.exists():
            raise ValueError(f"Target agent is not initialized: {target}")
        result = recipient.add_external_note(text, sender_agent_id=self.agent_id)
        self._touch_agent()
        return {"target_agent_id": target, **result}

    def publish_note(self, text: str) -> dict[str, Any]:
        """Add a public note to the shared dashboard Context section."""
        return self._publish(text, kind="public_note", target="public")

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
        result = self.add_node("task", description, status="open")
        self._sync_active_task(result["node"])
        return result

    def complete_task(self, node_id: str, message: str) -> dict[str, Any]:
        completion_message = message.strip()
        if not completion_message:
            raise ValueError("Task completion message must not be empty")
        graph = self._require_agent()
        try:
            result = self._complete_task_in_graph(graph, node_id, completion_message)
        finally:
            graph.close()
        self._sync_active_task(result["task"])
        context = self._publish(completion_message, kind="task_completion", target="public", task_id=node_id)
        return {**result, "context": context["context"]}

    def list_tasks(self, *, include_all: bool = False) -> dict[str, Any]:
        """Return this agent's tasks, defaulting to unfinished work."""
        graph = self._require_agent(read_only=True)
        try:
            tasks = [
                self._node_payload(node)
                for node in graph.store.all_nodes()
                if node.type == "task" and (include_all or node.status == "open")
            ]
            tasks.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")), reverse=True)
            return {"tasks": tasks}
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
                return {"kind": "node", "node": self._node_payload(node)}
            raise ValueError(f"Agent item not found: {item_id}")
        finally:
            graph.close()

    def list_items(
        self,
        *,
        node_type: str | None = None,
        status: str | None = None,
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
            return {"nodes": nodes[:limit]}
        finally:
            graph.close()

    def export(self, *, include_metadata: bool = False) -> dict[str, Any]:
        if not include_metadata:
            return self.private_dashboard()
        graph = self._require_agent(read_only=True)
        try:
            payload = graph.export_json()
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            return {
                "format": "reql-agent-memory-v1",
                "agent_id": self.agent_id,
                "agent_storage": str(self.paths.agent_storage),
                "dashboard_storage": str(self.paths.dashboard_storage),
                "initialized_at": workspace.properties.get("initialized_at") if workspace else None,
                "nodes": [self._node_payload(MemoryNode.from_dict(item), include_metadata=True) for item in payload["nodes"]],
            }
        finally:
            graph.close()

    def public_dashboard(self, *, limit: int = 50) -> dict[str, Any]:
        """Return the shared dashboard: agents, active task summaries, and context."""
        if not self.paths.dashboard_storage.exists() or self.paths.dashboard_storage.stat().st_size == 0:
            return {
                "format": "reql-agent-public-dashboard-v2",
                "dashboard_storage": str(self.paths.dashboard_storage),
                "agents": [],
                "active_tasks": [],
                "context": [],
                "drill": self._drill_section(),
            }
        graph = self._open_public_dashboard(read_only=True)
        try:
            nodes = [node for node in graph.store.all_nodes() if node.id != PUBLIC_DASHBOARD_NODE_ID]
            agents = [self._dashboard_agent_payload(node) for node in nodes if node.type == "agent"]
            tasks = [self._dashboard_context_payload(node) for node in nodes if node.type == "active_task" and node.status == "active"]
            context = [self._dashboard_context_payload(node) for node in nodes if node.type == "context"]
            agents.sort(key=lambda item: (str(item.get("last_activity_at") or ""), str(item["agent_id"])), reverse=True)
            tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            context.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
            return {
                "format": "reql-agent-public-dashboard-v2",
                "dashboard_storage": str(self.paths.dashboard_storage),
                "agents": agents[:limit],
                "active_tasks": tasks[:limit],
                "context": context[:limit],
                "drill": self._drill_section(),
            }
        finally:
            graph.close()

    @classmethod
    def list_registered_agents(
        cls, standard_storage: str | Path, *, include_all: bool = False, config: REQLConfig | None = None
    ) -> list[dict[str, Any]]:
        dashboard = cls.read_public_dashboard(standard_storage, limit=500, config=config)
        agents = dashboard["agents"]
        return agents if include_all else [agent for agent in agents if agent["status"] == "active"]

    @classmethod
    def terminate_agent(
        cls, standard_storage: str | Path, agent_id: str, *, config: REQLConfig | None = None
    ) -> dict[str, Any]:
        """Terminate a stale agent while retaining all private dashboard history."""
        workspace = cls(standard_storage, agent_id=agent_id, config=config)
        dashboard = workspace.public_dashboard(limit=500)
        if not any(item["agent_id"] == workspace.agent_id for item in dashboard["agents"]):
            raise ValueError(f"Agent is not registered: {workspace.agent_id}")
        closed_session = None
        if workspace.exists():
            graph = workspace._require_agent()
            try:
                with graph.store.transaction():
                    root = graph.get_node(WORKSPACE_NODE_ID)
                    session_id = workspace._current_session_id(root)
                    if session_id:
                        session = graph.get_node(session_id)
                        if session is not None and session.status == "active":
                            now = utcnow_iso()
                            properties = dict(session.properties)
                            properties.update({"ended_at": now, "is_current": False, "termination": "forced"})
                            graph.store.update_node_fields(session.id, status="terminated", properties=properties, updated_at=now)
                            closed_session = session.id
                        root_props = dict(root.properties) if root is not None else {}
                        current = dict(root_props.get("current_session_ids") or {})
                        current.pop(workspace._session_scope_key(), None)
                        if root is not None:
                            root_props["current_session_ids"] = current
                            graph.store.update_node_fields(root.id, properties=root_props)
            finally:
                graph.close()
        workspace._register_agent(status="terminated", session_id=closed_session)
        return {"agent_id": workspace.agent_id, "status": "terminated", "closed_session_id": closed_session}

    @classmethod
    def search_dashboards(
        cls, standard_storage: str | Path, query: str, *, limit: int = 20, config: REQLConfig | None = None
    ) -> dict[str, Any]:
        """Search public history and every registered private dashboard."""
        needle = query.strip()
        if not needle:
            raise ValueError("Dashboard search query must not be empty")
        reader = cls(standard_storage, agent_id=DEFAULT_AGENT_ID, config=config)
        dashboard = reader.public_dashboard(limit=10_000)
        results: list[dict[str, Any]] = []
        for entry in [*dashboard["context"], *dashboard["active_tasks"]]:
            context = str(entry.get("content") or "")
            if cls._dashboard_search_matches(context, needle):
                results.append({"timestamp": entry.get("timestamp") or entry.get("updated_at"), "agent_id": entry.get("agent_id"), "context": context, "scope": "public"})
        for identity in dashboard["agents"]:
            agent_id = str(identity["agent_id"])
            workspace = cls(standard_storage, agent_id=agent_id, config=config)
            if not workspace.exists():
                continue
            graph = workspace._require_agent(read_only=True)
            try:
                for node in graph.store.all_nodes():
                    if node.id == WORKSPACE_NODE_ID:
                        continue
                    content = str(node.properties.get("content") or node.text or "")
                    if cls._dashboard_search_matches(content, needle):
                        results.append({
                            "timestamp": node.updated_at or node.created_at,
                            "agent_id": agent_id,
                            "context": content,
                            "scope": f"private:{node.type}",
                            "id": node.id,
                        })
            finally:
                graph.close()
        results.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        return {"query": query, "results": results[:limit]}

    @staticmethod
    def _dashboard_search_matches(content: str, query: str) -> bool:
        """Match a literal phrase or all normalized query terms in dashboard text."""

        normalized_content = content.casefold()
        normalized_query = query.casefold()
        if normalized_query in normalized_content:
            return True
        query_terms = re.findall(r"\w+", normalized_query)
        if not query_terms:
            return False
        content_terms = set(re.findall(r"\w+", normalized_content))
        return all(term in content_terms for term in query_terms)

    def dashboard(
        self,
        *,
        limit: int = DASHBOARD_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Read the public dashboard together with this agent's private dashboard."""

        if limit < 1 or limit > DASHBOARD_MAX_LIMIT:
            raise ValueError(f"Agent dashboard limit must be between 1 and {DASHBOARD_MAX_LIMIT}")
        return {
            "format": "reql-agent-dashboard-v2",
            "public": self.public_dashboard(limit=limit),
            "private": self.private_dashboard(limit=limit),
        }

    def private_dashboard(self, *, limit: int = 50) -> dict[str, Any]:
        """Return the selected agent's complete private planning view."""
        graph = self._require_agent(read_only=True)
        try:
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            nodes = [node for node in graph.store.all_nodes() if node.id != WORKSPACE_NODE_ID]
            tasks = [self._node_payload(node) for node in nodes if node.type == "task"]
            private_notes = [self._node_payload(node) for node in nodes if node.type in {"private_note", "note"}]
            external_notes = [self._node_payload(node) for node in nodes if node.type == "external_note"]
            for section in (tasks, private_notes, external_notes):
                section.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            status = self._current_session_status_payload(nodes, workspace)
            return {
                "format": "reql-agent-private-dashboard-v2",
                "agent": {"agent_id": self.agent_id, "status": self._agent_status(), **status},
                "tasks": tasks[:limit],
                "private_notes": private_notes[:limit],
                "external_notes": external_notes[:limit],
            }
        finally:
            graph.close()

    def finish(self, summary: str | None = None) -> dict[str, Any]:
        """Close the current session and publish its final message as shared context."""

        if not self.exists():
            raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
        summary_text = (summary or "").strip()
        if not summary_text:
            raise ValueError("Finish message must not be empty")
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
        context = self._publish(summary_text, kind="finish", target="public")
        self._register_agent(status="finished", session_id=closed_session)
        return {
            "agent_id": self.agent_id,
            "status": "finished",
            "closed_session_id": closed_session,
            "context": context["context"],
        }

    def _publish(self, text: str, *, kind: str, target: str, task_id: str | None = None) -> dict[str, Any]:
        """Append one durable entry to the public dashboard Context section."""

        content = text.strip()
        if not content:
            raise ValueError("Dashboard context message must not be empty")
        kind = kind.strip().casefold() or "public_note"
        target = target.strip() or "public"
        session_id = self._active_session_id()
        graph = self._ensure_public_dashboard()
        try:
            with graph.store.transaction():
                now = utcnow_iso()
                dashboard_node = self._public_dashboard_node(graph, now)
                message = MemoryNode(
                    id=stable_id("agent-dashboard-context", now, self.agent_id, kind, content),
                    type="context",
                    label=self._title_from_content(content),
                    text=content,
                    canonical_key=stable_id("agent-dashboard-context-key", now, self.agent_id, kind, content),
                    properties={
                        "source": "dashboard",
                        "message_type": kind,
                        "agent_id": self.agent_id,
                        "target_agent_id": target,
                        "content": content,
                        "title": self._title_from_content(content),
                        "session_id": session_id,
                        "task_id": task_id,
                    },
                    status="active",
                    created_at=now,
                    updated_at=now,
                    salience=0.6,
                    confidence=1.0,
                )
                stored, created = graph.add_node(message)
                graph.store.update_node_fields(dashboard_node.id, updated_at=now, properties=dashboard_node.properties)
        finally:
            graph.close()
        self._touch_agent()
        return {"created": created, "context": self._dashboard_context_payload(stored)}

    def _complete_task_in_graph(self, graph: MemoryGraph, node_id: str, message: str) -> dict[str, Any]:
        node = graph.get_node(node_id)
        if node is None or node.id == WORKSPACE_NODE_ID:
            raise ValueError(f"Agent node not found: {node_id}")
        if node.type != "task":
            raise ValueError(f"Agent node is not a task: {node_id}")
        props = dict(node.properties)
        props["completed_at"] = utcnow_iso()
        props["completion_message"] = message
        updated = graph.store.update_node_fields(node.id, status="done", properties=props)
        if updated is None:
            raise ValueError(f"Agent node not found: {node_id}")
        return {"task": self._node_payload(updated)}

    def _sync_active_task(self, task: dict[str, Any]) -> None:
        """Mirror coordination-safe task status on the public dashboard."""
        graph = self._ensure_public_dashboard()
        try:
            with graph.store.transaction():
                now = utcnow_iso()
                self._public_dashboard_node(graph, now)
                task_id = str(task["id"])
                node_id = stable_id("agent-dashboard-task", self.agent_id, task_id)
                active = task.get("status") != "done"
                node = MemoryNode(
                    id=node_id,
                    type="active_task",
                    label=str(task.get("title") or task.get("content") or task_id),
                    text=str(task.get("content") or task.get("title") or task_id),
                    canonical_key=node_id,
                    properties={
                        "source": "dashboard",
                        "agent_id": self.agent_id,
                        "task_id": task_id,
                        "content": str(task.get("content") or task.get("title") or task_id),
                        "title": str(task.get("title") or task.get("content") or task_id),
                    },
                    status="active" if active else "completed",
                    created_at=str(task.get("created_at") or now),
                    updated_at=now,
                    salience=0.6,
                    confidence=1.0,
                )
                graph.add_node(node)
        finally:
            graph.close()
        self._touch_agent()

    def _agent_status(self) -> str:
        dashboard = self.public_dashboard(limit=500)
        for identity in dashboard["agents"]:
            if identity["agent_id"] == self.agent_id:
                return str(identity["status"])
        return "unknown"

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
                    "dashboard_storage": str(self.paths.dashboard_storage),
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
                "dashboard_storage": str(self.paths.dashboard_storage),
                "initialized_at": initialized_at,
            }
        finally:
            agent.close()

    def _remove_agent_store(self) -> dict[str, int]:
        return self._remove_agent_store_path(self.paths.agent_storage)

    def _remove_agent_store_path(self, base: Path) -> dict[str, int]:
        files_removed = 0
        bytes_reclaimed = 0
        for path in self._agent_store_files(base):
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            path.unlink()
            files_removed += 1
            bytes_reclaimed += size
        readers = base.with_name(f"{base.name}.readers")
        try:
            readers.rmdir()
        except (FileNotFoundError, OSError):
            pass
        return {"files_removed": files_removed, "bytes_reclaimed": bytes_reclaimed}

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
        dashboard_storage: Path,
        explicit_agent_id: str | None,
        activity_id: str | None,
    ) -> tuple[str, str, bool]:
        if explicit_agent_id:
            return cls._normalize_agent_id(explicit_agent_id), "explicit", True
        if activity_id:
            derived = stable_id("agent-activity", str(standard_storage).casefold(), activity_id)
            return f"agent:{derived.split(':')[-1][:16]}", "activity", True
        registered = cls._registered_agent_ids(dashboard_storage)
        if len(registered) > 1:
            raise AgentIdentitySelectionError(registered)
        if len(registered) == 1:
            return registered[0], "single-agent", True
        return DEFAULT_AGENT_ID, "default", False

    @classmethod
    def _registered_agent_ids(cls, dashboard_storage: Path) -> list[str]:
        if not dashboard_storage.exists() or dashboard_storage.stat().st_size == 0:
            return []
        graph = MemoryGraph.open(dashboard_storage, read_only=True)
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

    def _register_agent(self, *, status: str, session_id: str | None = None) -> None:
        graph = self._ensure_public_dashboard()
        try:
            with graph.store.transaction():
                self._register_agent_in_graph(graph, status=status, session_id=session_id)
        finally:
            graph.close()

    def _touch_agent(self) -> None:
        """Refresh an active agent's last-activity timestamp without changing state."""
        if self._agent_status() != "active":
            return
        self._register_agent(status="active", session_id=self._active_session_id())

    def _register_agent_in_graph(
        self,
        graph: MemoryGraph,
        *,
        status: str,
        session_id: str | None = None,
        updated_at: str | None = None,
    ) -> MemoryNode:
        now = updated_at or utcnow_iso()
        dashboard_node = self._public_dashboard_node(graph, now)
        dashboard_props = dict(dashboard_node.properties)
        dashboard_props.update({"updated_at": now})
        dashboard_props.pop("standard_storage", None)
        dashboard_props.pop("current_agent_id", None)
        graph.store.update_node_fields(dashboard_node.id, updated_at=now, properties=dashboard_props)
        node_id = stable_id("agent-identity", self.agent_id)
        existing = graph.get_node(node_id)
        props = dict(existing.properties) if existing is not None else {}
        props.pop("standard_storage", None)
        props.pop("session_id", None)
        props.update(
            {
                "source": "dashboard",
                "agent_id": self.agent_id,
                "agent_storage": str(self.paths.agent_storage),
                "role": DEFAULT_AGENT_ID if self.agent_id == DEFAULT_AGENT_ID else "worker",
                "title": self.agent_id,
                "content": self.agent_id,
                "last_activity_at": now,
                "activity_id": self.activity_id,
                "selection_source": self.selection_source,
            }
        )
        if existing is None:
            props["session_started_at"] = now
        if status == "finished":
            props["completed_at"] = now
        elif status == "terminated":
            props["terminated_at"] = now
        if session_id:
            props["session_id"] = session_id
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

    def _active_session_id(self) -> str | None:
        """Read the current private session id for public context ownership."""

        if not self.exists():
            return None
        graph = self._require_agent(read_only=True)
        try:
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            return self._current_session_id(workspace) if workspace is not None else None
        finally:
            graph.close()

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
            f"The lock wait budget of {_agent_lock_wait_budget(read_only=read_only):.2f}s was exhausted. "
            "Retry the command, or avoid running multiple `reql agent` commands in parallel."
        ) from last_error

    def _ensure_public_dashboard(self) -> MemoryGraph:
        graph = self._open_public_dashboard()
        try:
            if graph.get_node(PUBLIC_DASHBOARD_NODE_ID) is None:
                with graph.store.transaction():
                    self._public_dashboard_node(graph, utcnow_iso())
            return graph
        except Exception:
            graph.close()
            raise

    def _open_public_dashboard(self, *, read_only: bool = False) -> MemoryGraph:
        self.paths.dashboard_storage.parent.mkdir(parents=True, exist_ok=True)
        last_error: StorageError | None = None
        for attempt in range(AGENT_LOCK_RETRY_ATTEMPTS):
            try:
                lock_timeout = AGENT_READ_LOCK_TIMEOUT_SECONDS if read_only else AGENT_LOCK_TIMEOUT_SECONDS
                store = BlockGraphStore(
                    self.paths.dashboard_storage,
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
            f"Public dashboard is busy; could not acquire {mode} access to {self.paths.dashboard_storage}. "
            f"The lock wait budget of {_agent_lock_wait_budget(read_only=read_only):.2f}s was exhausted. "
            "Retry the command, or avoid running multiple public-dashboard writes in parallel."
        ) from last_error

    def _public_dashboard_node(self, graph: MemoryGraph, now: str) -> MemoryNode:
        existing = graph.get_node(PUBLIC_DASHBOARD_NODE_ID)
        props = dict(existing.properties) if existing is not None else {}
        props.update(
            {
                "format": "reql-agent-public-dashboard-v2",
                "source": "system",
                "dashboard_storage": str(self.paths.dashboard_storage),
            }
        )
        props.pop("standard_storage", None)
        node = MemoryNode(
            id=PUBLIC_DASHBOARD_NODE_ID,
            type="PublicDashboard",
            label="Public Agent Dashboard",
            text="Shared agent state, active task summaries, and coordination context.",
            canonical_key=PUBLIC_DASHBOARD_NODE_ID,
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

    def _dashboard_agent_payload(self, node: MemoryNode) -> dict[str, Any]:
        properties = node.properties
        return {
            "agent_id": str(properties.get("agent_id") or node.label),
            "status": node.status,
            "session_started_at": properties.get("session_started_at"),
            "last_activity_at": properties.get("last_activity_at") or properties.get("last_seen_at") or node.updated_at,
            "completed_at": properties.get("completed_at"),
            "terminated_at": properties.get("terminated_at"),
        }

    def _dashboard_context_payload(self, node: MemoryNode) -> dict[str, Any]:
        properties = node.properties
        return {
            "id": node.id,
            "timestamp": node.updated_at or node.created_at,
            "updated_at": node.updated_at or node.created_at,
            "agent_id": properties.get("agent_id"),
            "message_type": properties.get("message_type") or node.type,
            "content": properties.get("content") or node.text,
            "task_id": properties.get("task_id"),
            "status": node.status,
        }

    def _drill_section(self) -> list[dict[str, str]]:
        """Keep actionable dashboard drill-downs separate from coordination data."""
        return [
            {"label": "open a private dashboard", "command": 'reql agent --agent "agent:AGENT_ID" dashboard'},
            {"label": "search dashboard history", "command": 'reql agent search "<terms>"'},
        ]

    def _public_dashboard_node_payload(self, node: MemoryNode, *, include_payload: bool = True) -> dict[str, Any]:
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
                raise ValueError("No current agent session. Run `reql agent init --name \"...\"` first.")
            return session_id
        session = graph.get_node(value)
        if session is None or session.type != "session":
            raise ValueError(f"Agent session not found: {selector}")
        return session.id

    def _title_from_content(self, content: str) -> str:
        return " ".join(content.split())[:80]
