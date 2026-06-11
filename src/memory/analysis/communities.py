"""Lightweight deterministic community detection."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..ports.graph_store import GraphStore


@dataclass(slots=True)
class CommunityResult:
    community_nodes: list[MemoryNode]
    membership: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {"communities": [node.to_dict() for node in self.community_nodes], "membership": dict(self.membership)}


class CommunityDetector:
    """Deterministic label propagation suitable for local graphs."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def detect(
        self,
        *,

        project_id: str | None = None,
        limit: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> CommunityResult:
        options = options or {}
        max_iterations = int(options.get("max_iterations", 12))
        min_size = int(options.get("min_size", 2))
        nodes = _candidate_nodes(self.store.all_nodes(), project_id)
        node_ids = {node.id for node in nodes}
        adjacency = _adjacency(self.store.all_edges(), node_ids)
        labels = {node_id: node_id for node_id in node_ids}

        for _ in range(max_iterations):
            changed = False
            for node_id in sorted(node_ids):
                scores: Counter[str] = Counter()
                for neighbor, weight in adjacency[node_id].items():
                    scores[labels[neighbor]] += weight
                if not scores:
                    continue
                best_score = max(scores.values())
                best = sorted(label for label, score in scores.items() if score == best_score)[0]
                if best != labels[node_id]:
                    labels[node_id] = best
                    changed = True
            if not changed:
                break

        grouped: dict[str, list[str]] = defaultdict(list)
        for node_id, label in labels.items():
            grouped[label].append(node_id)
        communities = [sorted(member_ids) for member_ids in grouped.values() if len(member_ids) >= min_size]
        communities.sort(key=lambda members: (-len(members), members[0]))
        if limit:
            communities = communities[:limit]

        membership: dict[str, str] = {}
        community_nodes: list[MemoryNode] = []
        nodes_by_id = {node.id: node for node in nodes}
        for index, member_ids in enumerate(communities, start=1):
            community_id = stable_id("community", project_id or "global", ",".join(member_ids))
            member_nodes = [node for node_id in member_ids if (node := nodes_by_id.get(node_id)) is not None]
            label = _community_label(member_nodes, index)
            density = _density(member_ids, adjacency)
            salience = sum(node.salience for node in member_nodes) / max(len(member_ids), 1)
            now = utcnow_iso()
            community_node = MemoryNode(
                id=community_id,

                type="Community",
                label=label,
                canonical_key=community_id,
                properties={
                    "id": community_id,
                    "project_id": project_id,
                    "label": label,
                    "size": len(member_ids),
                    "density": density,
                    "salience": salience,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                    "member_ids": member_ids,
                },
                salience=salience,
                confidence=1.0,
                status="active",
            )
            community_node, _ = self.store.upsert_node(community_node)
            community_nodes.append(community_node)
            for node_id in member_ids:
                membership[node_id] = community_node.id
                self.store.upsert_edge(
                    MemoryEdge(
                        id=stable_id("edge", node_id, "BELONGS_TO_COMMUNITY", community_node.id),

                        from_id=node_id,
                        to_id=community_node.id,
                        type="BELONGS_TO_COMMUNITY",
                        weight=1.0,
                        confidence=1.0,
                        origin="deterministic",
                        properties={"project_id": project_id, "community_id": community_node.id},
                    )
                )

        self._mark_bridges(project_id=project_id, adjacency=adjacency, membership=membership)
        return CommunityResult(community_nodes=community_nodes, membership=membership)

    def _mark_bridges(self, *, project_id: str | None, adjacency: dict[str, dict[str, float]], membership: dict[str, str]) -> None:
        for node_id, neighbors in adjacency.items():
            node_community = membership.get(node_id)
            neighbor_communities = {
                community_id
                for neighbor in neighbors
                if (community_id := membership.get(neighbor)) and community_id != node_community
            }
            for community_id in sorted(neighbor_communities):
                self.store.upsert_edge(
                    MemoryEdge(
                        id=stable_id("edge", node_id, "BRIDGES_COMMUNITY", community_id),

                        from_id=node_id,
                        to_id=community_id,
                        type="BRIDGES_COMMUNITY",
                        weight=0.7,
                        confidence=0.8,
                        origin="deterministic",
                        properties={"project_id": project_id, "community_id": community_id},
                    )
                )


def _candidate_nodes(nodes: list[MemoryNode], project_id: str | None) -> list[MemoryNode]:
    blocked = {"Community", "CompilationRun", "GraphDelta", "ArtifactCacheEntry", "RetrievalTrace"}
    out = []
    for node in nodes:
        if node.type in blocked or node.status in {"archived", "deleted", "rejected"}:
            continue
        if project_id and node.properties.get("project_id") not in {project_id, None}:
            continue
        out.append(node)
    return out


def _adjacency(edges: list[MemoryEdge], node_ids: set[str]) -> dict[str, dict[str, float]]:
    adjacency: dict[str, dict[str, float]] = {node_id: {} for node_id in node_ids}
    ignored = {"BELONGS_TO_COMMUNITY", "BRIDGES_COMMUNITY"}
    for edge in edges:
        if edge.type in ignored or edge.from_id not in node_ids or edge.to_id not in node_ids:
            continue
        weight = max(0.0, edge.weight * edge.confidence)
        adjacency[edge.from_id][edge.to_id] = adjacency[edge.from_id].get(edge.to_id, 0.0) + weight
        adjacency[edge.to_id][edge.from_id] = adjacency[edge.to_id].get(edge.from_id, 0.0) + weight
    return adjacency


def _density(member_ids: list[str], adjacency: dict[str, dict[str, float]]) -> float:
    size = len(member_ids)
    if size < 2:
        return 0.0
    possible = size * (size - 1) / 2
    actual = 0
    members = set(member_ids)
    for node_id in member_ids:
        actual += sum(1 for neighbor in adjacency[node_id] if neighbor in members)
    return min(1.0, (actual / 2) / possible)


def _community_label(nodes: list[MemoryNode], index: int) -> str:
    labels = []
    for node in nodes:
        value = node.properties.get("name") or node.label or node.text or node.canonical_key
        if value:
            labels.append(str(value).splitlines()[0][:40])
    return " / ".join(labels[:3]) if labels else f"Community {index}"
