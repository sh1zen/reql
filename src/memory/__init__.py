"""Core REQL memory runtime package."""
from __future__ import annotations

from .domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph
from .infrastructure.block import BlockGraphStore
from .query import QueryResult, REQLError, REQLEvaluationError, REQLSyntaxError
from .config import ConfigError, REQLConfig, load_config

__version__ = "0.3.0"

__all__ = [
    "MemoryNode",
    "MemoryEdge",
    "MemoryQuery",
    "MemorySubgraph",
    "BlockGraphStore",
    "QueryResult",
    "REQLError",
    "REQLSyntaxError",
    "REQLEvaluationError",
    "REQLConfig",
    "ConfigError",
    "load_config",
]
