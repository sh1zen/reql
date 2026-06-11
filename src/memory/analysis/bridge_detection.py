"""Deterministic cross-community bridge candidate detection."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

from ..domain.constants import ACTIVE_STATUSES
from ..domain.models import MemoryEdge, MemoryNode
from ..ports.graph_store import GraphStore
from .communities import CommunityDetector
from .specificity import SpecificityScorer


IGNORED_EDGE_TYPES = {
    "BELONGS_TO_COMMUNITY",
    "BRIDGES_COMMUNITY",
    "SUPPORTED_BY",
}

IGNORED_NODE_TYPES = {
    "Community",
    "CompilationRun",
    "GraphDelta",
    "ArtifactCacheEntry",
    "RetrievalTrace",
    "Bridge",
}


@dataclass(frozen=True, slots=True)
class BridgeCandidate:
    bridge_node_id: str
    bridge_label: str
    bridge_type: str
    community_ids: tuple[str, ...]
    community_labels: tuple[str, ...]
    evidence_node_ids: tuple[str, ...]
    evidence_labels: tuple[str, ...]
    cross_community_distance: float
    novelty: float
    potential_utility: float
    evidence_strength: float
    bridge_centrality: float
    specificity: float
    generic_penalty: float
    randomness_penalty: float
    bridge_score: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class BridgeDetector:
    """Finds specific nodes that bridge multiple existing communities."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self.communities = CommunityDetector(store)
        self.specificity = SpecificityScorer(store)

    def find_candidates(
        self,
        *,

        project_id: str | None = None,
        limit: int = 50,
    ) -> list[BridgeCandidate]:
        membership = self._existing_membership(project_id=project_id)
        if not membership:
            membership = self.communities.detect(project_id=project_id, limit=None).membership
        community_labels = self._community_labels(set(membership.values()))
        community_members = _community_members(membership)
        nodes = self._candidate_nodes(project_id=project_id)
        candidates: list[BridgeCandidate] = []
        for node in nodes:
            incident = self._incident_evidence(node)
            by_community: dict[str, list[tuple[MemoryNode, float]]] = defaultdict(list)
            for neighbor, strength in incident:
                community_id = membership.get(neighbor.id)
                if community_id:
                    by_community[community_id].append((neighbor, strength))
            if len(by_community) < 2:
                continue

            evidence = _select_evidence(by_community)
            evidence_strength = sum(strength for _, strength in evidence) / max(len(evidence), 1)
            if len(evidence) < 2 and evidence_strength < 0.90:
                continue

            spec = self.specificity.score(node)
            if spec.generic_penalty >= 0.45:
                continue
            distance = self._cross_community_distance(
                sorted(by_community),
                community_members=community_members,
                ignored_node_id=node.id,

            )
            bridge_centrality = min(1.0, (len(by_community) / 3.0) * 0.55 + min(1.0, len(incident) / 12.0) * 0.45)
            novelty = min(1.0, distance * 0.75 + min(1.0, len(by_community) / 4.0) * 0.25)
            utility = _potential_utility(node, spec.specificity, evidence_strength)
            randomness_penalty = _randomness_penalty(evidence_strength, len(evidence), distance, spec.specificity)
            score = _bridge_score(
                cross_community_distance=distance,
                novelty=novelty,
                potential_utility=utility,
                evidence_strength=evidence_strength,
                bridge_centrality=bridge_centrality,
                specificity=spec.specificity,
                randomness_penalty=randomness_penalty,
                generic_penalty=spec.generic_penalty,
            )
            if score < 0.30 or spec.generic_penalty >= 0.75:
                continue

            community_ids = tuple(sorted(by_community))
            candidates.append(
                BridgeCandidate(
                    bridge_node_id=node.id,
                    bridge_label=_node_label(node),
                    bridge_type=node.type,
                    community_ids=community_ids,
                    community_labels=tuple(community_labels.get(cid, cid) for cid in community_ids),
                    evidence_node_ids=tuple(neighbor.id for neighbor, _ in evidence),
                    evidence_labels=tuple(_node_label(neighbor) for neighbor, _ in evidence),
                    cross_community_distance=distance,
                    novelty=novelty,
                    potential_utility=utility,
                    evidence_strength=evidence_strength,
                    bridge_centrality=bridge_centrality,
                    specificity=spec.specificity,
                    generic_penalty=spec.generic_penalty,
                    randomness_penalty=randomness_penalty,
                    bridge_score=score,
                )
            )
        candidates.sort(key=lambda item: (item.bridge_score, item.specificity, item.evidence_strength), reverse=True)
        return candidates[:limit]

    def _existing_membership(self, *, project_id: str | None) -> dict[str, str]:
        membership: dict[str, str] = {}
        for edge in self.store.get_edges(type_="BELONGS_TO_COMMUNITY", limit=100000):
            if project_id and edge.properties.get("project_id") not in {project_id, None}:
                continue
            membership[edge.from_id] = edge.to_id
        return membership

    def _community_labels(self, community_ids: set[str]) -> dict[str, str]:
        labels = {}
        for community_id in community_ids:
            node = self.store.get_node(community_id)
            if node:
                labels[community_id] = node.label or node.text or node.id
        return labels

    def _candidate_nodes(self, *, project_id: str | None) -> list[MemoryNode]:
        return [
            node
            for node, _, _ in self.store.top_nodes_by_degree(
                limit=500,
                statuses=set(ACTIVE_STATUSES),
                exclude_types=IGNORED_NODE_TYPES,
                ignored_edge_types=IGNORED_EDGE_TYPES,
                project_id=project_id,
                include_global_project=True,
            )
        ]

    def _incident_evidence(self, node: MemoryNode) -> list[tuple[MemoryNode, float]]:
        evidence: dict[str, tuple[MemoryNode, float]] = {}
        for edge in [*self.store.get_edges(from_id=node.id, limit=500), *self.store.get_edges(to_id=node.id, limit=500)]:
            if edge.type in IGNORED_EDGE_TYPES:
                continue
            neighbor_id = edge.to_id if edge.from_id == node.id else edge.from_id
            neighbor = self.store.get_node(neighbor_id)
            if not neighbor or neighbor.status in {"archived", "deleted", "rejected"}:
                continue
            strength = max(0.0, min(1.0, edge.weight * edge.confidence))
            existing = evidence.get(neighbor.id)
            if existing is None or strength > existing[1]:
                evidence[neighbor.id] = (neighbor, strength)
        return list(evidence.values())

    def _cross_community_distance(
        self,
        community_ids: list[str],
        *,
        community_members: dict[str, set[str]],
        ignored_node_id: str,

    ) -> float:
        if len(community_ids) < 2:
            return 0.0
        member_ids: set[str] = set()
        for community_id in community_ids:
            member_ids.update(community_members.get(community_id, set()) - {ignored_node_id})
        candidate_edges = self.store.incident_edges(
            sorted(member_ids),
            ignored_edge_types=IGNORED_EDGE_TYPES,
            limit=max(1000, min(50000, len(member_ids) * 50)),
        )
        direct_strength = 0.0
        pair_count = 0
        for i, left in enumerate(community_ids):
            left_members = community_members.get(left, set()) - {ignored_node_id}
            for right in community_ids[i + 1 :]:
                right_members = community_members.get(right, set()) - {ignored_node_id}
                pair_count += 1
                direct_strength += _direct_connectivity(candidate_edges, left_members, right_members, ignored_node_id)
        obviousness = min(1.0, direct_strength / max(pair_count, 1))
        return max(0.0, min(1.0, 1.0 - obviousness))


