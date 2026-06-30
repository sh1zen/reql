"""AST types for REQL."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Scalar = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class SortSpec:
    field: str
    descending: bool = True


@dataclass(frozen=True, slots=True)
class ReturnSpec:
    fields: tuple[str, ...] = ()

    @property
    def is_default(self) -> bool:
        return not self.fields

    @property
    def is_all(self) -> bool:
        return self.fields == ("*",)


@dataclass(frozen=True, slots=True)
class Condition:
    """Base class for boolean filters."""


@dataclass(frozen=True, slots=True)
class Comparison(Condition):
    field: str
    operator: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class BooleanCondition(Condition):
    operator: Literal["AND", "OR"]
    left: Condition
    right: Condition


@dataclass(frozen=True, slots=True)
class NotCondition(Condition):
    condition: Condition


@dataclass(frozen=True, slots=True)
class FindNodes:
    node_types: tuple[str, ...] = ()
    where: Condition | None = None
    returns: ReturnSpec = field(default_factory=ReturnSpec)
    order_by: SortSpec | None = None
    limit: int = 50
    include_archived: bool = False


@dataclass(frozen=True, slots=True)
class FindEdges:
    edge_types: tuple[str, ...] = ()
    where: Condition | None = None
    returns: ReturnSpec = field(default_factory=ReturnSpec)
    order_by: SortSpec | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class Search:
    text: str
    node_types: tuple[str, ...] = ()
    returns: ReturnSpec = field(default_factory=ReturnSpec)
    top_k: int = 20
    max_depth: int = 3
    include_archived: bool = False
    context: bool = False


@dataclass(frozen=True, slots=True)
class Retrieve:
    text: str
    node_types: tuple[str, ...] = ()
    returns: ReturnSpec = field(default_factory=ReturnSpec)
    top_k: int = 12
    max_depth: int = 2
    limit: int = 12
    include_sources: bool = True
    include_archived: bool = False
    filter_generic: bool = True
    max_text_chars: int = 600


@dataclass(frozen=True, slots=True)
class Activate:
    node_ids: tuple[str, ...]
    returns: ReturnSpec = field(default_factory=ReturnSpec)
    max_depth: int = 3
    min_activation: float = 0.03
    limit: int = 50


@dataclass(frozen=True, slots=True)
class PatternNode:
    alias: str
    type_: str | None = None


@dataclass(frozen=True, slots=True)
class PatternEdge:
    alias: str
    types: tuple[str, ...] = ()
    direction: Literal["out", "in", "both"] = "out"


@dataclass(frozen=True, slots=True)
class Match:
    left: PatternNode
    edge: PatternEdge
    right: PatternNode
    where: Condition | None = None
    returns: ReturnSpec = field(default_factory=ReturnSpec)
    order_by: SortSpec | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class NodeSelector:
    mode: Literal["id", "key", "text"]
    value: str
    type_: str | None = None


@dataclass(frozen=True, slots=True)
class PathQuery:
    start: NodeSelector
    end: NodeSelector
    edge_types: tuple[str, ...] = ()
    returns: ReturnSpec = field(default_factory=ReturnSpec)
    max_depth: int = 4
    limit: int = 10


@dataclass(frozen=True, slots=True)
class Explain:
    mode: Literal["node", "search", "hub"]
    target: str
    top_k: int = 10
    max_depth: int = 2
    limit: int = 30


@dataclass(frozen=True, slots=True)
class Stats:
    group_by: tuple[str, ...] = ()
    node_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Communities:
    where: Condition | None = None
    order_by: SortSpec | None = None
    limit: int = 20


@dataclass(frozen=True, slots=True)
class Hubs:
    node_types: tuple[str, ...] = ()
    where: Condition | None = None
    order_by: SortSpec | None = None
    limit: int = 20


@dataclass(frozen=True, slots=True)
class TypedNodeList:
    command: Literal["PROJECTS", "ARTIFACTS", "FRAGMENTS", "SYMBOLS", "FINDINGS", "DELTAS"]
    node_types: tuple[str, ...]
    where: Condition | None = None
    returns: ReturnSpec = field(default_factory=ReturnSpec)
    order_by: SortSpec | None = None
    limit: int = 20


@dataclass(frozen=True, slots=True)
class CacheStatus:
    limit: int = 20


@dataclass(frozen=True, slots=True)
class VerifyFinding:
    finding_id: str


Statement = FindNodes | FindEdges | Search | Retrieve | Activate | Match | PathQuery | Explain | Stats | Communities | Hubs | TypedNodeList | CacheStatus | VerifyFinding
