"""Bounded graph expansion for retrieval candidates."""
from __future__ import annotations

from .common import *


class GraphExpansionMixin:
    def _expand_and_rank_candidates(
        self,
        seed_node_ids: list[str],
        seed_scores: OrderedDict[str, float],
        query: MemoryQuery,
        query_profile: _QueryProfile,
        *,
        edge_types: set[str],
        code_context: bool,
    ) -> tuple[OrderedDict[str, _PathCandidate], OrderedDict[str, MemoryEdge]]:
        candidates: dict[str, _PathCandidate] = {}
        candidate_edges: OrderedDict[str, MemoryEdge] = OrderedDict()
        queue: list[tuple[str, int, float, float, set[str], list[str]]] = []
        seen_depth: dict[str, int] = {}
        metrics_by_node: dict[str, dict[str, float]] = {}
        overlap_by_node: dict[str, set[str]] = {}

        def metrics_for(node: MemoryNode) -> dict[str, float]:
            metrics = metrics_by_node.get(node.id)
            if metrics is None:
                metrics = self._node_match_metrics(node, query_profile)
                metrics_by_node[node.id] = metrics
            return metrics

        def overlap_for(node: MemoryNode) -> set[str]:
            overlap = overlap_by_node.get(node.id)
            if overlap is None:
                overlap = self._node_query_token_overlap_tokens(node, query_profile.informative_tokens)
                overlap_by_node[node.id] = overlap
            return overlap

        for seed_id in seed_node_ids:
            seed = self.store.get_node(seed_id)
            if seed is None:
                continue
            if not self._candidate_node_allowed(seed, query, code_context=code_context):
                continue
            seed_score = seed_scores.get(seed_id, 0.0)
            seed_tokens = overlap_for(seed)
            self._add_path_candidate(
                candidates,
                seed,
                query_profile,
                seed_score=seed_score,
                path_tokens=seed_tokens,
                depth=0,
                edge_signal=1.0,
                edge_ids=[],
                metrics=metrics_for(seed),
            )
            queue.append((seed_id, 0, seed_score, seed_score, seed_tokens, []))
            seen_depth[seed_id] = 0

        cursor = 0
        while cursor < len(queue):
            current_id, depth, root_seed_score, current_path_score, path_tokens, path_edge_ids = queue[cursor]
            cursor += 1
            if depth >= query.max_depth:
                continue
            neighbors = self.store.neighbors(
                current_id,
                direction="both",
                edge_types=edge_types,
                min_weight=0.01,
                limit=120,
            )
            for edge, neighbor in neighbors:
                if edge.type in TECHNICAL_EDGE_TYPES:
                    continue
                if not self._candidate_node_allowed(neighbor, query, code_context=code_context):
                    continue
                next_depth = depth + 1
                neighbor_tokens = overlap_for(neighbor)
                combined_tokens = set(path_tokens) | neighbor_tokens
                previous_depth = seen_depth.get(neighbor.id)
                existing = candidates.get(neighbor.id)
                if (
                    previous_depth is not None
                    and previous_depth <= next_depth
                    and existing is not None
                    and existing.coverage >= self._coverage(combined_tokens, query_profile)
                ):
                    continue
                metrics = metrics_for(neighbor)
                if (
                    next_depth > 1
                    and metrics["match_score"] <= 0.0
                    and self._coverage(combined_tokens, query_profile) <= self._coverage(path_tokens, query_profile)
                ):
                    continue
                edge_signal = clamp(edge.weight * edge.confidence * max(edge.polarity, 0))
                next_path_score = clamp(0.55 * current_path_score + 0.25 * self._coverage(combined_tokens, query_profile) + 0.20 * edge_signal)
                next_edge_ids = [*path_edge_ids, edge.id]
                candidate_edges.setdefault(edge.id, edge)
                self._add_path_candidate(
                    candidates,
                    neighbor,
                    query_profile,
                    seed_score=root_seed_score,
                    path_tokens=combined_tokens,
                    depth=next_depth,
                    edge_signal=next_path_score,
                    edge_ids=next_edge_ids,
                    metrics=metrics,
                )
                if previous_depth is None or next_depth < previous_depth:
                    seen_depth[neighbor.id] = next_depth
                    queue.append((neighbor.id, next_depth, root_seed_score, next_path_score, combined_tokens, next_edge_ids))

        ordered = OrderedDict(
            sorted(
                candidates.items(),
                key=lambda item: (
                    -item[1].score,
                    item[1].depth_penalty,
                    self._node_relative_path(item[1].node) or "",
                    self._node_label(item[1].node),
                ),
            )
        )
        return ordered, candidate_edges

    def _candidate_node_allowed(self, node: MemoryNode, query: MemoryQuery, *, code_context: bool) -> bool:
        if not self._is_graph_layer_node(node):
            return False
        if code_context and not self._is_code_context_node(node):
            return False
        if query.node_types and node.type not in query.node_types:
            return False
        scopes = self._normalize_query_context_scopes(query.context_scopes)
        if scopes and not self._node_matches_query_context_scope(node, scopes):
            return False
        if not query.include_archived and node.status in INACTIVE_STATUSES:
            return False
        return True

    def _add_path_candidate(
        self,
        candidates: dict[str, _PathCandidate],
        node: MemoryNode,
        query_profile: _QueryProfile,
        *,
        seed_score: float,
        path_tokens: set[str],
        depth: int,
        edge_signal: float,
        edge_ids: list[str],
        metrics: dict[str, float] | None = None,
    ) -> None:
        metrics = metrics or self._node_match_metrics(node, query_profile)
        direct_coverage = metrics["coverage"]
        path_coverage = max(direct_coverage, self._coverage(path_tokens, query_profile))
        type_bonus = self._retrieval_type_bonus(node, metrics["match_score"])
        depth_penalty = min(0.30, depth * 0.08)
        path_score = clamp(0.60 * edge_signal + 0.40 * path_coverage)
        score = clamp(
            0.52 * metrics["match_score"]
            + 0.28 * path_coverage
            + 0.14 * path_score
            + 0.06 * seed_score
            + type_bonus
            - depth_penalty
        )
        if node.type in SOURCE_NODE_TYPES and edge_ids:
            score = clamp(score - 0.08)
        elif node.type in SOURCE_NODE_TYPES:
            score = clamp(score - 0.16)
        if len(query_profile.informative_tokens) >= 4 and metrics["match_score"] < 0.10 and path_coverage < 0.35:
            return
        existing = candidates.get(node.id)
        candidate = _PathCandidate(
            node=node,
            score=score,
            match_score=metrics["match_score"],
            coverage=direct_coverage,
            path_score=path_score,
            type_bonus=type_bonus,
            seed_score=seed_score,
            depth_penalty=depth_penalty,
            edge_ids=edge_ids,
        )
        if existing is None or candidate.score > existing.score:
            candidates[node.id] = candidate

