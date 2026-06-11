"""Hub analysis with generic high-degree node penalties."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..domain.constants import ACTIVE_STATUSES
from ..domain.models import MemoryNode
from ..ports.graph_store import GraphStore
from .centrality import CentralityCalculator, CentralityMetrics
from .communities import CommunityDetector
from .specificity import SpecificityScorer, SpecificityScore


@dataclass(frozen=True, slots=True)
class HubScore:
    node_id: str
    node_type: str
    label: str
    hub_score: float
    centrality_score: float
    specificity_score: float
    community_bridge_score: float
    generic_penalty: float
    hub_rank: int
    is_hub: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class HubReport:
    hubs: list[HubScore]
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        return {"hubs": [hub.to_dict() for hub in self.hubs], "warnings": list(self.warnings)}


class HubAnalyzer:
    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self.communities = CommunityDetector(store)
        self.centrality = CentralityCalculator(store)
        self.specificity = SpecificityScorer(store)

    def analyze(
        self,
        *,

        project_id: str | None = None,
        limit: int = 20,
        node_types: set[str] | None = None,
        ensure_communities: bool = False,
    ) -> HubReport:
        candidate_limit = max(limit * 12, 120)
        candidates = self._candidate_nodes(project_id=project_id, node_types=node_types, limit=candidate_limit)
        community_result = self.communities.detect(project_id=project_id, limit=candidate_limit) if ensure_communities else None
        membership = community_result.membership if community_result else self._existing_membership()
        centrality = self.centrality.calculate(

            project_id=project_id,
            community_by_node=membership,
            candidate_nodes=candidates,
            edge_limit=max(candidate_limit * 40, 5000),
        )
        scores: list[HubScore] = []
        for node in candidates:
            if not _rankable_hub_node(node):
                continue
            metrics = centrality.get(node.id)
            if metrics is None:
                continue
            specificity = self.specificity.score(node)
            score = _hub_score(node, metrics, specificity)
            reasons = _hub_reasons(node, metrics, specificity, score)
            scores.append(
                HubScore(
                    node_id=node.id,
                    node_type=node.type,
                    label=_hub_label(node),
                    hub_score=score,
                    centrality_score=(metrics.degree_centrality + metrics.weighted_degree) / 2,
                    specificity_score=specificity.specificity,
                    community_bridge_score=metrics.community_bridge_score,
                    generic_penalty=specificity.generic_penalty,
                    hub_rank=0,
                    is_hub=False,
                    reasons=reasons,
                )
            )

        scores.sort(key=lambda item: (item.hub_score, item.specificity_score, item.centrality_score), reverse=True)
        ranked = []
        for rank, score in enumerate(scores[:limit], start=1):
            is_hub = score.hub_score >= 0.18 and score.generic_penalty < 0.65
            ranked_score = HubScore(
                node_id=score.node_id,
                node_type=score.node_type,
                label=score.label,
                hub_score=score.hub_score,
                centrality_score=score.centrality_score,
                specificity_score=score.specificity_score,
                community_bridge_score=score.community_bridge_score,
                generic_penalty=score.generic_penalty,
                hub_rank=rank,
                is_hub=is_hub,
                reasons=score.reasons,
            )
            ranked.append(ranked_score)
            self._update_node(ranked_score)

        warnings = [f"Generic high-degree candidate penalized: {score.label}" for score in scores if score.generic_penalty >= 0.45 and score.centrality_score >= 0.5][:10]
        return HubReport(hubs=ranked, warnings=warnings)

    def explain(self, node_id: str) -> HubScore | None:
        report = self.analyze(limit=1000, ensure_communities=False)
        for hub in report.hubs:
            if hub.node_id == node_id:
                return hub
        node = self.store.get_node(node_id)
        if node is None:
            return None
        metrics = self.centrality.calculate().get(node.id)
        if metrics is None:
            return None
        spec = self.specificity.score(node)
        score = _hub_score(node, metrics, spec)
        return HubScore(node.id, node.type, _hub_label(node), score, (metrics.degree_centrality + metrics.weighted_degree) / 2, spec.specificity, metrics.community_bridge_score, spec.generic_penalty, 0, False, _hub_reasons(node, metrics, spec, score))

    def _candidate_nodes(self, *, project_id: str | None, node_types: set[str] | None, limit: int) -> list[MemoryNode]:
        blocked = {"Community", "CompilationRun", "GraphDelta", "ArtifactCacheEntry", "RetrievalTrace"}
        return [
            node
            for node, _, _ in self.store.top_nodes_by_degree(
                limit=limit,
                node_types=node_types,
                statuses=set(ACTIVE_STATUSES),
                exclude_types=blocked,
                ignored_edge_types={"BELONGS_TO_COMMUNITY", "BRIDGES_COMMUNITY"},
                project_id=project_id,
                include_global_project=True,
            )
        ]

    def _existing_membership(self) -> dict[str, str]:
        membership = {}
        for edge in self.store.get_edges(type_="BELONGS_TO_COMMUNITY", limit=100000):
            membership[edge.from_id] = edge.to_id
        return membership

    def _update_node(self, score: HubScore) -> None:
        node = self.store.get_node(score.node_id)
        if node is None:
            return
        properties = dict(node.properties)
        properties.update(
            {
                "hub_score": score.hub_score,
                "centrality_score": score.centrality_score,
                "specificity_score": score.specificity_score,
                "community_bridge_score": score.community_bridge_score,
                "is_hub": score.is_hub,
                "hub_rank": score.hub_rank,
                "hub_reason": "; ".join(score.reasons),
                "generic_penalty": score.generic_penalty,
            }
        )
        self.store.update_node_fields(
            node.id,
            properties=properties,
            salience=max(node.salience, min(1.0, score.hub_score)),
        )


def _hub_score(node: MemoryNode, metrics: CentralityMetrics, spec: SpecificityScore) -> float:
    score = (
        0.20 * metrics.degree_centrality
        + 0.15 * metrics.weighted_degree
        + 0.15 * metrics.activation_frequency
        + 0.15 * metrics.retrieval_usefulness
        + 0.10 * node.salience
        + 0.10 * metrics.community_bridge_score
        + 0.10 * spec.specificity
        + 0.05 * node.confidence
        - 0.20 * spec.generic_penalty
    )
    return max(0.0, min(1.0, score))


def _hub_reasons(node: MemoryNode, metrics: CentralityMetrics, spec: SpecificityScore, score: float) -> list[str]:
    reasons = [
        f"degree={metrics.degree_centrality:.2f}",
        f"weighted_degree={metrics.weighted_degree:.2f}",
        f"specificity={spec.specificity:.2f}",
        f"generic_penalty={spec.generic_penalty:.2f}",
        f"score={score:.2f}",
    ]
    if metrics.community_bridge_score > 0:
        reasons.append(f"bridges communities={metrics.community_bridge_score:.2f}")
    reasons.extend(spec.reasons[:3])
    if spec.generic_penalty >= 0.45:
        reasons.append("warning: generic high-degree node")
    return reasons


def _rankable_hub_node(node: MemoryNode) -> bool:
    meaningful = _first_meaningful_label(node)
    if meaningful is None:
        return False
    if node.type == "CodeSymbol" and (node.properties.get("external") or node.properties.get("synthetic") or node.properties.get("kind") in {"external", "decorator"}):
        return False
    return True


def _hub_label(node: MemoryNode) -> str:
    return _first_meaningful_label(node) or node.id


def _first_meaningful_label(node: MemoryNode) -> str | None:
    for value in (node.label, node.text, node.properties.get("qualified_name"), node.properties.get("name"), node.canonical_key):
        if value is None:
            continue
        text = str(value).strip()
        if text and text.casefold() not in {"none", "null", "undefined", "unknown", "nan"}:
            return text
    return None
