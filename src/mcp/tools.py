"""Pure MCP tool handlers for REQL.

This module is deliberately independent from any MCP transport or SDK. It
accepts plain dictionaries and returns JSON-serializable dictionaries, so unit
tests and lightweight integrations can exercise the same behavior as the stdio
server.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from memory.config import ConfigError, REQLConfig, load_effective_config
from memory.domain.models import MemoryQuery
from memory.domain.exceptions import REQLError
from memory.security import SecurityError, sanitize_agent_text, validate_mcp_path
from api.memory_graph import MemoryGraph

MAX_TOP_K = 50
MAX_DEPTH = 5
MAX_ITEMS = 50
MAX_QUERY_ROWS = 200
MAX_MEMORY_TEXT_CHARS = 2000
MAX_CONTEXT_NODES = 200
MAX_CONTEXT_EDGES = 400
MUTATING_REQL_COMMANDS = {"COMMUNITIES", "HUBS"}


class MCPToolError(REQLError):
    """Raised when an MCP tool request is invalid or cannot be completed."""


ToolHandler = Callable[..., dict[str, Any]]


READ_ONLY_TOOLS = {
    "inspect_node",
    "query_context",
    "query_explore",
    "query_graph",
    "query_memories",
    "reql_query",
    "reql_project_status",
}

WRITE_TOOLS = {
    "reql_compile_project",
    "reql_hubs",
    "reql_watch_project",
}


def query_graph(
    *,
    storage_path: str,
    query: str,
    top_k: int = 12,
    max_depth: int = 2,
    max_nodes: int = 80,
    max_edges: int = 160,
    max_sources: int = 20,
    max_items: int = 20,
    filter_generic: bool = True,
    include_archived: bool = False,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured query-centered subgraph with sources and agent context."""
    config = _load_tool_config(config_path, config_overrides)
    query = _required_text(query, "query")
    top_k = _bounded_int(top_k, "top_k", minimum=1, maximum=MAX_TOP_K)
    max_depth = _bounded_int(max_depth, "max_depth", minimum=0, maximum=MAX_DEPTH)
    max_nodes = _bounded_int(max_nodes, "max_nodes", minimum=1, maximum=MAX_CONTEXT_NODES)
    max_edges = _bounded_int(max_edges, "max_edges", minimum=0, maximum=MAX_CONTEXT_EDGES)
    max_sources = _bounded_int(max_sources, "max_sources", minimum=0, maximum=MAX_ITEMS)
    max_items = _bounded_int(max_items, "max_items", minimum=1, maximum=MAX_ITEMS)
    with _open_graph(storage_path, config, read_only=True) as graph:
        return graph.query_graph(
            query,
            top_k=top_k,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_sources=max_sources,
            max_items=max_items,
            filter_generic=bool(filter_generic),
            include_archived=bool(include_archived),
        )


def query_context(
    *,
    storage_path: str,
    query: str,
    mode: str = "informative",
    scopes: list[str] | None = None,
    code: bool = False,
    docs: bool = False,
    test: bool = False,
    top_k: int = 12,
    max_depth: int = 3,
    max_items: int = 12,
    include_risky: bool = False,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded deterministic context block for a task/query."""
    config = _load_tool_config(config_path, config_overrides)
    top_k = _bounded_int(top_k, "top_k", minimum=1, maximum=MAX_TOP_K)
    max_depth = _bounded_int(max_depth, "max_depth", minimum=0, maximum=MAX_DEPTH)
    max_items = _bounded_int(max_items, "max_items", minimum=1, maximum=MAX_ITEMS)
    query = _required_text(query, "query")
    selected_scopes = list(scopes or [])
    for enabled, scope in ((code, "code"), (docs, "docs"), (test, "test")):
        if enabled and scope not in selected_scopes:
            selected_scopes.append(scope)
    with _open_graph(storage_path, config, read_only=True) as graph:
        subgraph = graph.retrieval.retrieve(
            MemoryQuery(
                text=query,
                top_k=top_k,
                max_depth=max_depth,
                context_scopes=set(selected_scopes) if selected_scopes else None,
            )
        )
        payload = graph.retrieval.query_context_payload(
            subgraph,
            max_items=max_items,
            query_mode=mode,
            query_scopes=selected_scopes,
            include_risky=bool(include_risky),
        )
        payload.update(
            {
                "trace_id": subgraph.trace_id,
                "ranked_nodes": len(subgraph.ranked_nodes),
                "seed_node_ids": list(subgraph.seed_node_ids),
            }
        )
        return payload


def query_explore(
    *,
    storage_path: str,
    query: str,
    views: list[str] | None = None,
    top_k: int = 12,
    max_depth: int = 3,
    limit: int = 12,
    max_items: int = 18,
    include_archived: bool = False,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return dependency-oriented query slices for coding agents."""
    config = _load_tool_config(config_path, config_overrides)
    query = _required_text(query, "query")
    views = _optional_string_list(views, "views")
    top_k = _bounded_int(top_k, "top_k", minimum=1, maximum=MAX_TOP_K)
    max_depth = _bounded_int(max_depth, "max_depth", minimum=0, maximum=MAX_DEPTH)
    limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_ITEMS)
    max_items = _bounded_int(max_items, "max_items", minimum=1, maximum=MAX_ITEMS)
    with _open_graph(storage_path, config, read_only=True) as graph:
        return graph.query_explore(
            query,
            views=views,
            top_k=top_k,
            max_depth=max_depth,
            limit=limit,
            max_items=max_items,
            include_archived=bool(include_archived),
        )


