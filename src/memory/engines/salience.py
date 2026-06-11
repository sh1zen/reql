"""Salience scoring for graph-native memory."""
from __future__ import annotations

from typing import Any

from ..domain.constants import INACTIVE_STATUSES
from ..domain.models import MemoryNode
from ..extraction.normalization import clamp
from ..domain.timeutils import seconds_since
from ..ports.graph_store import GraphStore


class SalienceEngine:
    """Numerical salience computation independent of LLM calls."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def compute_salience_signal(self, node: MemoryNode) -> dict[str, Any]:
        """Compute deterministic salience for code graph retrieval."""
        props = node.properties
        novelty = clamp(float(props.get("novelty", 1.0 / max(1, node.usage_count + 1))))
        recency = self.recency_score(node)
        graph_centrality = clamp(self.store.degree(node.id) / 12.0)
        code_priority = clamp(float(props.get("cleanup_rank", 0.0) or props.get("priority", 0.0) or 0.0) / 3.0)
        evidence_count = clamp(node.evidence_count / 5.0)
        confidence = clamp(node.confidence)
        salience_score = clamp(
            0.16 * novelty
            + 0.12 * recency
            + 0.24 * graph_centrality
            + 0.18 * code_priority
            + 0.12 * evidence_count
            + 0.18 * confidence
        )
        if node.status in INACTIVE_STATUSES:
            salience_score *= 0.25
        return {
            "input": {
                "novelty": novelty,
                "recency": recency,
                "graph_centrality": graph_centrality,
                "code_priority": code_priority,
                "evidence_count": evidence_count,
            },
            "output": {"salience_score": clamp(salience_score)},
            "used_by": ["retrieval", "code_context"],
        }

    def compute_node_salience(self, node: MemoryNode) -> float:
        type_prior = {
            "Topic": 0.03,
            "Entity": 0.02,
            "Project": 0.05,
            "Directory": 0.04,
            "File": 0.08,
            "SourceArtifact": 0.08,
            "Module": 0.10,
            "Class": 0.11,
            "Interface": 0.10,
            "Function": 0.12,
            "Method": 0.12,
            "Endpoint": 0.12,
            "Schema": 0.10,
            "StaticAnalysisFinding": 0.12,
        }.get(node.type, 0.04)
        signal = self.compute_salience_signal(node)
        return clamp(type_prior + 0.82 * float(signal["output"]["salience_score"]) - 0.08 * clamp(node.volatility))

    def recompute_node(self, node_id: str) -> MemoryNode | None:
        node = self.store.get_node(node_id)
        if node is None:
            return None
        score = self.compute_node_salience(node)
        return self.store.update_node_fields(node.id, salience=score)

    def recompute_user(self, *, limit: int = 5000) -> int:
        nodes = self.store.find_nodes(limit=limit, order_by="updated_at")
        count = 0
        for node in nodes:
            score = self.compute_node_salience(node)
            self.store.update_node_fields(node.id, salience=score)
            count += 1
        return count

    def recency_score(self, node: MemoryNode, *, half_life_days: float = 14.0) -> float:
        elapsed = seconds_since(node.updated_at)
        half_life = max(1.0, half_life_days * 86400.0)
        return clamp(0.5 ** (elapsed / half_life))
