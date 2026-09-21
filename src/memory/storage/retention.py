"""Project-scoped retention for superseded graph and usage history."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import parse_dt, utcnow
from .graph_store import GraphStore

HISTORY_NODE_TYPES = {"CompilationRun", "GraphDelta", "ProjectRevision"}
INACTIVE_STATUSES = {"archived", "deleted"}


@dataclass(slots=True)
class RetentionCleanupResult:
    """Observable outcome of one automatic project retention pass."""

    cutoff: str
    project_id: str
    nodes_removed: int = 0
    edges_removed: int = 0
    usage_events_removed: int = 0
    usage_entries_removed: int = 0
    bytes_reclaimed: int = 0
    compacted: bool = False

    @property
    def records_removed(self) -> int:
        return self.nodes_removed + self.edges_removed

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "records_removed": self.records_removed}


def prune_project_data(
    store: GraphStore,
    *,
    project_id: str,
    retention_days: int,
    now: datetime | None = None,
) -> RetentionCleanupResult:
    """Prune expired data owned by one project and compact after graph deletion."""

    cutoff_dt = (now or utcnow()) - timedelta(days=retention_days)
    cutoff = cutoff_dt.isoformat()
    result = RetentionCleanupResult(cutoff=cutoff, project_id=project_id)
    nodes = store.all_nodes()
    project_node_ids = {
        node.id
        for node in nodes
        if node.id == project_id or str(node.properties.get("project_id") or "") == project_id
    }
    protected_history = _newest_history_ids(nodes, project_id)
    removal_ids = {
        node.id
        for node in nodes
        if _expired_project_node(node, project_id, cutoff_dt, protected_history)
    }

    edges = store.all_edges()
    removal_edge_ids = {
        edge.id
        for edge in edges
        if edge.from_id in removal_ids
        or edge.to_id in removal_ids
        or _expired_project_edge(edge, project_id, project_node_ids, cutoff_dt)
    }
    if removal_ids or removal_edge_ids:
        with store.transaction():
            for edge_id in sorted(removal_edge_ids):
                result.edges_removed += int(store.remove_edge(edge_id))
            for node_id in sorted(removal_ids):
                result.nodes_removed += int(store.remove_node(node_id))

    usage = store.prune_usage_events(cutoff=cutoff, node_ids=project_node_ids)
    result.usage_events_removed = int(usage.get("events_removed", 0))
    result.usage_entries_removed = int(usage.get("entries_removed", 0))
    result.bytes_reclaimed += int(usage.get("bytes_reclaimed", 0))

    compact = getattr(store, "compact_storage", None)
    if result.records_removed and compact is not None:
        payload = compact()
        result.compacted = True
        result.bytes_reclaimed += int(payload.get("bytes_reclaimed", 0))
    return result


def _newest_history_ids(nodes: list[MemoryNode], project_id: str) -> set[str]:
    protected: set[str] = set()
    for node_type in HISTORY_NODE_TYPES:
        candidates = [
            node
            for node in nodes
            if node.type == node_type
            and str(node.properties.get("project_id") or "") == project_id
            and (node_type != "CompilationRun" or node.properties.get("status") == "completed")
        ]
        if candidates:
            protected.add(max(candidates, key=_node_timestamp).id)
    return protected


def _expired_project_node(
    node: MemoryNode,
    project_id: str,
    cutoff: datetime,
    protected_history: set[str],
) -> bool:
    if str(node.properties.get("project_id") or "") != project_id:
        return False
    if node.type in HISTORY_NODE_TYPES:
        return node.id not in protected_history and _node_timestamp(node) < cutoff
    return node.status in INACTIVE_STATUSES and _node_timestamp(node) < cutoff


def _expired_project_edge(
    edge: MemoryEdge,
    project_id: str,
    project_node_ids: set[str],
    cutoff: datetime,
) -> bool:
    if str(edge.properties.get("status") or "") not in INACTIVE_STATUSES:
        return False
    belongs_to_project = (
        str(edge.properties.get("project_id") or "") == project_id
        or edge.from_id in project_node_ids
        or edge.to_id in project_node_ids
    )
    return belongs_to_project and _edge_timestamp(edge) < cutoff


def _node_timestamp(node: MemoryNode) -> datetime:
    for value in (
        node.properties.get("completed_at"),
        node.properties.get("updated_at"),
        node.updated_at,
        node.properties.get("created_at"),
        node.created_at,
    ):
        parsed = _safe_parse(value)
        if parsed is not None:
            return parsed
    return utcnow()


def _edge_timestamp(edge: MemoryEdge) -> datetime:
    return _safe_parse(edge.properties.get("updated_at")) or _safe_parse(edge.updated_at) or utcnow()


def _safe_parse(value: Any) -> datetime | None:
    try:
        return parse_dt(str(value or ""))
    except ValueError:
        return None


__all__ = ["RetentionCleanupResult", "prune_project_data"]
