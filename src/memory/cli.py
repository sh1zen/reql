"""Command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CONFIG_FILENAME, ConfigError, REQLConfig, load_config, load_effective_config, parse_config_override_assignments, write_sample_config
from .diagnostics import PerformanceLogger
from .domain.exceptions import StorageError
from .storage import BlockGraphStore
from .reporting.html_graph import write_graph_html
from api.memory_graph import MemoryGraph


DEFAULT_STORAGE_DIR = ".reql"
DEFAULT_STORAGE_FILE = "memory.reql"
DANGEROUS_EXCLUDE_PATTERNS = {"*", "**", "**/*", "/**", "/*", "/", ".", "./"}


class _PromptInterrupted(Exception):
    pass


@dataclass(frozen=True)
class _AgentCommandResolution:
    platforms: tuple[str, ...]
    project: bool
    project_dir: Path
    home_dir: Path | None


class _SortedSubparserChoices(dict[str, argparse.ArgumentParser]):
    def __iter__(self):
        return iter(sorted(super().keys()))


def _print_json(payload: object) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")


def _print_compile_result(result: Any) -> None:
    run = result.run
    print(f"Project: {result.scan.project.name}")
    print(f"Run: {run.id}")
    print(f"Status: {run.status}")
    print(f"Files seen: {run.files_seen}")
    print(f"Changed: {run.files_changed}")
    print(f"Skipped: {run.files_skipped}")
    print(f"Deleted: {run.files_deleted}")
    print(f"Nodes: created={run.nodes_created}, updated={run.nodes_updated}")
    print(f"Edges: created={run.edges_created}, updated={run.edges_updated}")
    print(f"Delta: {result.delta.id}")
    if run.errors:
        print("Errors:")
        for error in run.errors:
            print(f"  {error}")


def _available_disk_roots() -> list[str]:
    if os.name == "nt":
        roots = [f"{chr(letter)}:\\" for letter in range(ord("A"), ord("Z") + 1)]
        return [root for root in roots if Path(root).exists()]

    roots = ["/"]
    for mount_parent in (Path("/mnt"), Path("/Volumes")):
        try:
            roots.extend(str(path) for path in mount_parent.iterdir() if path.is_dir())
        except OSError:
            continue
    return list(dict.fromkeys(roots))


def _no_agent_profiles_message() -> str:
    disks = _available_disk_roots()
    disk_text = ", ".join(disks) if disks else "none detected"
    return (
        "No supported coding-agent profiles were detected.\n"
        f"Available disks: {disk_text}\n"
        "Choose a platform and target explicitly, for example: "
        "reql install codex --user"
    )


def _prompt_agent_target(available_platforms: str, *, action: str) -> tuple[list[str], Path] | None:
    if not sys.stdin.isatty():
        return None

    from agents.install import detect_platforms

    disks = _available_disk_roots()
    print("No supported coding-agent profiles were detected.", file=sys.stderr)
    if disks:
        print("Available disks:", file=sys.stderr)
        for index, disk in enumerate(disks, start=1):
            print(f"  {index}. {disk}", file=sys.stderr)
        raw_target = _read_stderr_prompt("Agent profile disk or path: ").strip()
        if not raw_target:
            print("No path selected.", file=sys.stderr)
            return None
        if raw_target.isdigit() and 1 <= int(raw_target) <= len(disks):
            target = Path(disks[int(raw_target) - 1])
            home_dir = _home_dir_for_disk(target.expanduser())
        else:
            target = Path(raw_target)
            home_dir = _home_dir_for_agent_path(target.expanduser())
    else:
        raw_target = _read_stderr_prompt("Agent profile path: ").strip()
        if not raw_target:
            print("No path selected.", file=sys.stderr)
            return None
        target = Path(raw_target)
        home_dir = _home_dir_for_agent_path(target.expanduser())

    detected = list(detect_platforms(project=False, home_dir=home_dir))
    if detected:
        print(f"Detected platforms: {', '.join(detected)}", file=sys.stderr)
        return detected, home_dir

    print(f"No supported profiles found at {home_dir}.", file=sys.stderr)
    print(f"Available platforms: {available_platforms}", file=sys.stderr)
    platform = _read_stderr_prompt(f"Platform to {action}: ").strip()
    if not platform:
        print("No platform selected.", file=sys.stderr)
        return None
    return [platform], home_dir


def _resolve_agent_command_target(args: argparse.Namespace, *, action: str) -> _AgentCommandResolution | None:
    from agents.install import available_platforms_text, resolve_platforms

    requested_platforms = [*args.platforms, *args.platform]
    project = not args.user
    project_dir = Path(args.project_dir)
    home_dir = None
    platforms = resolve_platforms(
        requested_platforms,
        install_all=args.all,
        auto_detect=not requested_platforms and not args.all,
        project=project,
        project_dir=project_dir,
    )
    if not platforms:
        prompted = None
        if project and not requested_platforms and not args.all:
            prompted = _prompt_agent_target(available_platforms_text(), action=action)
        if prompted is not None:
            prompted_platforms, home_dir = prompted
            project = False
            platforms = resolve_platforms(prompted_platforms)
    if not platforms:
        print(_no_agent_profiles_message(), file=sys.stderr)
        return None
    return _AgentCommandResolution(platforms=platforms, project=project, project_dir=project_dir, home_dir=home_dir)


def _home_dir_for_agent_path(path: Path) -> Path:
    resolved = path.resolve(strict=False) if path.is_absolute() else path
    if path.is_absolute() and resolved == Path(resolved.anchor):
        return _home_dir_for_disk(resolved)
    if path.name == "skills" and path.parent.name in _AGENT_PROFILE_DIR_NAMES:
        return path.parent.parent
    if path.name in _AGENT_PROFILE_DIR_NAMES:
        return path.parent
    return path

def _home_dir_for_disk(disk_root: Path) -> Path:
    home = Path.home()
    if not disk_root.is_absolute():
        return home
    try:
        home_relative = home.relative_to(Path(home.anchor))
    except ValueError:
        return disk_root / home.name
    return disk_root / home_relative


_AGENT_PROFILE_DIR_NAMES = {
    ".agents",
    ".antigravity",
    ".claude",
    ".codex",
    ".config",
    ".copilot",
    ".cursor",
    ".gemini",
    ".github",
    ".hermes",
    ".kilocode",
    ".kimi",
    ".openclaw",
}


def _read_stderr_prompt(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr, flush=True)
    try:
        return sys.stdin.readline().strip()
    except KeyboardInterrupt as exc:
        print(file=sys.stderr)
        raise _PromptInterrupted from exc


def _print_storage_inspection(payload: dict[str, Any]) -> None:
    blocks = payload["blocks"]
    records = payload["records"]
    compression = payload["compression"]
    dense = payload["dense_nodes"]
    indexes = payload["index_stats"]
    print(f"Path: {payload['path']}")
    print(f"Schema version: {payload['manifest'].get('schema_version', payload.get('schema_version', 0))}")
    print(f"Generation id: {payload['generation_id']}")
    print(f"Block size: {payload['block_size']}")
    print(f"Data offset: {payload.get('data_offset', 0)}")
    print(f"Root index offset: {payload['root_index_offset']}")
    print(f"Blocks: total={blocks['total']}, data={blocks['data']}, superblock={blocks['superblock']}")
    print(f"Records: {records['total']}")
    for kind, count in sorted(records["by_kind"].items()):
        print(f"  {kind}: {count}")
    print(
        "Compression: "
        f"compressed={payload['bytes']['compressed_payload']}, "
        f"uncompressed={payload['bytes']['uncompressed_payload']}, "
        f"ratio={compression['ratio']:.3f}, "
        f"saved={compression['space_saved_ratio']:.3f}"
    )
    print(f"Dense nodes: {dense['count']} (threshold={dense['threshold']})")
    for node_id in dense["ids"][:10]:
        print(f"  {node_id}")
    wal = payload.get("wal", {})
    print(f"WAL: exists={wal.get('exists', False)}, frames={wal.get('frames', 0)}, bytes={wal.get('bytes', 0)}")
    root_index = payload.get("root_index", {})
    print(
        "Root index: "
        f"nodes={root_index.get('nodes', 0)}, "
        f"edges={root_index.get('edges', 0)}, "
        f"node_keys={root_index.get('node_keys', 0)}, "
        f"edge_patterns={root_index.get('edge_patterns', 0)}"
    )
    space_map = payload.get("space_map", {})
    print(f"Space map free bytes: {space_map.get('free_bytes_total', 0)}")
    print("Index stats:")
    for key, value in sorted(indexes.items()):
        print(f"  {key}: {value}")


def _print_storage_compaction(payload: dict[str, Any]) -> None:
    print(f"Compacted: {payload['path']}")
    print(f"Generation: {payload['generation_id_before']} -> {payload['generation_id_after']}")
    print(f"Blocks: {payload['blocks_before']} -> {payload['blocks_after']}")
    print(f"Records: {payload['records_before']} -> {payload['records_after']}")
    print(f"Bytes: {payload['bytes_before']} -> {payload['bytes_after']}")
    print(f"Bytes reclaimed: {payload['bytes_reclaimed']}")


def _print_agent_status(payload: dict[str, Any]) -> None:
    print(f"Agent workspace: {'initialized' if payload['exists'] else 'not initialized'}")
    print(f"Agent id: {payload.get('agent_id') or ''}")
    print(f"Standard storage: {payload['standard_storage']}")
    print(f"Agent storage: {payload['agent_storage']}")
    print(f"Agent bus: {payload.get('bus_storage') or ''}")
    if payload.get("initialized_at"):
        print(f"Initialized at: {payload['initialized_at']}")
    print(f"Nodes: {payload['nodes']}")
    print(f"Relations: {payload['relations']}")
    print(f"Derived nodes: {payload['derived_nodes']}")
    print(f"Agent nodes: {payload['agent_nodes']}")
    if payload.get("current_session_id"):
        print(f"Current session: {payload['current_session_id']} ({payload.get('current_session_title') or ''})")


def _print_agent_node(payload: dict[str, Any]) -> None:
    node = payload.get("node") or payload.get("task") or payload
    print(f"{node['id']}\t{node['type']}\t{node.get('status') or ''}\t{node.get('title') or node.get('content') or ''}")


def _print_agent_list(payload: dict[str, Any]) -> None:
    for node in payload.get("nodes", []):
        print(f"{node['updated_at']}\t{node['id']}\t{node['type']}\t{node.get('status') or ''}\t{node.get('title') or ''}")
    for edge in payload.get("relations", []):
        print(f"{edge['updated_at']}\t{edge['id']}\t{edge['relation']}\t{edge['from_id']} -> {edge['to_id']}")


def _print_agent_relations(payload: dict[str, Any]) -> None:
    relations = payload.get("relations", [])
    if not relations and "relation" in payload:
        relations = [payload["relation"]]
    for relation in relations:
        print(f"{relation['id']}\t{relation['relation']}\t{relation['from_id']} -> {relation['to_id']}")


def _print_agent_search(payload: dict[str, Any]) -> None:
    for item in payload.get("results", []):
        node = item["node"]
        print(f"{float(item['score']):.3f}\t{node['id']}\t{node['type']}\t{node.get('status') or ''}\t{node.get('title') or ''}")


def _print_agent_map(payload: dict[str, Any]) -> None:
    filters = payload.get("filters") or {}
    if filters:
        print("Filters:")
        for key, value in sorted(filters.items()):
            print(f"  {key}: {value}")
    sections = [
        ("Open tasks", payload.get("open_tasks", [])),
        ("Decisions", payload.get("decisions", [])),
        ("Files", payload.get("files", [])),
        ("Symbols", payload.get("symbols", [])),
    ]
    if "completed_tasks" in payload:
        sections.insert(1, ("Completed tasks", payload.get("completed_tasks", [])))
    for title, nodes in sections:
        print(f"{title}:")
        if not nodes:
            print("  none")
            continue
        for node in nodes:
            print(f"  {node['id']}\t{node.get('status') or ''}\t{node.get('title') or node.get('content') or ''}")
    print("Relations:")
    relations = payload.get("relations", [])
    if not relations:
        print("  none")
    for edge in relations:
        print(f"  {edge['id']}\t{edge['relation']}\t{edge['from_id']} -> {edge['to_id']}")


def _print_agent_bus(payload: dict[str, Any]) -> None:
    print(f"Agent bus: {payload.get('bus_storage') or ''}")
    print(f"Current agent: {payload.get('current_agent_id') or 'none'}")
    print("Agents:")
    agents = payload.get("agents", [])
    if not agents:
        print("  none")
    for agent in agents:
        print(f"  {agent.get('agent_id') or ''}\t{agent.get('status') or ''}\t{agent.get('agent_storage') or ''}")
    print("Messages:")
    messages = payload.get("messages", [])
    if not messages:
        print("  none")
    for message in messages:
        print(f"  {message['updated_at']}\t{message.get('agent_id') or ''} -> {message.get('target_agent_id') or ''}\t{message.get('content') or ''}")
    print("Handoffs:")
    handoffs = payload.get("handoffs", [])
    if not handoffs:
        print("  none")
    for handoff in handoffs:
        print(f"  {handoff['updated_at']}\t{handoff.get('agent_id') or ''} -> {handoff.get('target_agent_id') or ''}\t{handoff.get('title') or ''}")


def _load_agent_batch_file(path: str) -> list[dict[str, Any]]:
    if path == "-":
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        source = path
        raw = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid agent batch JSON in {source}: {exc}") from exc
    operations = payload.get("operations") if isinstance(payload, dict) else payload
    if not isinstance(operations, list):
        raise ValueError("Agent batch JSON must be an array or an object with an operations array")
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise ValueError(f"Agent batch operation {index} must be an object")
    return operations


def _agent_batch_entry(value: str, *, op: str, text_key: str) -> dict[str, Any]:
    text = value.strip()
    if not text:
        raise ValueError(f"Agent batch {op} text must not be empty")
    operation: dict[str, Any] = {"op": op, text_key: text}
    alias, content = _split_agent_batch_alias(text)
    if alias is not None:
        operation[text_key] = content
        operation["as"] = alias
    return operation


def _split_agent_batch_alias(value: str) -> tuple[str | None, str]:
    alias, separator, content = value.partition("=")
    if not separator:
        return None, value
    alias = alias.strip()
    content = content.strip()
    if not alias or not content:
        return None, value
    if not (alias[0].isalpha() or alias[0] == "_"):
        return None, value
    if any(not (char.isalnum() or char in {"_", "-"}) for char in alias):
        return None, value
    return alias, content


def _split_agent_batch_targets(value: str) -> list[str]:
    targets = [item.strip() for item in value.split(",") if item.strip()]
    if not targets:
        raise ValueError("Agent batch link targets must not be empty")
    return targets


def _agent_batch_operations_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    if getattr(args, "file", None):
        operations.extend(_load_agent_batch_file(args.file))
    for value in getattr(args, "note", []) or []:
        operations.append(_agent_batch_entry(value, op="add", text_key="text"))
    for value in getattr(args, "task", []) or []:
        operations.append(_agent_batch_entry(value, op="task.add", text_key="description"))
    for value in getattr(args, "decision", []) or []:
        operations.append(_agent_batch_entry(value, op="decision.add", text_key="text"))
    for value in getattr(args, "finding", []) or []:
        operations.append(_agent_batch_entry(value, op="finding.add", text_key="text"))
    for value in getattr(args, "done", []) or []:
        task_id = value.strip()
        if not task_id:
            raise ValueError("Agent batch done id must not be empty")
        operations.append({"op": "task.done", "id": task_id})
    for from_id, relation, to_id in getattr(args, "link", []) or []:
        operations.append({"op": "link", "from": from_id, "to": to_id, "relation": relation})
    for from_id, relation, targets in getattr(args, "link_many", []) or []:
        operations.append({"op": "link-many", "from": from_id, "to": _split_agent_batch_targets(targets), "relation": relation})
    for from_id, targets in getattr(args, "touches", []) or []:
        operations.append({"op": "link-many", "from": from_id, "to": _split_agent_batch_targets(targets), "relation": "touches"})
    if not operations:
        raise ValueError("Agent batch requires a JSON file or at least one inline operation")
    return operations


_READ_ONLY_COMMANDS = {"query_context", "query_explore", "query_graph", "query_memories", "query", "stats", "inspect"}
_MUTATING_REQL_COMMANDS = {"COMMUNITIES", "HUBS"}


def _query_requires_write(args: argparse.Namespace) -> bool:
    if str(getattr(args, "command", "")) != "query":
        return False
    statement = _normalize_reql_statement_arg(getattr(args, "statement", None))
    first = statement.split(None, 1)[0].rstrip(";").upper() if statement else ""
    return first in _MUTATING_REQL_COMMANDS


def _open(args: argparse.Namespace, config: REQLConfig, profile_logger: PerformanceLogger | None = None) -> MemoryGraph:
    read_only_command = str(getattr(args, "command", "")) in _READ_ONLY_COMMANDS and not _query_requires_write(args)
    if read_only_command:
        if profile_logger:
            profile_logger.event("storage.open.start", category="lifecycle", path=str(args.storage), read_only=True)
            try:
                with profile_logger.span("storage.open", path=str(args.storage), read_only=True):
                    return MemoryGraph.open(Path(args.storage), config=config, profile_logger=profile_logger, read_only=True)
            except StorageError as exc:
                if "missing REQL storage" not in str(exc):
                    raise
                profile_logger.event("storage.open.read_only_unavailable", category="lifecycle", reason=str(exc))
                with profile_logger.span("storage.open", path=str(args.storage), read_only=False):
                    graph = MemoryGraph.open(Path(args.storage), config=config, profile_logger=profile_logger)
                _checkpoint_opened_store_if_needed(graph, profile_logger)
                return graph
        try:
            return MemoryGraph.open(Path(args.storage), config=config, read_only=True)
        except StorageError as exc:
            if "missing REQL storage" not in str(exc):
                raise
            graph = MemoryGraph.open(Path(args.storage), config=config)
            _checkpoint_opened_store_if_needed(graph, None)
            return graph
    if profile_logger:
        profile_logger.event("storage.open.start", category="lifecycle", path=str(args.storage), read_only=False)
        with profile_logger.span("storage.open", path=str(args.storage), read_only=False):
            graph = MemoryGraph.open(Path(args.storage), config=config, profile_logger=profile_logger)
        _checkpoint_opened_store_if_needed(graph, profile_logger)
        return graph
    graph = MemoryGraph.open(Path(args.storage), config=config)
    _checkpoint_opened_store_if_needed(graph, None)
    return graph


def _checkpoint_opened_store_if_needed(graph: MemoryGraph, profile_logger: PerformanceLogger | None) -> None:
    if bool(getattr(graph.store, "read_only", False)):
        if profile_logger:
            profile_logger.event("storage.open_checkpoint.result", category="counter", checkpointed=False, reason="read_only")
        return
    checkpoint = getattr(graph.store, "checkpoint_if_needed", None)
    if checkpoint is None:
        return
    if profile_logger:
        with profile_logger.span("storage.open_checkpoint"):
            result = checkpoint()
        profile_logger.event("storage.open_checkpoint.result", category="counter", **dict(result))
        return
    checkpoint()


def _default_storage_path(build_path: str | Path = ".") -> Path:
    root = Path(build_path).expanduser()
    if root.suffix:
        root = root.parent
    return root.resolve(strict=False) / DEFAULT_STORAGE_DIR / DEFAULT_STORAGE_FILE


def _resolve_storage_arg(args: argparse.Namespace) -> str:
    explicit = getattr(args, "storage", None)
    if explicit:
        return str(explicit)
    build_path: str | Path = "."
    if getattr(args, "command", None) in {"project", "cache"}:
        build_path = getattr(args, "path", ".")
    return str(_default_storage_path(build_path))


def _config_start_dir(args: argparse.Namespace) -> str | Path | None:
    if getattr(args, "command", None) in {"project", "cache"}:
        return getattr(args, "path", None)
    return None


def _profile_logger_from_config(config: REQLConfig, command: str) -> PerformanceLogger | None:
    if not bool(getattr(config.diagnostics, "enabled", False)):
        return None
    path = str(getattr(config.diagnostics, "path", "") or "").strip()
    if not path:
        return None
    return PerformanceLogger(path, command=command)


def _add_query_graph_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--max-nodes", type=int, default=80)
    parser.add_argument("--max-edges", type=int, default=160)
    parser.add_argument("--max-sources", type=int, default=20)
    parser.add_argument("--max-items", type=int, default=18, help="Maximum rendered items per section")
    parser.add_argument("--no-filter-generic", action="store_true", help="Keep isolated generic nodes in the returned subgraph")
    parser.add_argument("--include-archived", action="store_true", help="Include archived graph records")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _add_query_memories_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--max-text-chars", type=int, default=600)
    parser.add_argument("--no-sources", action="store_true", help="Do not include connected source texts")
    parser.add_argument("--no-filter-generic", action="store_true", help="Keep isolated generic nodes")
    parser.add_argument("--include-archived", action="store_true", help="Include archived graph records")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _add_query_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-items", type=int, default=20, help="Maximum rendered context items")
    parser.add_argument("--include-archived", action="store_true", help="Include archived graph records")
    parser.add_argument("--cleanup", action="store_true", help="Return only cleanup findings matching the query")
    parser.add_argument("--include-risky", action="store_true", help="In cleanup mode, include validate/risky and low-confidence findings")
    parser.add_argument("--code", action="store_true", help="Limit context to code symbols and source files")
    parser.add_argument("--docs", action="store_true", help="Limit context to documentation and imported document content")
    parser.add_argument("--test", action="store_true", help="Limit context to tests")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _add_query_explore_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--limit", type=int, default=12, help="Maximum records per explore section")
    parser.add_argument("--max-items", type=int, default=18, help="Maximum rendered code-context items")
    parser.add_argument(
        "--view",
        action="append",
        choices=["all", "owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "code"],
        help="Explore view to include; may be repeated. Defaults to all views.",
    )
    parser.add_argument("--owners-only", action="store_true", help="Shortcut for --view owners")
    parser.add_argument("--callers-only", action="store_true", help="Shortcut for --view callers")
    parser.add_argument("--public-surface-only", action="store_true", help="Shortcut for --view public_surface")
    parser.add_argument("--serialization-paths-only", action="store_true", help="Shortcut for --view serialization_paths")
    parser.add_argument("--docs-mentions-only", action="store_true", help="Shortcut for --view docs_mentions")
    parser.add_argument("--code-only", action="store_true", help="Shortcut for --view code")
    parser.add_argument("--include-archived", action="store_true", help="Include archived graph records")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _query_explore_views_from_args(args: argparse.Namespace) -> list[str] | None:
    shortcuts = [
        ("owners_only", "owners"),
        ("callers_only", "callers"),
        ("public_surface_only", "public_surface"),
        ("serialization_paths_only", "serialization_paths"),
        ("docs_mentions_only", "docs_mentions"),
        ("code_only", "code"),
    ]
    selected = [view for attr, view in shortcuts if bool(getattr(args, attr, False))]
    if selected:
        return selected
    return list(args.view or []) or None


def _query_context_mode_from_args(args: argparse.Namespace) -> str:
    if bool(getattr(args, "cleanup", False)):
        return "cleanup"
    return "informative"


def _query_context_scopes_from_args(args: argparse.Namespace) -> list[str] | None:
    scopes = [scope for attr, scope in (("code", "code"), ("docs", "docs"), ("test", "test")) if bool(getattr(args, attr, False))]
    return scopes or None


def _add_reql_statement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("statement", nargs="*", help="REQL statement")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _add_agent_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", dest="node_type", default=None, help="Filter by agent node type")
    parser.add_argument("--status", default=None, help="Filter by node status")
    parser.add_argument("--relation", default=None, help="Filter by relation type")
    parser.add_argument("--since", default=None, help="Filter by ISO updated_at timestamp")
    parser.add_argument("--limit", type=int, default=50, help="Maximum items to print")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


_TEXT_QUERY_CLAUSES = {
    "RETRIEVE": {"TYPE", "TYPES", "TOP", "LIMIT", "DEPTH", "INCLUDE", "NO", "RETURN", "MAX"},
    "SEARCH": {"TYPE", "TYPES", "TOP", "LIMIT", "DEPTH", "CONTEXT", "INCLUDE", "RETURN"},
}


def _normalize_reql_statement_arg(statement: list[str] | str | None) -> str:
    if statement is None:
        return ""
    if isinstance(statement, str):
        return statement.strip()
    parts = [part for part in statement if part]
    if not parts:
        return ""
    joined = " ".join(parts).strip()
    if len(parts) == 1:
        return joined
    return _quote_split_text_query(joined)


def _quote_split_text_query(statement: str) -> str:
    tokens = statement.split()
    if len(tokens) < 3:
        return statement
    command = tokens[0].upper()
    clauses = _TEXT_QUERY_CLAUSES.get(command)
    if not clauses:
        return statement
    if tokens[1].startswith(("'", '"')):
        return statement

    clause_index = len(tokens)
    for index, token in enumerate(tokens[2:], start=2):
        if token.upper() in clauses:
            clause_index = index
            break
    if clause_index <= 2:
        return statement

    text = " ".join(tokens[1:clause_index])
    suffix = " ".join(tokens[clause_index:])
    quoted = json.dumps(text, ensure_ascii=False)
    return f"{tokens[0]} {quoted}" + (f" {suffix}" if suffix else "")


def _normalize_subparser_help(action: argparse._SubParsersAction) -> None:
    action._choices_actions.sort(key=lambda choice: choice.dest)
    action.metavar = "{" + ",".join(choice.dest for choice in action._choices_actions) + "}"
    action.choices = _SortedSubparserChoices(action.choices)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reql",
        description="Relational Entities Query Language memory graph engine",
    )
    parser.add_argument(
        "--storage",
        default=None,
        help="REQL block storage path. Defaults to <build path>/.reql/memory.reql for project/cache commands, otherwise ./.reql/memory.reql",
    )
    parser.add_argument("--config", default=None, help="Path to conf.yaml")
    parser.add_argument(
        "--set",
        dest="config_overrides",
        action="append",
        default=[],
        metavar="SECTION.OPTION=VALUE",
        help="Override a config value after loading conf.yaml; may be repeated",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="Install REQL agent instructions for coding assistants")
    install.add_argument(
        "platforms",
        nargs="*",
        help="Platforms such as codex, claude, opencode, kilo, cursor, gemini, copilot, openclaw, hermes, kimi, antigravity, agents, or all",
    )
    install.add_argument("--platform", action="append", default=[], help="Platform name; may be repeated or comma-separated")
    install.add_argument("--all", action="store_true", help="Install all supported assistant integrations instead of auto-detecting installed agents")
    install.add_argument("--user", action="store_true", help="Install into the user assistant profile instead of the current project")
    install.add_argument("--project-dir", default=".", help="Project root for project installs")
    install.add_argument("--command-dir", default=None, help="Directory where the REQL command shim is installed")
    install.add_argument("--no-hooks", action="store_true", help="Do not install automatic assistant hooks")
    install.add_argument("--dry-run", action="store_true", help="Print planned files without writing them")
    install.add_argument("--json", action="store_true", help="Print structured JSON result")

    uninstall = sub.add_parser("uninstall", help="Remove REQL agent instructions, version stamps, and hooks")
    uninstall.add_argument(
        "platforms",
        nargs="*",
        help="Platforms such as codex, claude, opencode, kilo, cursor, gemini, copilot, openclaw, hermes, kimi, antigravity, agents, or all",
    )
    uninstall.add_argument("--platform", action="append", default=[], help="Platform name; may be repeated or comma-separated")
    uninstall.add_argument("--all", action="store_true", help="Uninstall all supported assistant integrations")
    uninstall.add_argument("--user", action="store_true", help="Remove from the user assistant profile instead of the current project")
    uninstall.add_argument("--project-dir", default=".", help="Project root for project uninstalls")
    uninstall.add_argument("--command-dir", default=None, help="Directory where the REQL command shim was installed")
    uninstall.add_argument("--dry-run", action="store_true", help="Print planned removals without writing them")
    uninstall.add_argument("--json", action="store_true", help="Print structured JSON result")

    agent = sub.add_parser("agent", help="Agent Workspace commands for coding-agent working memory")
    agent.add_argument("--agent", dest="agent_id", default=None, help="Use an agent id; defaults to REQL_AGENT_ID or the bus current agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_init = agent_sub.add_parser("init", help="Initialize a private agent working graph from the standard graph")
    agent_init.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_status = agent_sub.add_parser("status", help="Show agent working graph status")
    agent_status.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_sync = agent_sub.add_parser("sync", help="Refresh derived standard graph references without deleting agent memory")
    agent_sync.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_reset = agent_sub.add_parser("reset", help="Reset the agent working graph from the current standard graph")
    agent_reset.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_add = agent_sub.add_parser("add", help="Add an operational note")
    agent_add.add_argument("text")
    agent_add.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_task = agent_sub.add_parser("task", help="Task commands: add, done")
    agent_task_sub = agent_task.add_subparsers(dest="agent_task_command", required=True)
    agent_task_add = agent_task_sub.add_parser("add", help="Add an agent task")
    agent_task_add.add_argument("description")
    agent_task_add.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_task_done = agent_task_sub.add_parser("done", help="Mark an agent task as done")
    agent_task_done.add_argument("id")
    agent_task_done.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_decision = agent_sub.add_parser("decision", help="Decision commands: add")
    agent_decision_sub = agent_decision.add_subparsers(dest="agent_decision_command", required=True)
    agent_decision_add = agent_decision_sub.add_parser("add", help="Record a technical decision")
    agent_decision_add.add_argument("decision")
    agent_decision_add.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_finding = agent_sub.add_parser("finding", help="Finding commands: add")
    agent_finding_sub = agent_finding.add_subparsers(dest="agent_finding_command", required=True)
    agent_finding_add = agent_finding_sub.add_parser("add", help="Record a code finding")
    agent_finding_add.add_argument("observation")
    agent_finding_add.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_session = agent_sub.add_parser("session", help="Session commands: start")
    agent_session_sub = agent_session.add_subparsers(dest="agent_session_command", required=True)
    agent_session_start = agent_session_sub.add_parser("start", help="Start a new current agent session")
    agent_session_start.add_argument("title")
    agent_session_start.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_link = agent_sub.add_parser("link", help="Create a relation between agent graph elements")
    agent_link.add_argument("id1")
    agent_link.add_argument("id2")
    agent_link.add_argument("--relation", required=True, help="Relation type")
    agent_link.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_link_task = agent_sub.add_parser("link-task", help="Link an open task to a target resolved from a readable path")
    agent_link_task.add_argument("--task", dest="task_id", default=None, help="Task id; defaults to the latest open task in the current session")
    agent_link_task.add_argument("--file", dest="file_path", required=True, help="File path to resolve in the agent graph")
    agent_link_task.add_argument("--relation", default="touches", help="Relation type; defaults to touches")
    agent_link_task.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_link_many = agent_sub.add_parser("link-many", help="Create one relation from a source to multiple targets")
    agent_link_many.add_argument("id1")
    agent_link_many.add_argument("ids", nargs="+", help="One or more target node IDs")
    agent_link_many.add_argument("--relation", required=True, help="Relation type")
    agent_link_many.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_batch = agent_sub.add_parser("batch", help="Apply agent workspace operations from a JSON file or inline options")
    agent_batch.add_argument("file", nargs="?", default=None, help="Optional JSON file path, or '-' to read from stdin")
    agent_batch.add_argument("--note", action="append", default=[], metavar="[ALIAS=]TEXT", help="Add an operational note; may be repeated")
    agent_batch.add_argument("--task", action="append", default=[], metavar="[ALIAS=]TEXT", help="Add an open task; may be repeated")
    agent_batch.add_argument("--decision", action="append", default=[], metavar="[ALIAS=]TEXT", help="Add a decision; may be repeated")
    agent_batch.add_argument("--finding", action="append", default=[], metavar="[ALIAS=]TEXT", help="Add a finding; may be repeated")
    agent_batch.add_argument("--done", action="append", default=[], metavar="TASK_ID", help="Mark a task done; may be repeated")
    agent_batch.add_argument("--link", action="append", nargs=3, metavar=("FROM", "RELATION", "TO"), default=[], help="Create one relation; aliases may be referenced as $alias")
    agent_batch.add_argument("--link-many", dest="link_many", action="append", nargs=3, metavar=("FROM", "RELATION", "TARGETS"), default=[], help="Create relations from one source to comma-separated targets")
    agent_batch.add_argument("--touches", action="append", nargs=2, metavar=("FROM", "TARGETS"), default=[], help="Create touches relations from one source to comma-separated targets")
    agent_batch.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_search = agent_sub.add_parser("search", help="Textually search the agent working graph")
    agent_search.add_argument("query")
    agent_search.add_argument("--type", dest="node_type", default=None, help="Filter by agent node type")
    agent_search.add_argument("--status", default=None, help="Filter by node status")
    agent_search.add_argument("--limit", type=int, default=20, help="Maximum matches")
    agent_search.add_argument("--metadata", action="store_true", help="Include timestamps, source fields, and stored metadata")
    agent_search.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_show = agent_sub.add_parser("show", help="Show a node or relation")
    agent_show.add_argument("id")
    agent_show.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_list = agent_sub.add_parser("list", help="List recent working memory items")
    _add_agent_filters(agent_list)
    agent_map = agent_sub.add_parser("map", help="Summarize the current agent working memory")
    agent_map.add_argument("--task", dest="task_id", default=None, help="Focus the map on one agent task and related agent items")
    agent_map.add_argument("--session", default=None, help="Focus the map on an agent session id, or 'current'")
    agent_map.add_argument("--since", default=None, help="Only include agent items or relations updated at or after this ISO timestamp")
    agent_map.add_argument("--completed", action="store_true", help="Include completed tasks for a session summary")
    agent_map.add_argument("--metadata", action="store_true", help="Include timestamps, source fields, and stored metadata")
    agent_map.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_bus = agent_sub.add_parser("bus", help="Read the shared internal agent bus")
    agent_bus.add_argument("--limit", type=int, default=50, help="Maximum agents, messages, and handoffs")
    agent_bus.add_argument("--include-payloads", action="store_true", help="Include full handoff payload snapshots")
    agent_bus.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_publish = agent_sub.add_parser("publish", help="Publish a short message to the shared agent bus")
    agent_publish.add_argument("text")
    agent_publish.add_argument("--kind", default="note", help="Message kind")
    agent_publish.add_argument("--target", default="all", help="Target agent id, or all")
    agent_publish.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_handoff = agent_sub.add_parser("handoff", help="Publish this agent's saved working context to the master bus")
    agent_handoff.add_argument("summary", nargs="?", default=None)
    agent_handoff.add_argument("--target", default="master", help="Target agent id")
    agent_handoff.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_export = agent_sub.add_parser("export", help="Export the agent working graph")
    agent_export.add_argument("--metadata", action="store_true", help="Include full workspace metadata and all stored nodes")
    agent_export.add_argument("--json", action="store_true", help="Print structured JSON result")

    config = sub.add_parser("config", help="Configuration commands: show, init")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="Print the effective configuration")
    config_init = config_sub.add_parser("init", help="Create a sample conf.yaml if absent")
    config_init.add_argument("--path", default="conf.yaml", help="Target config file path")

    project = sub.add_parser("project", help="Project commands: compile, exclude, update, status, report")
    project_sub = project.add_subparsers(dest="project_command", required=True)

    project_compile = project_sub.add_parser("compile", help="Scan and incrementally compile dirty artifacts")
    project_compile.add_argument("path", nargs="?", default=".", help="Project path to compile; defaults to the current working directory")
    project_compile.add_argument("--max-file-size-mb", type=float, default=None)
    project_compile.add_argument("--watch", action="store_true", help="Monitor the project filesystem and compile dirty artifacts automatically")
    project_compile.add_argument("--watch-interval", type=float, default=0.5, help="Maximum seconds to wait between bounded watchdog checks")
    project_compile.add_argument("--watch-debounce", type=float, default=0.1, help="Seconds to wait before compiling detected changes")
    project_compile.add_argument("--watch-iterations", type=int, default=None, help="Stop after this many watch checks; default is until interrupted")

    project_update = project_sub.add_parser("update", help="Incrementally update a previously compiled project")
    project_update.add_argument("path", nargs="?", default=".", help="Project path to update; defaults to the current working directory")
    project_update.add_argument("--max-file-size-mb", type=float, default=None)

    project_status = project_sub.add_parser("status", help="Show registered project artifact status")
    project_status.add_argument("path")
    project_status.add_argument("--json", action="store_true", help="Print structured JSON result")

    project_report = project_sub.add_parser("report", help="Write project Markdown reports")
    project_report.add_argument("path")
    project_report.add_argument("--output", default=None, help="Output directory for GRAPH_REPORT.md, GRAPH_DELTAS.md, and CACHE_REPORT.md")
    project_report.add_argument("--json", action="store_true", help="Print structured JSON result")

    project_exclude = project_sub.add_parser("exclude", help="Add scan.exclude patterns to a project config")
    project_exclude.add_argument("patterns", nargs="+", help="One or more scan.exclude patterns to add")
    project_exclude.add_argument("--path", default=".", help="Project directory whose config should be updated; defaults to the current working directory")
    project_exclude.add_argument("--json", action="store_true", help="Print structured JSON result")

    cache = sub.add_parser("cache", help="Cache commands: status, clear")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_status = cache_sub.add_parser("status", help="Show incremental cache status for a project path")
    cache_status.add_argument("path", nargs="?", default=".", help="Project path; defaults to the current working directory")
    cache_status.add_argument("--max-file-size-mb", type=float, default=None)
    cache_status.add_argument("--json", action="store_true", help="Print structured JSON result")
    cache_clear = cache_sub.add_parser("clear", help="Archive cache metadata for a project path")
    cache_clear.add_argument("path", nargs="?", default=".", help="Project path; defaults to the current working directory")
    cache_clear.add_argument("--json", action="store_true", help="Print structured JSON result")

    query_context = sub.add_parser("query_context", help="Compose a deterministic context block for a query")
    _add_query_context_arguments(query_context)

    query_explore = sub.add_parser("query_explore", help="Explore owners, callers, public surface, serialization paths, docs, and code")
    _add_query_explore_arguments(query_explore)

    query_graph = sub.add_parser("query_graph", help="Retrieve a structured query-centered subgraph")
    _add_query_graph_arguments(query_graph)
    query_memories = sub.add_parser("query_memories", help="Retrieve relevant memory texts for a query")
    _add_query_memories_arguments(query_memories)

    query = sub.add_parser("query", help="Execute a REQL statement")
    _add_reql_statement_arguments(query)

    stats = sub.add_parser("stats", help="Print graph statistics")
    stats.add_argument("--json", action="store_true")

    storage = sub.add_parser("storage", help="Storage commands: inspect, compact")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    storage_compact = storage_sub.add_parser("compact", help="Rewrite the block store into a compact generation")
    storage_compact.add_argument("--json", action="store_true", help="Print structured JSON result")
    storage_inspect = storage_sub.add_parser("inspect", help="Inspect block layout, compression, dense nodes, and indexes")
    storage_inspect.add_argument("--json", action="store_true", help="Print structured JSON result")

    export = sub.add_parser("export", help="Export nodes and edges as JSON or standalone HTML")
    export.add_argument("--out", default=None, help="Optional output file or directory")
    export.add_argument("--html", action="store_true", help="Write an interactive standalone graph.html visualization")
    export.add_argument("--json", action="store_true", help="Write graph JSON to a file")

    inspect = sub.add_parser("inspect", help="Inspect a node and adjacent edges")
    inspect.add_argument("--node-id", required=True)
    inspect.add_argument("--limit", type=int, default=30)
    inspect.add_argument("--json", action="store_true", help="Print structured JSON result")

    _normalize_subparser_help(sub)
    _normalize_subparser_help(config_sub)
    _normalize_subparser_help(project_sub)
    _normalize_subparser_help(cache_sub)
    _normalize_subparser_help(storage_sub)
    _normalize_subparser_help(agent_sub)
    _normalize_subparser_help(agent_task_sub)
    _normalize_subparser_help(agent_decision_sub)
    _normalize_subparser_help(agent_finding_sub)
    _normalize_subparser_help(agent_session_sub)

    return parser


def _max_file_size_bytes(args: argparse.Namespace, config: REQLConfig) -> int:
    value = getattr(args, "max_file_size_mb", None)
    if value is None:
        value = config.scan.max_file_size_mb
    return max(0, int(float(value) * 1024 * 1024))


def _parsing_options(config: REQLConfig) -> dict[str, object]:
    return {"compile": config.compile.to_dict()}


def _document_format_ingest_enabled(document_policies: list[dict[str, object]], format_name: str) -> bool:
    wanted = format_name.casefold()
    return any(
        str(item.get("format") or "").casefold() == wanted and bool(item.get("ingest", True))
        for item in document_policies
    )


def _append_config_exclude_patterns(project_path: str | Path, patterns: list[str]) -> dict[str, object]:
    root = Path(project_path).expanduser().resolve(strict=False)
    if root.exists() and not root.is_dir():
        raise ValueError(f"project path is not a directory: {root}")
    if not root.exists():
        raise ValueError(f"project path does not exist: {root}")

    normalized: list[str] = []
    for raw in patterns:
        pattern = raw.strip().replace("\\", "/")
        if not pattern:
            raise ValueError("exclude patterns must not be empty")
        if "\n" in raw or "\r" in raw:
            raise ValueError("exclude patterns must be single-line values")
        _validate_exclude_pattern(pattern)
        if pattern not in normalized:
            normalized.append(pattern)

    config_path = root / CONFIG_FILENAME
    created = False
    if not config_path.exists():
        write_sample_config(config_path)
        created = True
    config = load_config(str(config_path))
    existing_rules = set(config.scan.exclude)
    added = [pattern for pattern in normalized if pattern not in existing_rules]
    skipped = [pattern for pattern in normalized if pattern in existing_rules]

    if added:
        current_text = config_path.read_text(encoding="utf-8")
        exclude_patterns = [*config.scan.exclude, *added]
        _write_text_atomic(config_path, _replace_scan_exclude(current_text, exclude_patterns))

    return {
        "path": str(config_path),
        "created": created,
        "added": added,
        "skipped": skipped,
    }


def _replace_scan_exclude(text: str, patterns: list[str]) -> str:
    lines = text.splitlines()
    scan_start = _top_level_section_line(lines, "scan")
    rendered = _render_yaml_string_list("exclude", patterns)
    if scan_start is None:
        prefix = text.rstrip("\n")
        separator = "\n\n" if prefix else ""
        return f"{prefix}{separator}scan:\n{rendered}\n"

    scan_end = _section_end(lines, scan_start)
    exclude_start = _section_option_line(lines, scan_start + 1, scan_end, "exclude")
    if exclude_start is None:
        updated = [*lines[:scan_end], *rendered.splitlines(), *lines[scan_end:]]
        return "\n".join(updated).rstrip("\n") + "\n"

    exclude_end = exclude_start + 1
    while exclude_end < scan_end:
        line = lines[exclude_end]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped and indent <= 2 and not stripped.startswith("- "):
            break
        exclude_end += 1
    updated = [*lines[:exclude_start], *rendered.splitlines(), *lines[exclude_end:]]
    return "\n".join(updated).rstrip("\n") + "\n"


def _top_level_section_line(lines: list[str], section: str) -> int | None:
    marker = f"{section}:"
    for index, line in enumerate(lines):
        if line.strip() == marker and not line.startswith((" ", "\t")):
            return index
    return None


def _section_end(lines: list[str], start: int) -> int:
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t")):
            return index
    return len(lines)


def _section_option_line(lines: list[str], start: int, end: int, option: str) -> int | None:
    prefix = f"{option}:"
    for index in range(start, end):
        line = lines[index]
        if len(line) - len(line.lstrip(" ")) == 2 and line.strip().startswith(prefix):
            return index
    return None


def _render_yaml_string_list(key: str, values: list[str]) -> str:
    if not values:
        return f"  {key}: []"
    lines = [f"  {key}:"]
    lines.extend(f"    - {json.dumps(value, ensure_ascii=False)}" for value in values)
    return "\n".join(lines)


def _validate_exclude_pattern(pattern: str) -> None:
    compact = pattern.strip("/")
    if pattern in DANGEROUS_EXCLUDE_PATTERNS or compact in DANGEROUS_EXCLUDE_PATTERNS:
        raise ValueError(f"refusing dangerous scan.exclude pattern that would exclude the workspace: {pattern}")


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _graph_html_path(raw_path: str | None) -> Path:
    path = Path(raw_path or "graph.html")
    if path.suffix.casefold() != ".html":
        path = path / "graph.html"
    return path


def _graph_json_path(raw_path: str | None) -> Path:
    path = Path(raw_path or "graph.json")
    if path.suffix.casefold() != ".json":
        path = path / "graph.json"
    return path


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit:
        raise
    profile_logger: PerformanceLogger | None = None

    if args.command == "config" and args.config_command == "init":
        try:
            path = write_sample_config(args.path)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Created {path}")
        return 0

    if args.command == "install":
        from agents.install import available_platforms_text, install_agent_files

        try:
            resolved = _resolve_agent_command_target(args, action="install")
            if resolved is None:
                return 2
            result = install_agent_files(
                resolved.platforms,
                project=resolved.project,
                project_dir=resolved.project_dir,
                home_dir=resolved.home_dir,
                command_dir=Path(args.command_dir) if args.command_dir else None,
                dry_run=args.dry_run,
                hooks=not args.no_hooks,
            )
        except ValueError as exc:
            print(f"{exc}. Available platforms: {available_platforms_text()}", file=sys.stderr)
            return 2
        except _PromptInterrupted:
            print("Install cancelled.", file=sys.stderr)
            return 130
        if args.json:
            _print_json(result.to_dict())
        else:
            print(f"REQL agent install ({result.scope})")
            for action in result.actions:
                print(f"{action.status}\t{action.platform}\t{action.kind}\t{action.path}")
        return 0

    if args.command == "uninstall":
        from agents.install import available_platforms_text, uninstall_agent_files

        try:
            resolved = _resolve_agent_command_target(args, action="uninstall")
            if resolved is None:
                return 2
            result = uninstall_agent_files(
                resolved.platforms,
                project=resolved.project,
                project_dir=resolved.project_dir,
                home_dir=resolved.home_dir,
                command_dir=Path(args.command_dir) if args.command_dir else None,
                dry_run=args.dry_run,
            )
        except ValueError as exc:
            print(f"{exc}. Available platforms: {available_platforms_text()}", file=sys.stderr)
            return 2
        except _PromptInterrupted:
            print("Uninstall cancelled.", file=sys.stderr)
            return 130
        if args.json:
            _print_json(result.to_dict())
        else:
            print(f"REQL agent uninstall ({result.scope})")
            for action in result.actions:
                print(f"{action.status}\t{action.platform}\t{action.kind}\t{action.path}")
        return 0

    if args.command == "project" and args.project_command == "exclude":
        try:
            result = _append_config_exclude_patterns(args.path, args.patterns)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            _print_json(result)
        else:
            action = "Created" if result["created"] else "Updated"
            print(f"{action}: {result['path']}")
            if result["added"]:
                print("Added rules:")
                for pattern in result["added"]:
                    print(f"  {pattern}")
            if result["skipped"]:
                print("Already present:")
                for pattern in result["skipped"]:
                    print(f"  {pattern}")
        return 0

    args.storage = _resolve_storage_arg(args)

    try:
        overrides = parse_config_override_assignments(args.config_overrides)
        config = load_effective_config(args.config, start_dir=_config_start_dir(args), overrides=overrides)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    profile_logger = _profile_logger_from_config(config, str(args.command))
    if profile_logger:
        profile_logger.event("cli.configured", category="lifecycle", argv=raw_argv)
        profile_logger.event("storage.resolved", category="lifecycle", path=str(args.storage))

    if args.command == "config":
        if args.config_command == "show":
            _print_json(config.to_dict())
            return 0

    if args.command == "agent":
        from memory.agent import AgentWorkspace

        agent_id = args.agent_id or os.environ.get("REQL_AGENT_ID")
        if args.agent_command == "init" and not agent_id:
            agent_id = AgentWorkspace.new_agent_id()
        workspace = AgentWorkspace(args.storage, agent_id=agent_id, config=config)
        try:
            if args.agent_command == "init":
                result = workspace.init()
                if args.json:
                    _print_json(result)
                else:
                    print(f"Agent id: {result['agent_id']}")
                    print(f"Initialized agent workspace: {result['agent_storage']}")
                    print(f"Agent bus: {result['bus_storage']}")
                    print(f"Derived nodes: {result['derived_nodes']}")
                    print(f"Derived relations: {result['derived_relations']}")
                return 0
            if args.agent_command == "status":
                result = workspace.status()
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_status(result)
                return 0
            if args.agent_command == "sync":
                result = workspace.sync()
                if args.json:
                    _print_json(result)
                else:
                    print(f"Synced agent workspace: {result['agent_storage']}")
                    print(f"Derived nodes: {result['derived_nodes']}")
                    print(f"Derived relations: {result['derived_relations']}")
                    print(f"Preserved agent nodes: {result['preserved_agent_nodes']}")
                    print(f"Preserved agent relations: {result['preserved_agent_relations']}")
                return 0
            if args.agent_command == "reset":
                result = workspace.reset()
                if args.json:
                    _print_json(result)
                else:
                    print(f"Reset agent workspace: {result['agent_storage']}")
                    print(f"Derived nodes: {result['derived_nodes']}")
                    print(f"Derived relations: {result['derived_relations']}")
                return 0
            if args.agent_command == "add":
                result = workspace.add_note(args.text)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_node(result)
                return 0
            if args.agent_command == "task":
                if args.agent_task_command == "add":
                    result = workspace.add_task(args.description)
                    if args.json:
                        _print_json(result)
                    else:
                        _print_agent_node(result)
                    return 0
                if args.agent_task_command == "done":
                    result = workspace.complete_task(args.id)
                    if args.json:
                        _print_json(result)
                    else:
                        _print_agent_node(result)
                    return 0
            if args.agent_command == "decision" and args.agent_decision_command == "add":
                result = workspace.add_decision(args.decision)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_node(result)
                return 0
            if args.agent_command == "finding" and args.agent_finding_command == "add":
                result = workspace.add_finding(args.observation)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_node(result)
                return 0
            if args.agent_command == "session" and args.agent_session_command == "start":
                result = workspace.start_session(args.title)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_node({"node": result["session"]})
                return 0
            if args.agent_command == "link":
                result = workspace.link(args.id1, args.id2, args.relation)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_relations(result)
                return 0
            if args.agent_command == "link-task":
                result = workspace.link_task(task_id=args.task_id, file_path=args.file_path, relation=args.relation)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_relations(result)
                return 0
            if args.agent_command == "link-many":
                result = workspace.link_many(args.id1, args.ids, args.relation)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_relations(result)
                return 0
            if args.agent_command == "batch":
                result = workspace.batch(_agent_batch_operations_from_args(args))
                if args.json:
                    _print_json(result)
                else:
                    for item in result["results"]:
                        if "node" in item or "task" in item:
                            _print_agent_node(item)
                        elif "relation" in item or "relations" in item:
                            _print_agent_relations(item)
                return 0
            if args.agent_command == "search":
                result = workspace.search(args.query, node_type=args.node_type, status=args.status, limit=args.limit, include_metadata=args.metadata)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_search(result)
                return 0
            if args.agent_command == "show":
                result = workspace.show(args.id)
                if args.json:
                    _print_json(result)
                else:
                    if result["kind"] == "node":
                        _print_agent_node({"node": result["node"]})
                        for edge in [*result.get("outgoing", []), *result.get("incoming", [])]:
                            print(f"{edge['id']}\t{edge['relation']}\t{edge['from_id']} -> {edge['to_id']}")
                    else:
                        edge = result["relation"]
                        print(f"{edge['id']}\t{edge['relation']}\t{edge['from_id']} -> {edge['to_id']}")
                return 0
            if args.agent_command == "list":
                result = workspace.list_items(
                    node_type=args.node_type,
                    status=args.status,
                    relation=args.relation,
                    since=args.since,
                    limit=args.limit,
                )
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_list(result)
                return 0
            if args.agent_command == "map":
                result = workspace.map(
                    task_id=args.task_id,
                    since=args.since,
                    session=args.session,
                    include_completed=args.completed,
                    include_metadata=args.metadata,
                )
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_map(result)
                return 0
            if args.agent_command == "bus":
                result = workspace.bus(limit=args.limit, include_payloads=args.include_payloads)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_bus(result)
                return 0
            if args.agent_command == "publish":
                result = workspace.publish(args.text, kind=args.kind, target=args.target)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_node({"node": result["message"]})
                return 0
            if args.agent_command == "handoff":
                result = workspace.handoff(args.summary, target=args.target)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_node({"node": result["handoff"]})
                return 0
            if args.agent_command == "export":
                result = workspace.export(include_metadata=args.metadata)
                _print_json(result)
                return 0
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.command == "storage":
        read_only = args.storage_command == "inspect"
        if profile_logger:
            profile_logger.event("storage.open.start", category="lifecycle", path=str(args.storage), read_only=read_only)
        store = BlockGraphStore(Path(args.storage), read_only=read_only)
        try:
            if args.storage_command == "inspect":
                if profile_logger:
                    with profile_logger.span("storage.inspect"):
                        payload = store.inspect_storage()
                else:
                    payload = store.inspect_storage()
                if args.json:
                    _print_json(payload)
                else:
                    _print_storage_inspection(payload)
                return 0
            if args.storage_command == "compact":
                if profile_logger:
                    with profile_logger.span("storage.compact"):
                        payload = store.compact_storage()
                else:
                    payload = store.compact_storage()
                if args.json:
                    _print_json(payload)
                else:
                    _print_storage_compaction(payload)
                return 0
        finally:
            if profile_logger:
                with profile_logger.span("storage.close"):
                    store.close()
            else:
                store.close()

    graph = _open(args, config, profile_logger=profile_logger)
    try:
        if args.command == "project":
            if args.project_command in {"compile", "update"}:
                max_file_size = _max_file_size_bytes(args, config)
                compile_kwargs = {
                    "max_file_size_bytes": max_file_size,
                    "include_patterns": config.scan.include,
                    "exclude_patterns": config.scan.exclude,
                    "cache_enabled": config.cache.enabled,
                    "parsing_options": _parsing_options(config),
                }
                if args.project_command == "compile" and args.watch:
                    print(f"Monitor mode: {Path(args.path).expanduser().resolve(strict=False)}")
                    exit_code = 0
                    try:
                        for event in graph.watch_project(
                            args.path,
                            interval_seconds=args.watch_interval,
                            debounce_seconds=args.watch_debounce,
                            max_iterations=args.watch_iterations,
                            **compile_kwargs,
                        ):
                            print(
                                f"Watch poll {event.iteration}: "
                                f"dirty={event.dirty_artifacts} deleted={event.deleted_artifacts} total={event.total_artifacts}"
                            )
                            if event.result is None:
                                print("No changes detected")
                                continue
                            _print_compile_result(event.result)
                            if event.errors:
                                exit_code = 1
                    except KeyboardInterrupt:
                        print("Watch stopped")
                        return 130
                    return exit_code
                if args.project_command == "update":
                    result = graph.update_project(args.path, **compile_kwargs)
                else:
                    result = graph.compile_project(args.path, **compile_kwargs)
                _print_compile_result(result)
                return 0 if not result.run.errors else 1
            if args.project_command == "status":
                status = graph.project_status(args.path)
                if status is None:
                    print("Project not found", file=sys.stderr)
                    return 1
                if args.json:
                    _print_json(status)
                else:
                    project_node = status["project"]
                    print(f"Project: {project_node['label']}")
                    print(f"Root: {project_node['properties'].get('root_path')}")
                    print(f"Status: {project_node['status']}")
                    print(f"Artifacts: {status['artifacts']}")
                    for artifact_type, count in sorted(status["counts_by_type"].items()):
                        print(f"  {artifact_type}: {count}")
                    if status["status_counts"]:
                        print("Statuses:")
                        for item_status, count in sorted(status["status_counts"].items()):
                            print(f"  {item_status}: {count}")
                return 0
            if args.project_command == "report":
                files = graph.project_report(args.path, output_dir=args.output or config.reporting.output_dir)
                if args.json:
                    _print_json(files.to_dict())
                else:
                    print(f"Graph report: {files.graph_report}")
                    print(f"Delta report: {files.graph_deltas}")
                    print(f"Cache report: {files.cache_report}")
                return 0

        if args.command == "cache":
            if args.cache_command == "status":
                max_file_size = _max_file_size_bytes(args, config)
                status = graph.cache_status(
                    args.path,

                    max_file_size_bytes=max_file_size,
                    include_patterns=config.scan.include,
                    exclude_patterns=config.scan.exclude,
                    cache_enabled=config.cache.enabled,
                    parsing_options=_parsing_options(config),
                )
                if args.json:
                    _print_json(status)
                else:
                    print(f"Project: {status['project']['name']}")
                    print(f"Total artifacts: {status['total_artifacts']}")
                    print(f"Cached artifacts: {status['cached_artifacts']}")
                    print(f"Dirty artifacts: {status['dirty_artifacts']}")
                    print(f"Deleted artifacts: {status['deleted_artifacts']}")
                return 0
            if args.cache_command == "clear":
                result = graph.clear_cache(args.path)
                if args.json:
                    _print_json(result)
                else:
                    print(f"Project: {result['project_id']}")
                    print(f"Cleared cache entries: {result['cleared_entries']}")
                return 0

        if args.command == "query_context":
            if args.json:
                _print_json(
                    graph.query_context_payload(
                        args.query,

                        top_k=args.top_k,
                        max_depth=args.max_depth,
                        max_items=args.max_items,
                        mode=_query_context_mode_from_args(args),
                        scopes=_query_context_scopes_from_args(args),
                        include_archived=args.include_archived,
                        include_risky=args.include_risky,
                    )
                )
            else:
                print(
                    graph.query_context(
                        args.query,

                        top_k=args.top_k,
                        max_depth=args.max_depth,
                        max_items=args.max_items,
                        mode=_query_context_mode_from_args(args),
                        scopes=_query_context_scopes_from_args(args),
                        include_archived=args.include_archived,
                        include_risky=args.include_risky,
                    )
                )
            return 0

        if args.command == "query_explore":
            try:
                result = graph.query_explore(
                    args.query,

                    views=_query_explore_views_from_args(args),
                    top_k=args.top_k,
                    max_depth=args.max_depth,
                    limit=args.limit,
                    max_items=args.max_items,
                    include_archived=args.include_archived,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if args.json:
                _print_json(result)
            else:
                print(result["context"])
            return 0

        if args.command == "query_graph":
            result = graph.query_graph(
                args.query,

                top_k=args.top_k,
                max_depth=args.max_depth,
                max_nodes=args.max_nodes,
                max_edges=args.max_edges,
                max_sources=args.max_sources,
                max_items=args.max_items,
                filter_generic=not args.no_filter_generic,
                include_archived=args.include_archived,
            )
            if args.json:
                _print_json(result)
            else:
                print(result["context"])
            return 0

        if args.command == "query_memories":
            payload = graph.query_memories_payload(
                args.query,

                top_k=args.top_k,
                max_depth=args.max_depth,
                limit=args.limit,
                include_sources=not args.no_sources,
                filter_generic=not args.no_filter_generic,
                max_text_chars=args.max_text_chars,
                include_archived=args.include_archived,
            )
            memories = payload["memories"]
            if args.json:
                _print_json(payload)
            else:
                for item in memories:
                    print(f"{float(item['score']):.3f}\t{item['type']}\t{item['id']}\t{item['text']}")
            return 0

        if args.command == "query":
            statement = _normalize_reql_statement_arg(args.statement)
            if not statement:
                print("REQL statement required as positional argument", file=sys.stderr)
                return 2
            result = graph.query(statement)
            if args.json:
                _print_json(result.to_dict())
            else:
                print(result.to_table())
            return 0

        if args.command == "stats":
            by_type = graph.store.node_type_counts()
            payload = {
                "nodes": graph.store.count_nodes(),
                "edges": graph.store.count_edges(),
                "node_types": by_type,
            }
            if args.json:
                _print_json(payload)
            else:
                print(f"Nodes: {payload['nodes']}")
                print(f"Edges: {payload['edges']}")
                for k, v in sorted(by_type.items()):
                    print(f"  {k}: {v}")
            return 0

        if args.command == "export":
            payload = graph.export_json()
            if args.html:
                html_path = write_graph_html(payload, _graph_html_path(args.out))
                print(html_path)
                if args.json:
                    json_path = html_path.with_name("graph.json")
                    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json_path)
            else:
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                if args.json:
                    json_path = _graph_json_path(args.out)
                    json_path.parent.mkdir(parents=True, exist_ok=True)
                    json_path.write_text(text, encoding="utf-8")
                    print(json_path)
                elif args.out:
                    Path(args.out).write_text(text, encoding="utf-8")
                    print(args.out)
                else:
                    print(text)
            return 0

        if args.command == "inspect":
            result = graph.inspect_node(args.node_id, limit=args.limit)
            if not result["found"]:
                print("Node not found", file=sys.stderr)
                return 2
            _print_json(result)
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2
    finally:
        graph.close()


if __name__ == "__main__":
    raise SystemExit(main())