def _community_members(membership: dict[str, str]) -> dict[str, set[str]]:
    members: dict[str, set[str]] = defaultdict(set)
    for node_id, community_id in membership.items():
        members[community_id].add(node_id)
    return members


def _select_evidence(by_community: dict[str, list[tuple[MemoryNode, float]]]) -> list[tuple[MemoryNode, float]]:
    selected = []
    for items in by_community.values():
        items.sort(key=lambda item: (item[1], item[0].salience, _node_label(item[0])), reverse=True)
        selected.append(items[0])
    selected.sort(key=lambda item: (item[1], item[0].salience), reverse=True)
    return selected[:6]


def _direct_connectivity(edges: list[MemoryEdge], left_members: set[str], right_members: set[str], ignored_node_id: str) -> float:
    strength = 0.0
    for edge in edges:
        if edge.type in IGNORED_EDGE_TYPES or ignored_node_id in {edge.from_id, edge.to_id}:
            continue
        crosses = (edge.from_id in left_members and edge.to_id in right_members) or (edge.from_id in right_members and edge.to_id in left_members)
        if crosses:
            strength += max(0.0, edge.weight * edge.confidence)
    return min(1.0, strength)


def _potential_utility(node: MemoryNode, specificity: float, evidence_strength: float) -> float:
    stored = max(float(node.utility or 0.0), float(node.properties.get("usage_success") or 0.0), float(node.properties.get("retrieval_usefulness") or 0.0))
    graph_signal = node.salience * 0.35 + specificity * 0.40 + evidence_strength * 0.25
    return max(0.0, min(1.0, max(stored, graph_signal)))


def _randomness_penalty(evidence_strength: float, evidence_count: int, distance: float, specificity: float) -> float:
    penalty = 0.0
    if evidence_count < 2:
        penalty += 0.35
    if evidence_strength < 0.45:
        penalty += 0.25
    if specificity < 0.35:
        penalty += 0.20
    if distance < 0.25:
        penalty += 0.20
    return max(0.0, min(1.0, penalty))


def _bridge_score(
    *,
    cross_community_distance: float,
    novelty: float,
    potential_utility: float,
    evidence_strength: float,
    bridge_centrality: float,
    specificity: float,
    randomness_penalty: float,
    generic_penalty: float,
) -> float:
    score = (
        0.25 * cross_community_distance
        + 0.20 * novelty
        + 0.20 * potential_utility
        + 0.15 * evidence_strength
        + 0.10 * bridge_centrality
        + 0.10 * specificity
        - 0.25 * randomness_penalty
        - 0.15 * generic_penalty
    )
    return max(0.0, min(1.0, score))


def _node_label(node: MemoryNode) -> str:
    return node.label or node.text or node.canonical_key or node.id
