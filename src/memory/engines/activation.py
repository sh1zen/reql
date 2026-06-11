"""Neural-like spreading activation runtime."""
from __future__ import annotations

import heapq
from collections import defaultdict

from ..domain.constants import INACTIVE_STATUSES
from ..domain.models import ActivationOptions, ActivationResult, MemoryEdge, MemoryNode
from ..extraction.normalization import clamp
from ..domain.timeutils import utcnow_iso
from ..ports.graph_store import GraphStore


class ActivationEngine:
    """Propagates activation through the property graph.

    The engine is intentionally local: it starts from seed nodes, traverses only
    the strongest adjacent edges within a bounded depth, applies inhibition via
    negative polarity, then stores the resulting activation values.
    """

    BLOCKED_BY_DEFAULT = {"EXTRACTED_FROM", "COMPILED_IN", "AFFECTED_BY_DELTA"}

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def activate(self, seed_node_ids: list[str], options: ActivationOptions) -> ActivationResult:
        if not seed_node_ids:
            return ActivationResult(active_nodes=[], fired_edges=[], activation_by_node={})

        blocked = set(options.blocked_edge_types or set()) | self.BLOCKED_BY_DEFAULT
        activation: dict[str, float] = defaultdict(float)
        best_depth: dict[str, int] = {}
        fired_edges: dict[str, MemoryEdge] = {}
        heap: list[tuple[float, int, str]] = []

        for seed_id in seed_node_ids:
            node = self.store.get_node(seed_id)
            if node is None:
                continue
            if node.status in INACTIVE_STATUSES:
                continue
            value = clamp(max(options.seed_boost, node.base_activation, node.activation * options.persistence))
            activation[seed_id] = max(activation[seed_id], value)
            best_depth[seed_id] = 0
            heapq.heappush(heap, (-value, 0, seed_id))

        while heap:
            neg_value, depth, current_id = heapq.heappop(heap)
            current_value = -neg_value
            if depth >= options.max_depth:
                continue
            if current_value < options.min_activation:
                continue
            if current_value + 1e-9 < activation[current_id]:
                continue

            neighbors = self.store.neighbors(
                current_id,

                direction="both" if options.traverse_incoming else "out",
                edge_types=options.allowed_edge_types,
                min_weight=0.01,
                limit=120,
            )
            for edge, neighbor in neighbors:
                if edge.type in blocked:
                    continue
                if neighbor.status in INACTIVE_STATUSES:
                    continue
                if options.allowed_edge_types and edge.type not in options.allowed_edge_types:
                    continue
                depth_factor = options.depth_decay ** (depth + 1)
                status_factor = 0.35 if neighbor.status in {"latent", "candidate"} else 1.0
                inhibition_factor = 1.0
                signal = current_value * edge.weight * edge.confidence * depth_factor * status_factor
                if edge.polarity < 0 or edge.type in {"INHIBITS", "SUPPRESSES", "BLOCKS", "WEAKENS"}:
                    signal *= -1.0
                    inhibition_factor = 0.75
                new_value = clamp(activation[neighbor.id] + signal * inhibition_factor)
                if new_value <= activation[neighbor.id] + 1e-9:
                    continue
                activation[neighbor.id] = new_value
                fired_edges[edge.id] = edge
                prev_depth = best_depth.get(neighbor.id)
                next_depth = depth + 1
                if prev_depth is None or next_depth < prev_depth or new_value >= options.min_activation:
                    best_depth[neighbor.id] = next_depth
                    heapq.heappush(heap, (-new_value, next_depth, neighbor.id))

        active_items = sorted(activation.items(), key=lambda item: item[1], reverse=True)
        active_nodes: list[MemoryNode] = []
        now = utcnow_iso()
        if options.update_store:
            for node_id, value in active_items:
                node = self.store.get_node(node_id)
                if not node:
                    continue
                blended = clamp(max(value, node.activation * options.persistence))
                updated = self.store.update_node_fields(
                    node_id,
                    activation=blended,
                    last_activated_at=now,
                    usage_count=node.usage_count + 1,
                )
                if updated:
                    active_nodes.append(updated)
            for edge in fired_edges.values():
                self.store.update_edge_fields(
                    edge.id,
                    co_activation_count=edge.co_activation_count + 1,
                    last_fired_at=now,
                )
        else:
            active_nodes = [n for n in self.store.get_nodes([node_id for node_id, _ in active_items]) if n]

        # Preserve score ordering after updates.
        active_nodes.sort(key=lambda node: activation.get(node.id, 0.0), reverse=True)
        return ActivationResult(
            active_nodes=active_nodes,
            fired_edges=list(fired_edges.values()),
            activation_by_node=dict(activation),
        )

    def reinforce_coactivation(self, active_node_ids: list[str], *, learning_rate: float = 0.03, max_pairs: int = 80) -> list[MemoryEdge]:
        """Hebbian reinforcement for co-active nodes.

        Creates/strengthens ASSOCIATED_WITH edges among the most active nodes.
        The method caps pair count to keep maintenance local and cheap.
        """
        nodes = [n for n in self.store.get_nodes(active_node_ids) if n]
        nodes.sort(key=lambda n: n.activation, reverse=True)
        nodes = nodes[: min(len(nodes), 20)]
        updated: list[MemoryEdge] = []
        pairs = 0
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                if pairs >= max_pairs:
                    return updated
                delta = learning_rate * a.activation * b.activation * min(a.confidence, b.confidence)
                if delta < 0.002:
                    continue
                existing = self.store.get_edge_by_pattern(a.id, b.id, "ASSOCIATED_WITH")
                if existing:
                    edge = self.store.update_edge_fields(
                        existing.id,
                        weight=clamp(existing.weight + delta),
                        co_activation_count=existing.co_activation_count + 1,
                        last_fired_at=utcnow_iso(),
                    )
                    if edge:
                        updated.append(edge)
                else:
                    edge, _ = self.store.upsert_edge(
                        MemoryEdge(

                            from_id=a.id,
                            to_id=b.id,
                            type="ASSOCIATED_WITH",
                            weight=clamp(0.20 + delta),
                            confidence=min(a.confidence, b.confidence, 0.75),
                            origin="statistical",
                            properties={"rule": "hebbian_coactivation"},
                        )
                    )
                    updated.append(edge)
                pairs += 1
        return updated
