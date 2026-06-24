"""Compilation run and graph delta persistence."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..domain.ids import new_id
from ..domain.models import MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore


@dataclass(slots=True)
class CompilationRun:
    id: str
    project_id: str
    started_at: str
    completed_at: str | None = None
    status: str = "running"
    files_seen: int = 0
    files_changed: int = 0
    files_skipped: int = 0
    files_deleted: int = 0
    nodes_created: int = 0
    nodes_updated: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class GraphDelta:
    id: str
    run_id: str
    project_id: str
    artifact_id: str
    added_nodes: list[str] = field(default_factory=list)
    updated_nodes: list[str] = field(default_factory=list)
    archived_nodes: list[str] = field(default_factory=list)
    added_edges: list[str] = field(default_factory=list)
    updated_edges: list[str] = field(default_factory=list)
    archived_edges: list[str] = field(default_factory=list)
    affected_node_ids: list[str] = field(default_factory=list)
    affected_community_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class DeltaRepository:
    """Persists compilation runs and deltas as generic graph nodes."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def new_run(self, *, project_id: str) -> CompilationRun:
        return CompilationRun(id=new_id("compile-run"), project_id=project_id, started_at=utcnow_iso())

    def persist_run(self, run: CompilationRun) -> MemoryNode:
        node, _ = self.store.upsert_node(_run_node(run))
        return node

    def persist_delta(self, delta: GraphDelta) -> MemoryNode:
        node, _ = self.store.upsert_node(_delta_node(delta))
        return node

    def list_deltas(self, *, limit: int = 20) -> list[GraphDelta]:
        nodes = self.store.find_nodes_by_property("kind", "compilation", type_="GraphDelta", limit=max(limit * 5, limit))
        deltas = [_delta_from_node(node) for node in nodes if node.type == "GraphDelta"]
        deltas.sort(key=lambda item: item.created_at, reverse=True)
        return deltas[:limit]

    def get_delta(self, delta_id: str) -> GraphDelta | None:
        node = self.store.get_node(delta_id)
        if node is None or node.type != "GraphDelta" or node.properties.get("kind") != "compilation":
            return None
        return _delta_from_node(node)


def _run_node(run: CompilationRun) -> MemoryNode:
    return MemoryNode(
        id=run.id,
        type="CompilationRun",
        label=run.status,
        canonical_key=run.id,
        properties=run.to_dict(),
        salience=0.05,
        confidence=1.0,
        status="active",
    )


def _delta_node(delta: GraphDelta) -> MemoryNode:
    properties = delta.to_dict()
    properties["kind"] = "compilation"
    return MemoryNode(
        id=delta.id,
        type="GraphDelta",
        label=f"Compilation delta {delta.run_id}",
        canonical_key=delta.id,
        properties=properties,
        salience=0.08,
        confidence=1.0,
        status="active",
    )


def _delta_from_node(node: MemoryNode) -> GraphDelta:
    data: dict[str, Any] = dict(node.properties)
    return GraphDelta(
        id=str(data.get("id") or node.id),
        run_id=str(data["run_id"]),
        project_id=str(data["project_id"]),
        artifact_id=str(data["artifact_id"]),
        added_nodes=list(data.get("added_nodes") or []),
        updated_nodes=list(data.get("updated_nodes") or []),
        archived_nodes=list(data.get("archived_nodes") or []),
        added_edges=list(data.get("added_edges") or []),
        updated_edges=list(data.get("updated_edges") or []),
        archived_edges=list(data.get("archived_edges") or []),
        affected_node_ids=list(data.get("affected_node_ids") or []),
        affected_community_ids=list(data.get("affected_community_ids") or []),
        created_at=str(data.get("created_at") or node.created_at),
    )
