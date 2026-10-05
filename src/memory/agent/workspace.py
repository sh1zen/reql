"""Project-local working memory graph for coding agents."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from api.memory_graph import MemoryGraph
from memory.config import REQLConfig, default_config
from memory.services.coordination import CoordinationStore, KINDS, TERMINAL
from memory.domain.exceptions import StorageError
from memory.domain.ids import stable_id
from memory.domain.models import MemoryNode
from memory.domain.timeutils import parse_dt, utcnow_iso
from memory.storage import BlockGraphStore, StoreLease, exclusive_store_lock

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
AGENT_NODE_TYPES = {"private_note", "external_note", "session"}


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
        if len({self.paths.standard_storage, self.paths.agent_storage, self.paths.dashboard_storage}) != 3:
            raise ValueError("Project, private agent, and public dashboard stores must use distinct paths")
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
        with self._lifecycle_lease(), StoreLease(init_lease, timeout_seconds=AGENT_READ_LOCK_TIMEOUT_SECONDS):
            already_initialized = self.exists()
            result = self._initialized_workspace() if already_initialized else self._recreate(remove_existing=False)
            result["already_initialized"] = already_initialized
            self._migrate_work()
            self._register_agent(status="active")
            if not self._active_session_id():
                result["session"] = self.start_session(session_name or "Agent session")["session"]
            result["retention"] = self._prune_completed_agents()
        return result

    def reset(self) -> dict[str, Any]:
        with self._lifecycle_lease():
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
        shared = self.coordination.records()
        graph = self._open_agent(read_only=True)
        try:
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            nodes = graph.store.all_nodes()
            agent_nodes = [node for node in nodes if node.id != WORKSPACE_NODE_ID]
            session_status = self._current_session_status_payload(nodes, workspace, shared)
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

    def _current_session_status_payload(self, nodes: list[MemoryNode], workspace: MemoryNode | None, work: list[dict[str, Any]]) -> dict[str, Any]:
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
        open_tasks = [item for item in work if item["kind"] == "task"
                      and item["status"] not in TERMINAL and item["session_id"] == session_id]
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

    def reject_approach(self, approach: str, reason: str) -> dict[str, Any]:
        """Retain an attempted approach and why it was rejected across sessions."""
        reason = reason.strip()
        if not reason:
            raise ValueError("Rejected approach reason must not be empty")
        if not approach.strip():
            raise ValueError("Agent node content must not be empty")
        result = self.record("failure", approach, rationale=reason)
        result["node"] = self._work_payload(result["node"])
        return result

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

    @property
    def coordination(self) -> CoordinationStore:
        """Access the authoritative project work store without opening it."""
        return CoordinationStore(self.paths.dashboard_storage)

    def record(self, kind: str, content: str, **fields: Any) -> dict[str, Any]:
        """Persist engineering state with the current agent/session provenance."""
        session_id = self._active_session_id()
        if not session_id:
            raise ValueError("No active session. Run `reql agent init` first.")
        graph = self._require_agent(read_only=True)
        try:
            session = graph.get_node(session_id)
            title = str(session.label or "") if session else ""
        finally:
            graph.close()
        result = self.coordination.put(kind, content, agent_id=self.agent_id,
                                       session_id=session_id, session_title=title, **fields)
        if result["changed"]:
            self._touch_agent()
        return result

    def _migrate_work(self) -> None:
        """Move old tasks/decisions/failures out of private scratch on resume."""
        source = self._require_agent()
        try:
            target = self._ensure_public_dashboard()
            try:
                moved = CoordinationStore.migrate(target.store, source.store.all_nodes(), self.agent_id)
            finally:
                target.close()
            with source.store.transaction():
                for item_id in moved:
                    source.store.remove_node(item_id)
        finally:
            source.close()

    def add_task(self, description: str, **fields: Any) -> dict[str, Any]:
        """Create a durable outcome; identical requests reuse the same record."""
        return self.record("task", description, **fields)

    def complete_task(self, node_id: str, message: str) -> dict[str, Any]:
        """Complete shared work with evidence, preserving its dependencies."""
        if not message.strip():
            raise ValueError("Task completion message must not be empty")
        task = self.coordination.show(node_id)
        if task["kind"] != "task":
            raise ValueError(f"Agent node is not a task: {node_id}")
        result = self.record("task", task["content"], record_id=node_id,
                             expected_revision=task["revision"], status="done", rationale=message)
        context = self._publish(message, kind="task_completion", target="public", task_id=node_id)
        return {"task": self._work_payload(result["node"]), "context": context["context"]}

    def list_tasks(self, *, include_all: bool = False) -> dict[str, Any]:
        """Return owned work, including unfinished outcomes from previous sessions."""
        return {"tasks": [self._work_payload(item) for item in self.coordination.records()
                          if item["kind"] == "task" and item["agent_id"] == self.agent_id
                          and (include_all or item["status"] not in TERMINAL)]}

    def add_decision(self, text: str, *, rationale: str, **fields: Any) -> dict[str, Any]:
        """Record a shared decision and why it holds."""
        return self.record("decision", text, rationale=rationale, **fields)

    def add_finding(self, text: str, **fields: Any) -> dict[str, Any]:
        """Record a scoped discovery for later work."""
        return self.record("observation", text, **fields)

    @staticmethod
    def _work_payload(item: dict[str, Any]) -> dict[str, Any]:
        """Adapt durable records to supported dashboard/task output fields."""
        payload = dict(item)
        if item["kind"] == "failure":
            payload.update(type="rejected_approach", reason=item["rationale"])
        if item["kind"] == "task" and item["status"] == "done":
            payload.update(completion_message=item["rationale"], completed_at=item["updated_at"])
        return payload

    def add_node(
        self,
        node_type: str,
        content: str,
        *,
        title: str | None = None,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if node_type in KINDS or node_type in {"finding", "rejected_approach"}:
            kind = {"finding": "observation", "rejected_approach": "failure"}.get(node_type, node_type)
            return self.record(kind, content, status=status, rationale=(metadata or {}).get("reason"))
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
        shared_matches = self.coordination.context(query, limit=min(40, max(1, limit)), include_history=True)["records"]
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
            for item in shared_matches:
                work = self._work_payload(item)
                if item["agent_id"] == self.agent_id and (not node_type or work["type"] == node_type) and (not status or item["status"] == status):
                    items.append({"score": item["score"], "node": work})
            items.sort(key=lambda item: item["score"], reverse=True)
            return {"query": query, "results": items[:limit]}
        finally:
            graph.close()

    def show(self, item_id: str) -> dict[str, Any]:
        for item in self.coordination.records():
            if item["id"] == item_id:
                return {"kind": "node", "node": self._work_payload(item)}
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
        shared = [self._work_payload(item) for item in self.coordination.records() if item["agent_id"] == self.agent_id]
        graph = self._require_agent(read_only=True)
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
            nodes.extend(item for item in shared if (not node_type or item["type"] == node_type)
                         and (not status or item["status"] == status)
                         and (not since_dt or parse_dt(item["updated_at"]) >= since_dt))
            nodes.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")), reverse=True)
            return {"nodes": nodes[:limit]}
        finally:
            graph.close()

    def export(self, *, include_metadata: bool = False) -> dict[str, Any]:
        if not include_metadata:
            return self.operational_overview()
        shared = [self._work_payload(item) for item in self.coordination.records() if item["agent_id"] == self.agent_id]
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
                "nodes": [self._node_payload(MemoryNode.from_dict(item), include_metadata=True) for item in payload["nodes"]] + shared,
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
            active_agents = {item["agent_id"] for item in agents if item["status"] == "active"}
            active_sessions = {session_id for node in nodes if node.type == "agent" and node.status == "active"
                               for session_id in node.properties.get("active_session_ids", [])}
            tasks = [self._work_payload(CoordinationStore._payload(node)) for node in nodes if node.type == "work_record"
                     and (item := node.properties)["kind"] == "task" and item["status"] not in TERMINAL
                     and item["agent_id"] in active_agents and item["session_id"] in active_sessions]
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
        """Terminate a stale agent and release its private operational store."""
        workspace = cls(standard_storage, agent_id=agent_id, config=config)
        with workspace._lifecycle_lease():
            dashboard = workspace.public_dashboard(limit=10_000)
            if not any(item["agent_id"] == workspace.agent_id for item in dashboard["agents"]):
                raise ValueError(f"Agent is not registered: {workspace.agent_id}")
            closed_sessions: list[str] = []
            if workspace.exists():
                graph = workspace._require_agent()
                try:
                    with graph.store.transaction():
                        root = graph.get_node(WORKSPACE_NODE_ID)
                        now = utcnow_iso()
                        for session in graph.store.all_nodes():
                            if session.type == "session" and session.status == "active":
                                properties = dict(session.properties)
                                properties.update({"ended_at": now, "is_current": False, "termination": "forced"})
                                graph.store.update_node_fields(session.id, status="terminated", properties=properties, updated_at=now)
                                closed_sessions.append(session.id)
                        if root is not None:
                            properties = dict(root.properties)
                            properties["current_session_ids"] = {}
                            graph.store.update_node_fields(root.id, properties=properties)
                finally:
                    graph.close()
            closed_session = closed_sessions[-1] if closed_sessions else None
            workspace._publish(f"Agent terminated: {workspace.agent_id}", kind="termination", target="public", session_id=closed_session)
            workspace._register_agent(status="terminated", session_id=closed_session)
            retention = workspace._prune_completed_agents()
        return {"agent_id": workspace.agent_id, "status": "terminated", "closed_session_id": closed_session, "retention": retention}

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
                    if node.type == "rejected_approach":
                        reason = str((node.properties.get("metadata") or {}).get("reason") or "")
                        content = f"{content} — {reason}"
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
        for item in reader.coordination.context(needle, limit=min(40, max(1, limit)), include_history=True)["records"]:
            results.append({"timestamp": item["updated_at"], "agent_id": item["agent_id"],
                            "context": item["content"] + " — " + item["rationale"], "scope": "work", "id": item["id"]})
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
            "format": "reql-agent-dashboard-v3",
            "public": self.public_dashboard(limit=limit),
            "private": self.private_dashboard(limit=limit),
            "overview_command": "reql project overview",
        }

    def private_dashboard(self, *, limit: int = 50) -> dict[str, Any]:
        """Return bounded orientation: what failed, what finished, and what remains."""
        overview = self.operational_overview()
        return {
            "format": "reql-agent-private-dashboard-v3",
            "agent": overview["agent"],
            "open": overview["open_tasks"][:limit],
            "done": overview["done_tasks"][:limit],
            "rejected": overview["rejected_approaches"][:limit],
            "private_notes": overview["private_notes"][:limit],
            "external_notes": overview["external_notes"][:limit],
        }

    def operational_overview(self) -> dict[str, Any]:
        """Return complete private task and rejection history for project overview."""
        shared = [self._work_payload(item) for item in self.coordination.records() if item["agent_id"] == self.agent_id]
        agent_status = self._agent_status()
        graph = self._require_agent(read_only=True)
        try:
            workspace = graph.get_node(WORKSPACE_NODE_ID)
            nodes = [node for node in graph.store.all_nodes() if node.id != WORKSPACE_NODE_ID]
            tasks = [item for item in shared if item["kind"] == "task"]
            private_notes = [self._node_payload(node) for node in nodes if node.type in {"private_note", "note"}]
            external_notes = [self._node_payload(node) for node in nodes if node.type == "external_note"]
            rejected_approaches = [item for item in shared if item["kind"] == "failure"]
            for section in (tasks, private_notes, external_notes, rejected_approaches):
                section.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            status = self._current_session_status_payload(nodes, workspace, shared)
            return {
                "agent": {"agent_id": self.agent_id, "status": agent_status, **status},
                "open_tasks": [task for task in tasks if task["status"] not in TERMINAL],
                "done_tasks": [task for task in tasks if task["status"] == "done"],
                "private_notes": private_notes,
                "external_notes": external_notes,
                "rejected_approaches": rejected_approaches,
            }
        finally:
            graph.close()

    @classmethod
    def project_operational_overview(
        cls, standard_storage: str | Path, *, config: REQLConfig | None = None, limit: int = 5, include_private: bool = True
    ) -> dict[str, Any]:
        """Collect complete operational history from every registered agent."""
        dashboard_storage = cls.default_dashboard_storage(standard_storage)
        agents = []
        for agent_id in cls._registered_agent_ids(dashboard_storage) if include_private else []:
            workspace = cls(standard_storage, agent_id=agent_id, config=config)
            if workspace.exists():
                agents.append(workspace.operational_overview())
        dashboard = cls.read_public_dashboard(standard_storage, limit=10_000, config=config)
        work = CoordinationStore(dashboard_storage).overview(limit=limit)
        return {"agents": agents, "context": dashboard["context"], "work": work}

    def finish(self, summary: str | None = None) -> dict[str, Any]:
        """Close the current session and publish its final message as shared context."""

        if not self.exists():
            raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
        summary_text = (summary or "").strip()
        if not summary_text:
            raise ValueError("Finish message must not be empty")
        with self._lifecycle_lease():
            session_id = self._active_session_id()
            work = [item for item in self.coordination.records() if item["session_id"] == session_id]
            work.sort(key=lambda item: item["updated_at"], reverse=True)
            work.sort(key=lambda item: item["status"] in TERMINAL)
            existing_checkpoint = next((item for item in work if item["id"] == stable_id("work", "checkpoint", "", session_id or "")), None)
            checkpoint_fields = {"record_id": existing_checkpoint["id"], "expected_revision": existing_checkpoint["revision"]} if existing_checkpoint else {}
            self.record("checkpoint", summary_text, key=session_id, **checkpoint_fields,
                        status="done", files=sorted({path for item in work for path in item["files"]})[:64],
                        summarizes=[item["id"] for item in work if item["kind"] != "checkpoint"][:32],
                        next_action="; ".join(item["next_action"] or item["content"] for item in work if item["kind"] == "task" and item["status"] not in TERMINAL)[:4000])
            graph = self._require_agent()
            closed_session = None
            remaining_sessions: dict[str, str] = {}
            try:
                with graph.store.transaction():
                    workspace = graph.get_node(WORKSPACE_NODE_ID)
                    if workspace is None:
                        raise ValueError("Agent workspace is not initialized. Run `reql agent init` first.")
                    session_id = self._current_session_id(workspace)
                    session = graph.get_node(session_id) if session_id else None
                    if session is None or session.type != "session" or session.status != "active":
                        raise ValueError("No current agent session. Run `reql agent init --name \"...\"` first.")
                    now = utcnow_iso()
                    properties = dict(session.properties)
                    properties.update({"ended_at": now, "is_current": False})
                    graph.store.update_node_fields(session.id, status="completed", updated_at=now, properties=properties)
                    closed_session = session.id
                    workspace_properties = dict(workspace.properties)
                    remaining_sessions = dict(workspace_properties.get("current_session_ids") or {})
                    remaining_sessions.pop(self._session_scope_key(), None)
                    workspace_properties["current_session_ids"] = remaining_sessions
                    graph.store.update_node_fields(workspace.id, properties=workspace_properties)
            finally:
                graph.close()
            context = self._publish(summary_text, kind="finish", target="public", session_id=closed_session)
            status = "active" if remaining_sessions else "finished"
            self._register_agent(status=status, session_id=closed_session)
            retention = self._prune_completed_agents()
            retention["work_records_removed"] = self.coordination.prune()
        return {
            "agent_id": self.agent_id,
            "status": status,
            "closed_session_id": closed_session,
            "context": context["context"],
            "retention": retention,
        }

    def _publish(
        self, text: str, *, kind: str, target: str, task_id: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """Append one durable entry to the public dashboard Context section."""

        content = text.strip()
        if not content:
            raise ValueError("Dashboard context message must not be empty")
        kind = kind.strip().casefold() or "public_note"
        target = target.strip() or "public"
        session_id = session_id or self._active_session_id()
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

    def _agent_status(self) -> str:
        """Read roster status directly without projecting work or scratch."""
        if not self.paths.dashboard_storage.exists():
            return "unknown"
        graph = self._open_public_dashboard(read_only=True)
        try:
            node = graph.get_node(stable_id("agent-identity", self.agent_id))
            return str(node.status) if node else "unknown"
        finally:
            graph.close()

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
        """Remove private data under its writer lock, preserving live readers."""
        if base.resolve() in {self.paths.standard_storage, self.paths.dashboard_storage}:
            raise ValueError(f"Cannot remove a project or public dashboard store as agent memory: {base}")
        files_removed = 0
        bytes_reclaimed = 0
        with exclusive_store_lock(base, timeout_seconds=0.0):
            for path in self._agent_store_files(base):
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    continue
                path.unlink()
                files_removed += 1
                bytes_reclaimed += size
        return {"files_removed": files_removed, "bytes_reclaimed": bytes_reclaimed}

    @contextmanager
    def _lifecycle_lease(self) -> Iterator[None]:
        """Serialize initialization, completion, and retention across agents."""
        path = self.paths.dashboard_storage.with_name(f"{self.paths.dashboard_storage.name}.lifecycle")
        try:
            with StoreLease(path, timeout_seconds=AGENT_READ_LOCK_TIMEOUT_SECONDS):
                yield
        except StorageError as exc:
            raise ValueError(f"Cannot update agent lifecycle: {exc}") from exc

    def _prune_completed_agents(self) -> dict[str, Any]:
        """Release completed private stores and bound public completion history."""
        result: dict[str, Any] = {
            "files_removed": 0,
            "records_removed": 0,
            "bytes_reclaimed": 0,
            "busy_agents": [],
            "skipped_agents": [],
        }
        graph = self._ensure_public_dashboard()
        try:
            nodes = graph.store.all_nodes()
            identities = {str(node.properties.get("agent_id") or node.label): node for node in nodes if node.type == "agent"}
            protected = {agent_id for agent_id, node in identities.items() if node.status == "active"}
            active_paths = {
                Path(str(identities[agent_id].properties.get("agent_storage") or self.agent_storage_for(self.paths.standard_storage, agent_id))).resolve()
                for agent_id in protected
            }
            deferred: set[str] = set()
            for agent_id, identity in identities.items():
                if identity.status not in {"finished", "terminated"}:
                    continue
                base = self.agent_storage_for(self.paths.standard_storage, agent_id)
                registered_path = Path(str(identity.properties.get("agent_storage") or base)).resolve()
                if agent_id == self.agent_id:
                    base = self.paths.agent_storage
                if (
                    registered_path != base
                    or base.resolve() != base
                    or base in active_paths
                    or (agent_id != self.agent_id and not base.is_relative_to(self.paths.standard_storage.parent))
                ):
                    result["skipped_agents"].append(agent_id)
                    deferred.add(agent_id)
                    protected.add(agent_id)
                    continue
                if not any(path.exists() for path in self._agent_store_files(base)):
                    continue
                try:
                    legacy = MemoryGraph.open(base, read_only=True)
                    try:
                        CoordinationStore.migrate(graph.store, legacy.store.all_nodes(), agent_id)
                    finally:
                        legacy.close()
                    removed = self._remove_agent_store_path(base)
                except StorageError as exc:
                    if "locked" not in str(exc).casefold():
                        raise
                    result["busy_agents"].append(agent_id)
                    deferred.add(agent_id)
                    protected.add(agent_id)
                    continue
                except OSError as exc:
                    raise ValueError(f"Cannot remove completed agent store {base}: {exc}") from exc
                for key, value in removed.items():
                    result[key] += value

            completions = [node for node in nodes if node.type == "context" and node.properties.get("message_type") in {"finish", "termination"}]
            owners_with_messages = {str(node.properties.get("agent_id") or "") for node in completions}
            completions.extend(node for agent_id, node in identities.items() if node.status in {"finished", "terminated"} and agent_id not in owners_with_messages)
            completions.sort(key=lambda node: (node.updated_at, node.id), reverse=True)

            def completion_key(node: MemoryNode) -> tuple[str, str]:
                """Group a completed session's public records by owner and session."""
                return (str(node.properties.get("agent_id") or ""), str(node.properties.get("session_id") or node.id))

            ordered_keys = list(dict.fromkeys(completion_key(node) for node in completions))
            retained = set(ordered_keys[:self.config.retention.agent_sessions])
            completed = set(ordered_keys)
            expired = completed - retained
            retained_owners = {agent_id for agent_id, _ in retained}
            removed_ids = set()
            for node in nodes:
                owner = str(node.properties.get("agent_id") or "")
                identity = identities.get(owner)
                inactive = identity is not None and identity.status in {"finished", "terminated"}
                if node.type == "active_task" and (inactive or node.status != "active" or completion_key(node) in completed):
                    removed_ids.add(node.id)
                elif node.type in {"agent", "context"}:
                    if owner in deferred:
                        continue
                    if node.type == "agent" and owner in protected:
                        continue
                    if completion_key(node) in expired or (inactive and owner not in retained_owners and owner not in protected):
                        removed_ids.add(node.id)
            if removed_ids:
                with graph.store.transaction():
                    for node_id in sorted(removed_ids):
                        result["records_removed"] += int(graph.store.remove_node(node_id))
                compact = getattr(graph.store, "compact_storage", None)
                if compact is not None:
                    result["bytes_reclaimed"] += int(compact().get("bytes_reclaimed", 0))
        finally:
            graph.close()
        return result

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
        active_session_ids = []
        if self.exists():
            private = self._require_agent(read_only=True)
            try:
                root = private.get_node(WORKSPACE_NODE_ID)
                if root:
                    active_session_ids = list((root.properties.get("current_session_ids") or {}).values())
            finally:
                private.close()
        graph = self._ensure_public_dashboard()
        try:
            with graph.store.transaction():
                self._register_agent_in_graph(graph, status=status, session_id=session_id, active_session_ids=active_session_ids)
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
        active_session_ids: list[str],
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
                "active_session_ids": active_session_ids,
                "activity_id": self.activity_id,
                "selection_source": self.selection_source,
            }
        )
        if existing is None or (status == "active" and existing.status != "active"):
            props["session_started_at"] = now
            props.pop("completed_at", None)
            props.pop("terminated_at", None)
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
        """Enumerate data sidecars; the owning lease releases its own lock."""
        yield base
        yield base.with_name(f"{base.name}.wal")
        yield base.with_name(f"{base.name}.usage.jsonl")

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
        payload = {
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
        if node.type == "rejected_approach":
            payload["reason"] = payload["metadata"].get("reason")
        if node.type == "task" and node.status == "done":
            payload["completion_message"] = node.properties.get("completion_message")
            payload["completed_at"] = node.properties.get("completed_at")
        return payload

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

    def _title_from_content(self, content: str) -> str:
        return " ".join(content.split())[:80]
