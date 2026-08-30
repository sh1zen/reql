"""Data models and structural interfaces for the retrieval context pipeline."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import MutableMapping, Protocol, Sequence

from ....domain.query_context import ContextPayload
from ....domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph
from ....storage.graph_store import GraphStore


@dataclass(slots=True)
class QueryProfile:
    text: str
    canonical: str
    tokens: set[str]
    informative_tokens: set[str]
    ordered_tokens: tuple[str, ...]
    phrase_terms: set[str]


@dataclass(frozen=True, slots=True)
class NodeMatchMetrics:
    match_score: float
    coverage: float
    overlap_tokens: frozenset[str] = frozenset()
    strong_identifier_overlap: bool = False

    @property
    def overlap_count(self) -> int:
        return len(self.overlap_tokens)


@dataclass(slots=True)
class PathCandidate:
    node: MemoryNode
    score: float
    match_score: float
    coverage: float
    path_score: float
    type_bonus: float
    seed_score: float
    depth_penalty: float
    edge_ids: list[str] = field(default_factory=list)


class SearchComponent(Protocol):
    store: GraphStore

    def _query_profile(self, query_text: str) -> QueryProfile: ...

    def _pick_seed_node_ids(
        self,
        scored: list[tuple[str, float]],
        *,
        max_k: int,
        gap_ratio: float = 0.20,
    ) -> list[str]: ...


class ExpansionComponent(Protocol):
    def _expand_and_rank_candidates(
        self,
        seed_node_ids: list[str],
        seed_scores: OrderedDict[str, float],
        query: MemoryQuery,
        query_profile: QueryProfile,
        *,
        edge_types: set[str],
        code_context: bool,
        metrics_cache: MutableMapping[str, NodeMatchMetrics] | None = None,
    ) -> tuple[OrderedDict[str, PathCandidate], OrderedDict[str, MemoryEdge]]: ...


class ContextServiceComponent(Protocol):
    def query_context_payload(
        self,
        subgraph: MemorySubgraph,
        *,
        max_items: int = 20,
        query_mode: str = "informative",
        query_scopes: Sequence[str] | None = None,
    ) -> ContextPayload: ...

    def compose_context(
        self,
        subgraph: MemorySubgraph,
        *,
        max_items: int = 20,
        query_mode: str = "informative",
        query_scopes: Sequence[str] | None = None,
    ) -> str: ...


class ContextRendererComponent(Protocol):
    def _render_query_context_payload(self, payload: ContextPayload) -> str: ...
