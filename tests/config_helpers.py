from __future__ import annotations

from pathlib import Path

from api import MemoryGraph
from memory.config import default_config, merge_config


def open_graph_with_documents(path: str | Path) -> MemoryGraph:
    """Open a graph with every supported document format enabled."""

    config = default_config()
    config = merge_config(
        config,
        {"compile": {"documents": dict.fromkeys(config.compile.documents, True)}},
    )
    return MemoryGraph.open(path, config=config)