def query_memories(
    *,
    storage_path: str,
    query: str,
    top_k: int = 12,
    max_depth: int = 2,
    limit: int = 12,
    include_sources: bool = True,
    filter_generic: bool = True,
    max_text_chars: int = 600,
    include_archived: bool = False,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a compact list of relevant memory/source texts for an agent query."""
    config = _load_tool_config(config_path, config_overrides)
    query = _required_text(query, "query")
    top_k = _bounded_int(top_k, "top_k", minimum=1, maximum=MAX_TOP_K)
    max_depth = _bounded_int(max_depth, "max_depth", minimum=0, maximum=MAX_DEPTH)
    limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_ITEMS)
    max_text_chars = _bounded_int(max_text_chars, "max_text_chars", minimum=80, maximum=MAX_MEMORY_TEXT_CHARS)
    with _open_graph(storage_path, config, read_only=True) as graph:
        return graph.query_memories_payload(
            query,
            top_k=top_k,
            max_depth=max_depth,
            limit=limit,
            include_sources=bool(include_sources),
            filter_generic=bool(filter_generic),
            max_text_chars=max_text_chars,
            include_archived=bool(include_archived),
        )


def inspect_node(
    *,
    storage_path: str,
    node_id: str,
    limit: int = 30,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a node id to its node payload, local edges, and source locations."""
    config = _load_tool_config(config_path, config_overrides)
    node_id = _required_text(node_id, "node_id")
    limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_ITEMS)
    with _open_graph(storage_path, config, read_only=True) as graph:
        result = graph.inspect_node(node_id, limit=limit)
    if not result["found"]:
        raise MCPToolError(f"Node not found: {node_id}")
    return result


