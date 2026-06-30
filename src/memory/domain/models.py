"""Dataclasses and typed payloads for the memory graph."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .ids import new_id
from .timeutils import utcnow_iso

NodeStatus = Literal[
    "candidate",
    "latent",
    "mature",
    "active",
    "reinforced",
    "archived",
    "deleted",
    "rejected",
    "promoted",
    "confirmed",
    "dismissed",
]

EdgeOrigin = Literal[
    "observed",
    "deterministic",
    "statistical",
    "inferred",
    "ambiguous",
    "manual",
]


def copy_memory_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: copy_memory_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_memory_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(copy_memory_payload(item) for item in value)
    if isinstance(value, set):
        return {copy_memory_payload(item) for item in value}
    return value


@dataclass(slots=True)
class MemoryNode:
    """A property-graph node with dynamic memory state."""

    type: str
    id: str = field(default_factory=lambda: new_id("node"))
    label: str | None = None
    text: str | None = None
    canonical_key: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    activation: float = 0.0
    base_activation: float = 0.0
    salience: float = 0.0
    confidence: float = 1.0
    stability: float = 0.5
    volatility: float = 0.5
    utility: float = 0.0

    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    last_activated_at: str | None = None
    last_used_at: str | None = None
    usage_count: int = 0
    evidence_count: int = 0
    status: NodeStatus = "active"

    def to_dict(self, *, copy_properties: bool = True) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "label": self.label,
            "text": self.text,
            "canonical_key": self.canonical_key,
            "properties": copy_memory_payload(self.properties) if copy_properties else self.properties,
            "activation": self.activation,
            "base_activation": self.base_activation,
            "salience": self.salience,
            "confidence": self.confidence,
            "stability": self.stability,
            "volatility": self.volatility,
            "utility": self.utility,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activated_at": self.last_activated_at,
            "last_used_at": self.last_used_at,
            "usage_count": self.usage_count,
            "evidence_count": self.evidence_count,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryNode":
        return cls(**data)


@dataclass(slots=True)
class MemoryEdge:
    """A directed property-graph edge with activation and evidence state."""

    from_id: str
    to_id: str
    type: str
    id: str = field(default_factory=lambda: new_id("edge"))
    weight: float = 1.0
    confidence: float = 1.0
    polarity: int = 1
    origin: EdgeOrigin = "deterministic"
    properties: dict[str, Any] = field(default_factory=dict)
    co_activation_count: int = 0
    last_fired_at: str | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def to_dict(self, *, copy_properties: bool = True) -> dict[str, Any]:
        return {
            "from_id": self.from_id,
            "to_id": self.to_id,
            "type": self.type,
            "id": self.id,
            "weight": self.weight,
            "confidence": self.confidence,
            "polarity": self.polarity,
            "origin": self.origin,
            "properties": copy_memory_payload(self.properties) if copy_properties else self.properties,
            "co_activation_count": self.co_activation_count,
            "last_fired_at": self.last_fired_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEdge":
        return cls(**data)


@dataclass(slots=True)
class MemoryQuery:
    """Retrieval request."""

    text: str
    top_k: int = 20
    max_depth: int = 3
    min_activation: float = 0.03
    include_archived: bool = False
    node_types: set[str] | None = None
    edge_types: set[str] | None = None
    context_scopes: set[str] | None = None
    store_trace: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "top_k": self.top_k,
            "max_depth": self.max_depth,
            "min_activation": self.min_activation,
            "include_archived": self.include_archived,
            "node_types": set(self.node_types) if self.node_types is not None else None,
            "edge_types": set(self.edge_types) if self.edge_types is not None else None,
            "context_scopes": set(self.context_scopes) if self.context_scopes is not None else None,
            "store_trace": self.store_trace,
        }


@dataclass(slots=True)
class RankedNode:
    node: MemoryNode
    score: float
    reasons: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class MemorySubgraph:
    """Result of retrieval/activation."""

    query: MemoryQuery
    ranked_nodes: list[RankedNode]
    nodes: list[MemoryNode]
    edges: list[MemoryEdge]
    seed_node_ids: list[str]
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "ranked_nodes": [
                {"node": item.node.to_dict(), "score": item.score, "reasons": item.reasons}
                for item in self.ranked_nodes
            ],
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "seed_node_ids": self.seed_node_ids,
            "trace_id": self.trace_id,
        }


@dataclass(slots=True)
class ActivationOptions:
    seed_boost: float = 1.0
    max_depth: int = 3
    min_activation: float = 0.03
    depth_decay: float = 0.55
    persistence: float = 0.15
    traverse_incoming: bool = True
    allowed_edge_types: set[str] | None = None
    blocked_edge_types: set[str] | None = None
    update_store: bool = True


@dataclass(slots=True)
class ActivationResult:
    active_nodes: list[MemoryNode]
    fired_edges: list[MemoryEdge]
    activation_by_node: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_nodes": [node.to_dict() for node in self.active_nodes],
            "fired_edges": [edge.to_dict() for edge in self.fired_edges],
            "activation_by_node": dict(self.activation_by_node),
        }


