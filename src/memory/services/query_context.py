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
    GraphFreshness,
    QueryContextRequest,
    SourceRevision,
)
from ..artifacts.revision import RevisionRepository
from ..freshness import read_watch_state
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
        source_revision, freshness = _source_revision_and_freshness(subgraph, self.retrieval.store)
        return ContextResult(
            schema_version=CONTEXT_RESULT_SCHEMA_VERSION,
            graph_revision=_context_graph_revision(subgraph),
            confidence=Confidence.from_payload(confidence_payload),
            payload=payload,
            source_revision=source_revision,
            freshness=freshness,
        )

    def render(self, result: ContextResult) -> str:
        payload = dict(result.payload)
        payload["confidence"] = result.confidence.to_dict()
        rendered = self.retrieval.render_context_payload(payload)
        source = result.source_revision.id if result.source_revision else "unknown"
        return (
            f"Graph freshness: {result.freshness.status}; source_revision={source}; "
            f"snapshot_used={str(result.freshness.snapshot_used).lower()}\n\n{rendered}"
        )


def _source_revision_and_freshness(subgraph: MemorySubgraph, store: Any) -> tuple[SourceRevision | None, GraphFreshness]:
    project_ids = {
        str(node.properties.get("project_id") or (node.id if node.type == "Project" else ""))
        for node in subgraph.nodes
    }
    project_ids.discard("")
    revisions = RevisionRepository(store)
    latest = next((revision for project_id in sorted(project_ids) if (revision := revisions.latest(project_id))), None)
    source = SourceRevision(latest.id, latest.sequence, latest.tree_hash) if latest else None
    storage_path = getattr(store, "path", None)
    state = read_watch_state(storage_path) if storage_path is not None else {}
    raw_status = str(state.get("status") or "unknown")
    status = raw_status if raw_status in {"current", "refreshing", "stale", "unknown"} else "unknown"
    if status == "current" and latest is not None and state.get("source_revision_id") != latest.id:
        status = "stale"
    return source, GraphFreshness(
        status=status,  # type: ignore[arg-type]
        snapshot_used=bool(getattr(store, "snapshot", False)),
        pending_paths=max(0, int(state.get("pending_paths") or 0)),
        checked_at=str(state.get("checked_at")) if state.get("checked_at") else None,
    )


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
        return sorted([_canonical_json_value(item) for item in value], key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True, default=str))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
