"""Application service shared by Python, CLI, and MCP query-context adapters."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from ..domain.query_context import (
    CONTEXT_RESULT_SCHEMA_VERSION,
    Confidence,
    ContextResult,
    QueryContextRequest,
)
from .retrieval import RetrievalEngine


class QueryContextService:
    """Own query-context retrieval, projection, metadata, and rendering."""

    def __init__(self, retrieval: RetrievalEngine) -> None:
        self.retrieval = retrieval

    def execute(self, request: QueryContextRequest) -> ContextResult:
        query = MemoryQuery(
            text=request.text,
            top_k=request.budget.top_k,
            max_depth=request.budget.max_depth,
            include_archived=request.include_archived,
            context_scopes=set(request.scopes) or None,
        )
        subgraph = self.retrieval.retrieve(query)
        payload = dict(
            self.retrieval.query_context_payload(
                subgraph,
                max_items=request.budget.max_items,
                query_mode=request.mode.value,
                query_scopes=tuple(sorted(request.scopes)),
            )
        )
        confidence_payload = payload.pop("confidence", None)
        if not isinstance(confidence_payload, dict):
            raise ValueError("query_context projection did not return confidence metadata")
        payload.update(
            {
                "trace_id": subgraph.trace_id,
                "ranked_nodes": len(subgraph.ranked_nodes),
                "seed_node_ids": list(subgraph.seed_node_ids),
            }
        )
        return ContextResult(
            schema_version=CONTEXT_RESULT_SCHEMA_VERSION,
            graph_revision=_context_graph_revision(subgraph),
            confidence=Confidence.from_payload(confidence_payload),
            payload=payload,
        )

    def render(self, result: ContextResult) -> str:
        payload = dict(result.payload)
        payload["confidence"] = result.confidence.to_dict()
        return self.retrieval.render_context_payload(payload)


def _context_graph_revision(subgraph: MemorySubgraph) -> str:
    payload = {
        "nodes": [_node_revision_payload(node) for node in sorted(subgraph.nodes, key=lambda item: item.id)],
        "edges": [_edge_revision_payload(edge) for edge in sorted(subgraph.edges, key=lambda item: item.id)],
        "ranking": [_ranked_revision_payload(item) for item in sorted(subgraph.ranked_nodes, key=lambda item: item.node.id)],
    }
    encoded = json.dumps(_canonical_json_value(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _node_revision_payload(node: MemoryNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "label": node.label,
        "text": node.text,
        "canonical_key": node.canonical_key,
        "properties": node.properties,
        "base_activation": node.base_activation,
        "salience": node.salience,
        "confidence": node.confidence,
        "stability": node.stability,
        "volatility": node.volatility,
        "utility": node.utility,
        "status": node.status,
        "updated_at": node.updated_at,
    }


def _edge_revision_payload(edge: MemoryEdge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "type": edge.type,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "polarity": edge.polarity,
        "origin": edge.origin,
        "properties": edge.properties,
        "updated_at": edge.updated_at,
    }


def _ranked_revision_payload(item: RankedNode) -> dict[str, Any]:
    return {
        "node_id": item.node.id,
        "score": item.score,
        "reasons": item.reasons,
    }


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_json_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True, default=str))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
