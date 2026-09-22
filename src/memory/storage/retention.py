"""Commit-count retention for superseded project graph and usage history."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
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
    retention_commits: int,
) -> RetentionCleanupResult:
    """Prune data older than the project's retained meaningful compile commits."""

    if retention_commits < 1:
        raise ValueError("retention_commits must be greater than zero")
    revisions = sorted(
        store.find_nodes_by_property(
            "project_id",
            project_id,
            type_="ProjectRevision",
            limit=retention_commits + 1,
            clone=False,
        ),
        key=_revision_order,
        reverse=True,
    )
    oldest_retained = revisions[min(len(revisions), retention_commits) - 1]
    cutoff_dt = _node_timestamp(oldest_retained)
    cutoff = cutoff_dt.isoformat()
    result = RetentionCleanupResult(cutoff=cutoff, project_id=project_id)
    if len(revisions) <= retention_commits:
        return result

    nodes = store.find_nodes_by_property(
        "project_id",
        project_id,
        limit=None,
        clone=False,
    )
    project_node = store.get_node(project_id, clone=False)
    if project_node is not None:
        nodes.append(project_node)
    project_node_ids = {
        node.id
        for node in nodes
        if node.id == project_id or str(node.properties.get("project_id") or "") == project_id
    }
    protected_history = _retained_history_ids(nodes, revisions[:retention_commits])
    removal_ids = {
        node.id
        for node in nodes
        if _expired_project_node(node, project_id, cutoff_dt, protected_history)
    }

    edges_by_id = {
        edge.id: edge
        for edge in store.incident_edges(
            sorted(project_node_ids),
            limit=None,
            clone=False,
        )
    }
    edges_by_id.update(
        {
            edge.id: edge
            for edge in store.find_edges_by_property(
                "project_id",
                project_id,
                limit=2**63 - 1,
                clone=False,
            )
        }
    )
    removal_edge_ids = {
        edge.id
        for edge in edges_by_id.values()
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


def _retained_history_ids(
    nodes: list[MemoryNode],
    retained_revisions: list[MemoryNode],
) -> set[str]:
    """Return retained revisions plus their originating runs and deltas."""

    retained_revision_ids = {revision.id for revision in retained_revisions}
    retained_run_ids = {
        str(revision.properties.get("run_id") or "")
        for revision in retained_revisions
    }
    return {
        node.id
        for node in nodes
        if node.id in retained_run_ids
        or node.id in retained_revision_ids
        or (
            node.type == "GraphDelta"
            and str(node.properties.get("run_id") or "") in retained_run_ids
        )
    }


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


def _revision_order(node: MemoryNode) -> tuple[int, datetime, str]:
    """Order project revisions by their monotonic sequence with stable fallbacks."""

    return (int(node.properties.get("sequence") or 0), _node_timestamp(node), node.id)


def _edge_timestamp(edge: MemoryEdge) -> datetime:
    return _safe_parse(edge.properties.get("updated_at")) or _safe_parse(edge.updated_at) or utcnow()


def _safe_parse(value: Any) -> datetime | None:
    try:
        return parse_dt(str(value or ""))
    except ValueError:
        return None


__all__ = ["RetentionCleanupResult", "prune_project_data"]