def reql_query(
    *,
    storage_path: str,
    statement: str,
    limit: int | None = None,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a REQL statement and return bounded tabular results."""
    config = _load_tool_config(config_path, config_overrides)
    statement = _required_text(statement, "statement")
    if _reql_statement_requires_write(statement):
        raise MCPToolError("This REQL statement persists analysis results; use an approved write/update tool instead")
    output_limit = _bounded_optional_int(limit, "limit", minimum=0, maximum=MAX_QUERY_ROWS)
    if output_limit is None:
        output_limit = MAX_QUERY_ROWS
    with _open_graph(storage_path, config, read_only=True) as graph:
        result = graph.query(statement)
        rows = [_jsonable(row) for row in result.rows[:output_limit]]
        original_count = len(result.rows)
        return {
            "columns": list(result.columns),
            "rows": rows,
            "metadata": {
                "statement": result.statement,
                "command": result.command,
                "diagnostics": _jsonable(result.diagnostics),
                "row_count": original_count,
                "rows_returned": len(rows),
                "output_limit": output_limit,
                "output_limited": original_count > len(rows),
                "limit_applied": limit is not None,
            },
        }


def reql_project_status(
    *,
    storage_path: str,
    path: str,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return registered project/cache status for a path."""
    path = str(validate_mcp_path(_required_path_text(path, "path"), name="path"))
    config = _load_tool_config(config_path, config_overrides, start_dir=path)
    with _open_graph(storage_path, config, read_only=True) as graph:
        status = graph.project_status(path)
        if status is None:
            return {"found": False, "path": path}
        payload = _jsonable(status)
        payload["found"] = True
        return payload


def reql_hubs(
    *,
    storage_path: str,
    project_id: str | None = None,
    limit: int = 10,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded list of high-value hub nodes."""
    config = _load_tool_config(config_path, config_overrides)
    if not config.analysis.enable_hubs:
        raise MCPToolError("Hub analysis is disabled by configuration")
    limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_TOP_K)
    with _open_graph(storage_path, config) as graph:
        report = graph.analyze_hubs(project_id=project_id, limit=limit)
        hubs = [
            {
                "id": hub.node_id,
                "type": hub.node_type,
                "label": hub.label,
                "score": hub.hub_score,
                "reasons": list(hub.reasons),
                "metadata": {
                    "rank": hub.hub_rank,
                    "is_hub": hub.is_hub,
                    "centrality_score": hub.centrality_score,
                    "specificity_score": hub.specificity_score,
                    "community_bridge_score": hub.community_bridge_score,
                    "generic_penalty": hub.generic_penalty,
                },
            }
            for hub in report.hubs[:limit]
        ]
        return {"hubs": _jsonable(hubs), "warnings": list(report.warnings)}


def reql_compile_project(
    *,
    storage_path: str,
    path: str,
    cache_enabled: bool | None = None,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a project into the graph. Intended for client-side approval."""
    path = str(validate_mcp_path(_required_path_text(path, "path"), name="path"))
    config = _load_tool_config(config_path, config_overrides, start_dir=path)
    with _open_graph(storage_path, config) as graph:
        result = graph.compile_project(
            path,
            max_file_size_bytes=config.scan.max_file_size_bytes,
            include_patterns=config.scan.include,
            exclude_patterns=config.scan.exclude,
            cache_enabled=config.cache.enabled if cache_enabled is None else bool(cache_enabled),
            parsing_options=_parsing_options(config),
        )
        run = result.run
        return {
            "summary": {
                "project_id": result.scan.project.id,
                "run_id": run.id,
                "status": run.status,
                "created": {"nodes": run.nodes_created, "edges": run.edges_created},
                "updated": {"nodes": run.nodes_updated, "edges": run.edges_updated},
                "archived": {
                    "nodes": len(result.delta.archived_nodes),
                    "edges": len(result.delta.archived_edges),
                    "files_deleted": run.files_deleted,
                },
                "errors": list(run.errors),
                "delta_id": result.delta.id,
                "files_seen": run.files_seen,
                "files_changed": run.files_changed,
                "files_skipped": run.files_skipped,
            }
        }


def reql_watch_project(
    *,
    storage_path: str,
    path: str,
    cache_enabled: bool | None = None,
    interval_seconds: float = 0.0,
    debounce_seconds: float = 0.0,
    max_iterations: int = 1,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Poll a project for changes and compile dirty artifacts into the graph."""
    path = str(validate_mcp_path(_required_path_text(path, "path"), name="path"))
    config = _load_tool_config(config_path, config_overrides, start_dir=path)
    iterations = _bounded_int(max_iterations, "max_iterations", minimum=1, maximum=100)
    interval = _bounded_float(interval_seconds, "interval_seconds", minimum=0.0, maximum=3600.0)
    debounce = _bounded_float(debounce_seconds, "debounce_seconds", minimum=0.0, maximum=60.0)
    with _open_graph(storage_path, config) as graph:
        events = [
            _watch_event_payload(event)
            for event in graph.watch_project(
                path,
                max_file_size_bytes=config.scan.max_file_size_bytes,
                include_patterns=config.scan.include,
                exclude_patterns=config.scan.exclude,
                cache_enabled=config.cache.enabled if cache_enabled is None else bool(cache_enabled),
                parsing_options=_parsing_options(config),
                interval_seconds=interval,
                debounce_seconds=debounce,
                max_iterations=iterations,
            )
        ]
        compiled = [event for event in events if event["compiled"]]
        errors = [error for event in events for error in event["errors"]]
        return {
            "summary": {
                "path": path,
                "polls": len(events),
                "compiled_polls": len(compiled),
                "errors": errors,
            },
            "events": events,
        }


def _watch_event_payload(event: Any) -> dict[str, Any]:
    result = event.result
    payload: dict[str, Any] = {
        "iteration": event.iteration,
        "checked_at": event.checked_at,
        "project_path": event.project_path,
        "total_artifacts": event.total_artifacts,
        "dirty_artifacts": event.dirty_artifacts,
        "deleted_artifacts": event.deleted_artifacts,
        "compiled": event.compiled,
        "errors": list(event.errors),
        "result": None,
    }
    if result is not None:
        run = result.run
        payload["result"] = {
            "project_id": result.scan.project.id,
            "run_id": run.id,
            "status": run.status,
            "delta_id": result.delta.id,
            "files_seen": run.files_seen,
            "files_changed": run.files_changed,
            "files_skipped": run.files_skipped,
            "files_deleted": run.files_deleted,
            "created": {"nodes": run.nodes_created, "edges": run.edges_created},
            "updated": {"nodes": run.nodes_updated, "edges": run.edges_updated},
            "archived": {
                "nodes": len(result.delta.archived_nodes),
                "edges": len(result.delta.archived_edges),
            },
        }
    return payload


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "inspect_node": inspect_node,
    "query_graph": query_graph,
    "query_context": query_context,
    "query_explore": query_explore,
    "query_memories": query_memories,
    "reql_query": reql_query,
    "reql_project_status": reql_project_status,
    "reql_hubs": reql_hubs,
    "reql_compile_project": reql_compile_project,
    "reql_watch_project": reql_watch_project,
}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "inspect_node",
        "description": "Read-only: resolve a REQL node id to its node payload, adjacent edges, neighbors, and source/location hints.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "node_id"],
            "properties": {
                "storage_path": {"type": "string"},
                "node_id": {"type": "string"},
                "limit": {"type": "integer", "default": 30, "maximum": MAX_ITEMS},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "query_context",
        "description": "Read-only: retrieve a compact deterministic context block from REQL.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "query"],
            "properties": {
                "storage_path": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 12, "maximum": MAX_TOP_K},
                "max_depth": {"type": "integer", "default": 3, "maximum": MAX_DEPTH},
                "max_items": {"type": "integer", "default": 12, "maximum": MAX_ITEMS},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "query_graph",
        "description": "Read-only: retrieve a structured query-centered REQL subgraph with seed nodes, edges, sources, and compact agent context.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "query"],
            "properties": {
                "storage_path": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 12, "maximum": MAX_TOP_K},
                "max_depth": {"type": "integer", "default": 2, "maximum": MAX_DEPTH},
                "max_nodes": {"type": "integer", "default": 80, "maximum": MAX_CONTEXT_NODES},
                "max_edges": {"type": "integer", "default": 160, "maximum": MAX_CONTEXT_EDGES},
                "max_sources": {"type": "integer", "default": 20, "maximum": MAX_ITEMS},
                "max_items": {"type": "integer", "default": 18, "maximum": MAX_ITEMS},
                "filter_generic": {"type": "boolean", "default": True},
                "include_archived": {"type": "boolean", "default": False},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "query_explore",
        "description": "Read-only: retrieve dependency-oriented REQL slices for coding agents: owners, callers, public surface, serialization paths, docs mentions, and code.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "query"],
            "properties": {
                "storage_path": {"type": "string"},
                "query": {"type": "string"},
                "views": {
                    "type": ["array", "null"],
                    "default": None,
                    "items": {"type": "string", "enum": ["all", "owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "code"]},
                },
                "top_k": {"type": "integer", "default": 12, "maximum": MAX_TOP_K},
                "max_depth": {"type": "integer", "default": 3, "maximum": MAX_DEPTH},
                "limit": {"type": "integer", "default": 12, "maximum": MAX_ITEMS},
                "max_items": {"type": "integer", "default": 18, "maximum": MAX_ITEMS},
                "include_archived": {"type": "boolean", "default": False},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "query_memories",
        "description": "Read-only: return a compact ranked list of relevant REQL memory/source texts for clients that do not need graph debugging details.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "query"],
            "properties": {
                "storage_path": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 12, "maximum": MAX_TOP_K},
                "max_depth": {"type": "integer", "default": 2, "maximum": MAX_DEPTH},
                "limit": {"type": "integer", "default": 12, "maximum": MAX_ITEMS},
                "include_sources": {"type": "boolean", "default": True},
                "filter_generic": {"type": "boolean", "default": True},
                "max_text_chars": {"type": "integer", "default": 600, "maximum": MAX_MEMORY_TEXT_CHARS},
                "include_archived": {"type": "boolean", "default": False},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_query",
        "description": "Read-only: execute a REQL statement and return bounded rows.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "statement"],
            "properties": {
                "storage_path": {"type": "string"},
                "statement": {"type": "string"},
                "limit": {"type": ["integer", "null"], "default": None, "maximum": MAX_QUERY_ROWS},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_project_status",
        "description": "Read-only: inspect registered project and cache status for a path.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "path"],
            "properties": {
                "storage_path": {"type": "string"},
                "path": {"type": "string"},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_hubs",
        "description": "Write/update: analyze graph hubs and persist hub scores. Use with client-side approval.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path"],
            "properties": {
                "storage_path": {"type": "string"},
                "project_id": {"type": ["string", "null"], "default": None},
                "limit": {"type": "integer", "default": 10, "maximum": MAX_TOP_K},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_compile_project",
        "description": "Write/update: scan and incrementally compile a project into the graph. Use with client-side approval.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "path"],
            "properties": {
                "storage_path": {"type": "string"},
                "path": {"type": "string"},
                "cache_enabled": {"type": ["boolean", "null"], "default": None},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_watch_project",
        "description": "Write/update: monitor a project with watchdog and incrementally compile dirty artifacts. Defaults to one bounded check for MCP clients.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "path"],
            "properties": {
                "storage_path": {"type": "string"},
                "path": {"type": "string"},
                "cache_enabled": {"type": ["boolean", "null"], "default": None},
                "interval_seconds": {"type": "number", "default": 0.0, "maximum": 3600.0},
                "debounce_seconds": {"type": "number", "default": 0.0, "maximum": 60.0},
                "max_iterations": {"type": "integer", "default": 1, "maximum": 100},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
]


def list_tools(*, include_write: bool = True) -> list[dict[str, Any]]:
    """Return MCP tool descriptors."""
    if include_write:
        return list(TOOL_SCHEMAS)
    return [schema for schema in TOOL_SCHEMAS if schema["name"] in READ_ONLY_TOOLS]


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch a tool call to a pure handler."""
    if name not in TOOL_HANDLERS:
        raise MCPToolError(f"Unknown REQL MCP tool: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise MCPToolError("Tool arguments must be an object")
    try:
        return _agent_safe_jsonable(_jsonable(TOOL_HANDLERS[name](**arguments)))
    except TypeError as exc:
        raise MCPToolError(f"Invalid arguments for {name}: {exc}") from exc
    except SecurityError as exc:
        raise MCPToolError(str(exc)) from exc


def _parsing_options(config: REQLConfig) -> dict[str, object]:
    return {"compile": config.compile.to_dict()}


def _load_tool_config(
    config_path: str | None,
    config_overrides: dict[str, Any] | None,
    *,
    start_dir: str | Path | None = None,
) -> REQLConfig:
    if config_overrides is not None and not isinstance(config_overrides, dict):
        raise MCPToolError("config_overrides must be an object when provided")
    if config_path is not None:
        config_path = str(validate_mcp_path(config_path, name="config_path", must_exist=True))
    try:
        return load_effective_config(config_path, start_dir=start_dir, overrides=config_overrides or None)
    except ConfigError as exc:
        raise MCPToolError(str(exc)) from exc


def _graph_scope(config: REQLConfig) -> str:
    return "default"


def _reql_statement_requires_write(statement: str) -> bool:
    first = statement.strip().split(None, 1)[0].rstrip(";").upper() if statement.strip() else ""
    return first in MUTATING_REQL_COMMANDS


@contextmanager
def _open_graph(storage_path: str, config: REQLConfig, *, read_only: bool = False) -> Iterator[MemoryGraph]:
    storage_path = str(validate_mcp_path(_required_path_text(storage_path, "storage_path"), name="storage_path"))
    graph = MemoryGraph.open(Path(storage_path), config=config, read_only=read_only)
    try:
        yield graph
    finally:
        graph.close()


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MCPToolError(f"{name} must be a non-empty string")
    return value


def _required_path_text(value: Any, name: str) -> str:
    text = _required_text(value, name)
    if "\x00" in text:
        raise MCPToolError(f"{name} cannot contain NUL bytes")
    return text


def _optional_string_list(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise MCPToolError(f"{name} must be an array of strings when provided")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MCPToolError(f"{name} must contain only non-empty strings")
        result.append(item)
    return result


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise MCPToolError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MCPToolError(f"{name} must be an integer") from exc
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _bounded_optional_int(value: Any, name: str, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return _bounded_int(value, name, minimum=minimum, maximum=maximum)


def _bounded_float(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise MCPToolError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MCPToolError(f"{name} must be a number") from exc
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    return str(value)


def _agent_safe_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_agent_text(value)
    if isinstance(value, dict):
        return {str(key): _agent_safe_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_agent_safe_jsonable(item) for item in value]
    return value

