"""Public deterministic retrieval engine assembled from pipeline components."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext

from ...diagnostics import PerformanceLogger
from ...domain.constants import INACTIVE_STATUSES
from ...domain.ids import stable_id
from ...domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from ...domain.timeutils import utcnow_iso
from ...extraction.deterministic import DeterministicExtractor
from ...extraction.normalization import canonicalize
from ...storage.extractor import SemanticExtractor
from ...storage.graph_store import GraphStore
from .common import (
    CODE_CONTEXT_EDGE_TYPES,
    CODE_CONTEXT_NODE_TYPES,
    DEFAULT_CONTEXT_EDGE_TYPES,
    TECHNICAL_EDGE_TYPES,
    TECHNICAL_NODE_TYPES,
)
from .context.projections.cleanup import CleanupContextProjectionMixin
from .context.projections.code import CodeContextProjectionMixin
from .context.projections.general import GeneralContextProjectionMixin
from .context.renderers.json import JsonContextRendererMixin
from .context.renderers.markdown import MarkdownContextRendererMixin
from .context.service import ContextServiceMixin
from .context.models import NodeMatchMetrics
from .expansion import GraphExpansionMixin
from .search import RetrievalSearchMixin


class RetrievalEngine(
    RetrievalSearchMixin,
    GraphExpansionMixin,
    ContextServiceMixin,
    CodeContextProjectionMixin,
    GeneralContextProjectionMixin,
    CleanupContextProjectionMixin,
    JsonContextRendererMixin,
    MarkdownContextRendererMixin,
):
    """Retrieve and project a relevant subgraph without mandatory LLM calls.

    The class remains the stable public facade. Its inherited components form
    the retrieval pipeline while preserving the established method contracts.
    """
    def __init__(self, store: GraphStore, extractor: SemanticExtractor | None = None, *, profile_logger: PerformanceLogger | None = None) -> None:
        self.store = store
        self.extractor = extractor or DeterministicExtractor()
        self.profile_logger = profile_logger

    def retrieve(self, query: MemoryQuery) -> MemorySubgraph:
        profile = self.profile_logger
        with (profile.span("retrieval.total", top_k=query.top_k, max_depth=query.max_depth) if profile else nullcontext()):
            return self._retrieve(query, profile=profile)

    @staticmethod
    def _is_graph_layer_node(node: MemoryNode) -> bool:
        return node.type not in TECHNICAL_NODE_TYPES

    def _retrieve(self, query: MemoryQuery, *, profile: PerformanceLogger | None) -> MemorySubgraph:
        with (profile.span("retrieval.extract") if profile else nullcontext()):
            extraction = self.extractor.extract(query.text)
        seed_scores: OrderedDict[str, float] = OrderedDict()
        match_metrics: dict[str, NodeMatchMetrics] = {}
        with (profile.span("retrieval.tokenize") if profile else nullcontext()):
            query_profile = self._query_profile(query.text)
        query_scopes = self._normalize_query_context_scopes(query.context_scopes)
        code_context = self._is_explicit_code_context(query.node_types) or bool(query_scopes and query_scopes <= {"code", "test"})
        traversal_edge_types = query.edge_types if query.edge_types is not None else (CODE_CONTEXT_EDGE_TYPES if code_context else DEFAULT_CONTEXT_EDGE_TYPES)
        lexical_node_types = query.node_types
        if code_context and lexical_node_types is None:
            lexical_node_types = tuple(sorted(CODE_CONTEXT_NODE_TYPES))

        # 1) canonical topic/entity matches.
        with (profile.span("retrieval.canonical_seed", topics=len(extraction.topics), entities=len(extraction.entities)) if profile else nullcontext()):
            for topic, score in extraction.topics:
                node = self.store.get_node_by_key("Topic", canonicalize(topic))
                if node:
                    if query_scopes and not self._node_matches_query_context_scope(node, query_scopes):
                        continue
                    seed_scores[node.id] = max(
                        seed_scores.get(node.id, 0.0),
                        0.55 + 0.40 * score,
                        self._direct_relevance_score(node, query.text, query_tokens=query_profile.tokens),
                    )
            for entity, _, score in extraction.entities:
                node = self.store.get_node_by_key("Entity", canonicalize(entity))
                if node:
                    if query_scopes and not self._node_matches_query_context_scope(node, query_scopes):
                        continue
                    seed_scores[node.id] = max(
                        seed_scores.get(node.id, 0.0),
                        0.60 + 0.35 * score,
                        self._direct_relevance_score(node, query.text, query_tokens=query_profile.tokens),
                    )

        # 2) lexical search across the graph.
        lexical_limit = max(query.top_k * 3, 30)
        with (profile.span("retrieval.lexical_search", top_k=lexical_limit) if profile else nullcontext()):
            lexical_matches = (
                self._scoped_lexical_search(
                    query,
                    query_profile,
                    lexical_node_types=lexical_node_types,
                    scopes=query_scopes,
                    top_k=lexical_limit,
                    metrics_cache=match_metrics,
                )
                if query_scopes
                else self.store.lexical_search(
                    query.text,
                    top_k=max(query.top_k * 3, 30),
                    node_types=lexical_node_types,
                    include_archived=query.include_archived,
                )
            )
            for node, score in lexical_matches:
                if node.type in TECHNICAL_NODE_TYPES:
                    continue
                if code_context and not self._is_code_context_node(node):
                    continue
                if query_scopes and not self._node_matches_query_context_scope(node, query_scopes):
                    continue
                metrics = match_metrics.get(node.id)
                if metrics is None:
                    metrics = self._node_match_metrics(node, query_profile)
                    match_metrics[node.id] = metrics
                if self._is_weak_multiterm_match(
                    node,
                    query_tokens=query_profile.informative_tokens,
                    direct_relevance=metrics.match_score,
                    overlap_count=metrics.overlap_count,
                    has_strong_identifier_overlap=metrics.strong_identifier_overlap,
                ):
                    continue
                adjusted_score = max(score, metrics.match_score)
                seed_scores[node.id] = max(seed_scores.get(node.id, 0.0), adjusted_score)

        sorted_seed_scores = sorted(seed_scores.items(), key=lambda item: item[1], reverse=True)
        seed_node_ids = self._pick_seed_node_ids(sorted_seed_scores, max_k=max(query.top_k * 2, 20), gap_ratio=0.20)
        with (profile.span("retrieval.expand", seed_nodes=len(seed_node_ids), max_depth=query.max_depth) if profile else nullcontext()):
            candidates, candidate_edges = self._expand_and_rank_candidates(
                seed_node_ids,
                seed_scores,
                query,
                query_profile,
                edge_types=traversal_edge_types,
                code_context=code_context,
                metrics_cache=match_metrics,
            )

        ranked: list[RankedNode] = []
        with (profile.span("retrieval.rank", candidate_nodes=len(candidates)) if profile else nullcontext()):
            for candidate in candidates.values():
                if candidate.score <= 0.0 and candidate.match_score <= 0.0:
                    continue
                ranked.append(
                    RankedNode(
                        node=candidate.node,
                        score=candidate.score,
                        reasons={
                            "match_score": candidate.match_score,
                            "coverage": candidate.coverage,
                            "path_score": candidate.path_score,
                            "type_bonus": candidate.type_bonus,
                            "seed_score": candidate.seed_score,
                            "depth_penalty": candidate.depth_penalty,
                        },
                    )
                )
        ranked.sort(
            key=lambda item: (
                -item.score,
                -float(item.reasons.get("match_score", 0.0)),
                -float(item.reasons.get("coverage", 0.0)),
                -float(item.reasons.get("type_bonus", 0.0)),
                -item.node.salience,
                self._node_relative_path(item.node) or "",
                self._node_label(item.node),
            )
        )
        ranked = ranked[: query.top_k]
        if query.store_trace:
            self.store.record_usage_event(
                query.text,
                [
                    {
                        "id": item.node.id,
                        "score": item.score,
                        "activation": item.reasons.get("path_score", 0.0),
                    }
                    for item in ranked
                    if item.node.type not in TECHNICAL_NODE_TYPES
                ],
            )

        # Context expansion: include immediate evidence/rationale/control edges.
        node_ids = [item.node.id for item in ranked]
        context_edges: OrderedDict[str, MemoryEdge] = OrderedDict()
        context_nodes: OrderedDict[str, MemoryNode] = OrderedDict((item.node.id, item.node) for item in ranked)
        expansion_edge_types = CODE_CONTEXT_EDGE_TYPES if code_context else {
            "SUPPORTS",
            "SUPERSEDES",
            "APPLIES_TO",
            "OVERRIDES",
            "ABOUT",
            "MENTIONS",
            "HAS_TOPIC",
            "PART_OF",
            "IS_A",
            "LIKES",
            "RELATED_TO",
            "DERIVED_FROM",
            "SUPPORTS",
            "SYNTHESIZES",
            "PROMOTED_TO",
            "EXPRESSES",
            "EXPLAINS",
            "EVIDENCED_BY",
            "UPDATED_BY",
            "TRACKS",
        }
        for edge in candidate_edges.values():
            if edge.type in TECHNICAL_EDGE_TYPES:
                continue
            if edge.type in expansion_edge_types and edge.from_id in context_nodes and edge.to_id in context_nodes:
                context_edges[edge.id] = edge
        for node_id in node_ids:
            neighbors = self.store.neighbors(
                node_id,

                direction="both",
                edge_types=traversal_edge_types,
                min_weight=0.25,
                limit=80,
            )
            for edge, neighbor in neighbors:
                if edge.type in TECHNICAL_EDGE_TYPES or neighbor.type in TECHNICAL_NODE_TYPES:
                    continue
                if code_context and not self._is_code_context_node(neighbor):
                    continue
                if query_scopes and not self._node_matches_query_context_scope(neighbor, query_scopes):
                    continue
                if not query.include_archived and neighbor.status in INACTIVE_STATUSES:
                    continue
                if edge.type in expansion_edge_types:
                    context_edges[edge.id] = edge
                    context_nodes.setdefault(neighbor.id, neighbor)

        trace_id: str | None = None
        if query.store_trace:
            trace_id = stable_id("retrieval", None, query.text, utcnow_iso())

        return MemorySubgraph(
            query=query,
            ranked_nodes=ranked,
            nodes=list(context_nodes.values()),
            edges=list(context_edges.values()),
            seed_node_ids=seed_node_ids,
            trace_id=trace_id,
        )

