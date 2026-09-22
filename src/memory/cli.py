"""Command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Any

from api.memory_graph import MemoryGraph

from .agent.progress import ProgressingAgentWorkspace
from .artifacts.options import CompilationOptions
from .config import (
    PROJECT_CONFIG_FILENAME,
    ConfigError,
    REQLConfig,
    load_effective_config,
    resolve_config_path,
    set_local_config_option,
    write_sample_config,
)
from .diagnostics import PerformanceLogger
from .domain.exceptions import StorageError
from .domain.query_context import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_ITEMS,
    DEFAULT_TOP_K,
    QueryContextRequest,
)
from .freshness import write_watch_state
from .reporting.html_graph import write_graph_html
from .reporting.project_pipeline import write_pipeline_html, write_pipeline_mermaid
from .storage import BlockGraphStore, StoreLease, inspect_store_locks
from .storage.maintenance import clear_project_storage

DEFAULT_STORAGE_DIR = ".reql"
DEFAULT_STORAGE_FILE = "memory.reql"
LOCKED_FOR_WRITE_PREFIX = "REQL block store is locked for write: "


class _PromptInterrupted(Exception):
    pass


@dataclass(frozen=True)
class _AgentCommandResolution:
    platforms: tuple[str, ...]
    project: bool
    project_dir: Path
    home_dir: Path | None


class AccessMode(str, Enum):
    """Storage access required by a CLI command."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"


@dataclass(frozen=True)
class CommandContext:
    """Runtime dependencies passed to declarative command handlers."""

    args: argparse.Namespace
    config: REQLConfig
    graph: MemoryGraph
    profile_logger: PerformanceLogger | None = None


@dataclass(frozen=True)
class CommandSpec:
    """Single source of truth for a leaf CLI command."""

    path: tuple[str, ...]
    access: AccessMode | Callable[[argparse.Namespace], AccessMode]
    help: str
    configure_parser: Callable[[argparse.ArgumentParser], None]
    handler: Callable[[CommandContext], int]

    def __post_init__(self) -> None:
        if not self.path or any(not part for part in self.path):
            raise ValueError("CommandSpec.path must contain non-empty command names")

    def access_mode(self, args: argparse.Namespace) -> AccessMode:
        return self.access if isinstance(self.access, AccessMode) else self.access(args)


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


def _format_storage_error(error: StorageError) -> str:
    message = str(error)
    if message.startswith(LOCKED_FOR_WRITE_PREFIX):
        return (
            "reql is locked for write: to fix any possible stale: "
            "reql storage locks --recover-stale"
        )
    return f"reql: {message}"


def _agent_command_name(args: argparse.Namespace) -> str:
    """Return the selected agent command path without its argument values."""

    parts = ["agent", str(getattr(args, "agent_command", "")).strip()]
    if args.agent_command == "task":
        parts.append(str(getattr(args, "agent_task_command", "")).strip())
    return " ".join(part for part in parts if part)


def _agent_identity_error_payload(error: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Describe an ambiguous private-workspace selection and its required action."""

    command_name = _agent_command_name(args)
    action = command_name.removeprefix("agent ")
    agent_ids = list(getattr(error, "agent_ids", ()))
    required_action = f'Target an existing workspace: reql agent --agent "agent:AGENT_ID" {action}'
    return {
        "code": "ambiguous_agent_identity",
        "message": "This command needs one private Agent Workspace, but no agent or activity identity was supplied.",
        "context": {"active_agent_ids": agent_ids, "command": f"reql {command_name}"},
        "next": [required_action],
    }


def _print_agent_identity_error(error: Any, args: argparse.Namespace) -> None:
    """Render an ambiguous identity error as guidance rather than a traceback."""

    payload = _agent_identity_error_payload(error, args)
    if getattr(args, "json", False):
        _print_json({"error": payload})
        return
    context = payload["context"]
    agents = context["active_agent_ids"]
    print(f"Cannot select a private Agent Workspace for {context['command']}.", file=sys.stderr)
    print(f"Why: {payload['message']}", file=sys.stderr)
    print(f"Context: active agents: {', '.join(agents) if agents else 'unknown'}.", file=sys.stderr)
    print(f"Action: {payload['next'][0]}", file=sys.stderr)


def _print_agent_command_error(error: ValueError, args: argparse.Namespace) -> None:
    """Render an agent command validation error with its focused help command."""

    command = f"reql {_agent_command_name(args)}"
    if getattr(args, "json", False):
        _print_json(
            {
                "error": {
                    "code": "invalid_agent_command",
                    "message": str(error),
                    "context": {"command": command},
                    "next": [f"Read command help: {command} --help"],
                }
            }
        )
        return
    print(f"Cannot complete `{command}`: {error}", file=sys.stderr)
    print(f"Next: read command help: {command} --help", file=sys.stderr)


def _agent_progress_label(args: argparse.Namespace) -> str:
    parts = ["agent", str(args.agent_command)]
    for field in ("agent_task_command",):
        value = getattr(args, field, None)
        if value:
            parts.append(str(value))
            break
    return " ".join(parts)


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
    if result.revision is not None:
        print(f"Revision: {result.revision.id} ({len(result.revision.changes)} file changes)")
    retention = result.retention
    if retention is not None and (retention.records_removed or retention.usage_entries_removed):
        print(
            "Retention: "
            f"records={retention.records_removed}, "
            f"usage_entries={retention.usage_entries_removed}, "
            f"bytes_reclaimed={retention.bytes_reclaimed}"
        )
    _print_compile_summary(result.summary)
    if run.errors:
        print("Errors:")
        for error in run.errors:
            print(f"  {error}")


def _print_compile_summary(summary: Any, *, limit: int = 20) -> None:
    changed_files = summary.changed_files
    changed_symbols = summary.updated_symbols
    associated_tests = summary.associated_tests
    print("Summary:")
    print(f"  Changed files ({len(changed_files)}):")
    for item in changed_files[:limit]:
        print(f"    {str(item.get('status') or 'changed'):8} {item.get('path')}")
    if len(changed_files) > limit:
        print(f"    ... {len(changed_files) - limit} more")
    print(f"  Changed symbols ({len(changed_symbols)}):")
    for item in changed_symbols[:limit]:
        location = item.relative_path
        if item.line_start is not None:
            location = f"{location}:{item.line_start}"
        print(f"    {item.status:8} {item.type} {item.name} @ {location}")
    if len(changed_symbols) > limit:
        print(f"    ... {len(changed_symbols) - limit} more")
    print(f"  Associated tests ({len(associated_tests)}):")
    for item in associated_tests[:limit]:
        print(f"    {item.path} ({item.reason})")
    if len(associated_tests) > limit:
        print(f"    ... {len(associated_tests) - limit} more")


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


def _print_storage_locks(payload: dict[str, Any]) -> None:
    print(f"Path: {payload['path']}")
    print(f"Locked: {payload['locked']}")
    locks = [payload["writer"]] if payload.get("writer") else []
    locks.extend(payload.get("readers") or [])
    for item in locks:
        alive = item.get("process_alive")
        alive_text = "unknown" if alive is None else str(bool(alive)).lower()
        print(
            f"  {item['mode']}: command={item.get('command') or 'unknown'}; "
            f"pid={item.get('pid')}; duration={float(item.get('duration_seconds', 0.0)):.3f}s; "
            f"alive={alive_text}; watcher={str(bool(item.get('watcher'))).lower()}; "
            f"stale={str(bool(item.get('stale'))).lower()}"
        )
    for item in payload.get("recovered") or []:
        print(f"Recovered stale {item['mode']} lock: {item['lock_path']}")


def _project_watch_status(storage_path: str | Path, project_path: str | Path) -> dict[str, Any]:
    storage = Path(storage_path).expanduser().resolve(strict=False)
    lease_target = storage.with_name(f"{storage.name}.watch")
    locks = inspect_store_locks(lease_target)
    writer = locks.get("writer")
    watcher = writer if isinstance(writer, dict) else None
    canonical_writer = inspect_store_locks(storage).get("writer")
    process_alive = watcher.get("process_alive") if watcher is not None else None
    stale = bool(watcher.get("stale")) if watcher is not None else False
    if watcher is None:
        status = "stopped"
        running: bool | None = False
    elif stale or process_alive is False:
        status = "stale"
        running = False
    elif process_alive is True:
        status = "running"
        running = True
    else:
        status = "unknown"
        running = None
    return {
        "status": status,
        "running": running,
        "project_path": str(Path(project_path).expanduser().resolve(strict=False)),
        "storage_path": str(storage),
        "pid": watcher.get("pid") if watcher is not None else None,
        "host": watcher.get("host") if watcher is not None else None,
        "process_alive": process_alive,
        "started_at": watcher.get("created_at") if watcher is not None else None,
        "duration_seconds": watcher.get("duration_seconds") if watcher is not None else None,
        "command": watcher.get("command") if watcher is not None else None,
        "stale": stale,
        "blocked_by_other_writer": canonical_writer is not None,
    }


def _print_project_watch_status(payload: dict[str, Any]) -> None:
    print(f"Watcher: {payload['status']}")
    print(f"Project: {payload['project_path']}")
    print(f"Storage: {payload['storage_path']}")
    if payload.get("pid") is not None:
        print(f"PID: {payload['pid']}")
        print(f"Process alive: {payload.get('process_alive')}")
        print(f"Started: {payload.get('started_at') or 'unknown'}")
        print(f"Duration: {float(payload.get('duration_seconds') or 0.0):.3f}s")
        print(f"Command: {payload.get('command') or 'unknown'}")
    elif payload.get("blocked_by_other_writer"):
        print("Writer active: yes (not a watcher)")


def _print_storage_compaction(payload: dict[str, Any]) -> None:
    print(f"Compacted: {payload['path']}")
    print(f"Generation: {payload['generation_id_before']} -> {payload['generation_id_after']}")
    print(f"Blocks: {payload['blocks_before']} -> {payload['blocks_after']}")
    print(f"Records: {payload['records_before']} -> {payload['records_after']}")
    print(f"Bytes: {payload['bytes_before']} -> {payload['bytes_after']}")
    print(f"Bytes reclaimed: {payload['bytes_reclaimed']}")


def _print_storage_clear(payload: dict[str, Any]) -> None:
    print(f"Cleared and rebuilt: {payload['path']}")
    print(f"Project: {payload['project_path']}")
    print(f"Files compiled: {payload['files_changed']} / {payload['files_seen']}")
    print(f"Nodes: {payload['nodes_after']} (archived: {payload['archived_nodes_after']})")
    print(f"Edges: {payload['edges_after']}")
    print(f"Bytes: {payload['bytes_before']} -> {payload['bytes_after']}")
    print(f"Bytes reclaimed: {payload['bytes_reclaimed']}")


def _print_agent_status(payload: dict[str, Any]) -> None:
    print(f"Agent memory: {'initialized' if payload['exists'] else 'not initialized'}")
    print(f"Agent id: {payload.get('agent_id') or ''}")
    print(f"Identity selection: {payload.get('selection_source') or 'unknown'}")
    print(f"Concurrency safe: {str(bool(payload.get('concurrency_safe'))).lower()}")
    print(f"Agent storage: {payload['agent_storage']}")
    print(f"Public dashboard: {payload.get('dashboard_storage') or ''}")
    if payload.get("initialized_at"):
        print(f"Initialized at: {payload['initialized_at']}")
    print(f"Items: {payload['agent_nodes']}")
    if payload.get("current_session_id"):
        open_tasks = int(payload.get("current_session_open_tasks") or 0)
        title = payload.get("current_session_title") or ""
        if payload.get("current_session_is_idle"):
            print(f"Last session: {payload['current_session_id']} ({title}; idle, open_tasks=0)")
        else:
            print(f"Current session: {payload['current_session_id']} ({title}; open_tasks={open_tasks})")


def _print_agent_node(payload: dict[str, Any]) -> None:
    node = payload.get("node") or payload.get("task") or payload.get("context") or payload
    node_type = node.get("type") or node.get("message_type") or ""
    print(f"{node['id']}\t{node_type}\t{node.get('status') or ''}\t{node.get('title') or node.get('content') or ''}")


def _print_agent_dashboard(payload: dict[str, Any]) -> None:
    """Render the public dashboard followed by the selected private dashboard."""
    public = payload.get("public", payload)
    private = payload.get("private")
    print("Agents:")
    for agent in public.get("agents") or []:
        print(f"  {agent.get('agent_id')}\t{agent.get('status')}\t{agent.get('last_activity_at') or '-'}")
    print("Active Tasks:")
    for task in public.get("active_tasks") or []:
        print(f"  {task.get('agent_id')}\t{task.get('content')}\t{task.get('updated_at') or '-'}")
    print("Context:")
    for entry in public.get("context") or []:
        print(f"  {entry.get('timestamp')}\t{entry.get('agent_id')}\t{entry.get('message_type')}\t{entry.get('content')}")
    print("Drill:")
    for item in public.get("drill") or []:
        print(f"  {item.get('label')}: {item.get('command')}")
    if private is not None:
        print(f"Private dashboard: {private.get('agent', {}).get('agent_id')}")
        for section, label in (("tasks", "Tasks"), ("private_notes", "Private Notes"), ("external_notes", "External Notes")):
            print(f"{label}:")
            for item in private.get(section) or []:
                if section == "tasks":
                    completion = item.get("completion_message")
                    suffix = f"\t{completion}" if completion else ""
                    print(
                        f"  {item.get('updated_at')}\t{item.get('id')}\t{item.get('status')}\t"
                        f"{item.get('title') or item.get('content')}{suffix}"
                    )
                else:
                    print(f"  {item.get('updated_at')}\t{item.get('title') or item.get('content')}")


def _configure_project_explain_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--focus", default=None, help="Feature, behavior, or business concept used to rank change guidance")
    parser.add_argument("--max-capabilities", type=int, default=12, help="Maximum business capabilities to return")
    parser.add_argument("--max-workflows", type=int, default=8, help="Maximum inferred workflows to return")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _handle_project_explain(context: CommandContext) -> int:
    args = context.args
    try:
        explanation = context.graph.explain_project(
            args.path,
            focus=args.focus,
            max_capabilities=args.max_capabilities,
            max_workflows=args.max_workflows,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        _print_json(explanation.to_dict())
    else:
        print(explanation.to_markdown())
    return 0


def _configure_project_pipeline_parser(parser: argparse.ArgumentParser) -> None:
    formats = parser.add_mutually_exclusive_group()
    formats.add_argument("--code", action="store_true", help="Write Mermaid source to pipeline.mmd")
    formats.add_argument("--html", action="store_true", help="Write an interactive pipeline.html visualization (default)")
    parser.add_argument("--out", default=None, help="Output file or directory; defaults to the registered project root")


def _handle_project_pipeline(context: CommandContext) -> int:
    args = context.args
    try:
        pipeline = context.graph.project_pipeline(args.path)
        output_format = "mermaid" if args.code else "html"
        output_path = _project_pipeline_output_path(
            args.out,
            project_root=str(pipeline.project.get("root_path") or args.path),
            output_format=output_format,
        )
        if output_format == "mermaid":
            written = write_pipeline_mermaid(pipeline, output_path)
        else:
            written = write_pipeline_html(pipeline, output_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Cannot write project pipeline: {exc}", file=sys.stderr)
        return 1
    print(written)
    return 0


def _configure_project_compile_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-file-size-mb", type=float, default=None)
    parser.add_argument("--watch", action="store_true", help="Monitor the project filesystem and compile dirty artifacts automatically")
    parser.add_argument("--watch-interval", type=float, default=0.5, help="Maximum seconds to wait between bounded watchdog checks")
    parser.add_argument("--watch-debounce", type=float, default=0.1, help="Seconds to wait before compiling detected changes")
    parser.add_argument("--watch-iterations", type=int, default=None, help="Stop after this many watch checks; default is until interrupted")


def _configure_project_status_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _configure_project_history_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=20, help="Maximum revisions to show")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _configure_project_diff_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--revision", default=None, help="Revision id; defaults to the latest project revision")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _configure_project_report_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", default=None, help="Output directory for GRAPH_REPORT.md, GRAPH_DELTAS.md, and CACHE_REPORT.md")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _configure_cache_status_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-file-size-mb", type=float, default=None)
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _configure_cache_clear_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _configure_locate_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", help="Exact relative path; known document extensions may be omitted")
    parser.add_argument("--include-archived", action="store_true", help="Include archived or deleted artifacts")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _configure_stats_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def _configure_export_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", default=None, help="Optional output file or directory")
    parser.add_argument("--html", action="store_true", help="Write an interactive standalone graph.html visualization")
    parser.add_argument("--json", action="store_true", help="Write graph JSON to a file")


def _configure_inspect_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _handle_project_compile(context: CommandContext) -> int:
    args = context.args
    config = context.config
    graph = context.graph
    max_file_size = _max_file_size_bytes(args, config)
    compile_kwargs = {
        "max_file_size_bytes": max_file_size,
        "include_patterns": config.scan.include,
        "exclude_patterns": config.scan.exclude,
        "config_path": _effective_config_path(args),
        "cache_enabled": config.cache.enabled,
        "parsing_options": CompilationOptions.from_config(config),
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
    result = graph.compile_project(args.path, **compile_kwargs)
    _print_compile_result(result)
    return 0 if not result.run.errors else 1


def _handle_detached_project_watch(
    args: argparse.Namespace,
    config: REQLConfig,
    profile_logger: PerformanceLogger | None,
) -> int:
    """Watch without retaining the canonical graph writer lock while idle."""

    from .services.project_watch import Observer, _WatchdogChangeHandler

    if Observer is None:
        raise RuntimeError("watchdog is required for monitor mode; install the watchdog package")
    root = Path(args.path).expanduser().resolve(strict=False)
    storage = Path(args.storage).expanduser().resolve(strict=False)
    lease_target = storage.with_name(f"{storage.name}.watch")
    changed = Event()
    ignored = (root / ".reql",)

    def mark_stale() -> None:
        write_watch_state(storage, status="stale", pending_paths=1)

    observer = Observer()
    observer.schedule(_WatchdogChangeHandler(changed, ignored_paths=ignored, on_change=mark_stale), str(root), recursive=True)
    print(f"Monitor mode: {root}")
    exit_code = 0
    with StoreLease(lease_target, timeout_seconds=0.0):
        observer.start()
        try:
            iteration = 0
            while args.watch_iterations is None or iteration < args.watch_iterations:
                if iteration:
                    timeout = args.watch_interval if args.watch_iterations is not None else None
                    observed = changed.wait(timeout)
                    if not observed and args.watch_iterations is None:
                        continue
                    if observed:
                        changed.clear()
                        if args.watch_debounce:
                            changed.wait(args.watch_debounce)
                            changed.clear()
                iteration += 1
                write_watch_state(storage, status="refreshing", pending_paths=1)
                graph = MemoryGraph.open(storage, config=config, profile_logger=profile_logger, defer_lexical_index=True)
                try:
                    result = graph.compile_project(
                        root,
                        max_file_size_bytes=_max_file_size_bytes(args, config),
                        include_patterns=config.scan.include,
                        exclude_patterns=config.scan.exclude,
                        config_path=_effective_config_path(args),
                        cache_enabled=config.cache.enabled,
                        parsing_options=CompilationOptions.from_config(config),
                    )
                    committed_revision = result.revision or graph.revisions.latest(result.scan.project.id)
                finally:
                    graph.close()
                if result.run.errors:
                    exit_code = 1
                revision_id = committed_revision.id if committed_revision is not None else None
                pending_after_compile = changed.is_set()
                write_watch_state(
                    storage,
                    status="stale" if result.run.errors or pending_after_compile else "current",
                    pending_paths=(len(result.dirty_set.changed_artifact_ids) or 1) if result.run.errors or pending_after_compile else 0,
                    source_revision_id=revision_id,
                )
                print(
                    f"Watch poll {iteration}: dirty={len(result.dirty_set.changed_artifact_ids)} "
                    f"deleted={len(result.dirty_set.deleted_artifact_ids)} total={len(result.scan.artifacts)}"
                )
                _print_compile_result(result)
        finally:
            observer.stop()
            observer.join(timeout=5)
    return exit_code


def _handle_project_status(context: CommandContext) -> int:
    args = context.args
    status = context.graph.project_status(args.path)
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


def _handle_project_history(context: CommandContext) -> int:
    args = context.args
    graph = context.graph
    if graph.project_status(args.path) is None:
        print("Project not found", file=sys.stderr)
        return 1
    revisions = graph.project_history(args.path, limit=max(0, args.limit))
    if args.json:
        _print_json([revision.to_dict(include_manifest=False) for revision in revisions])
    elif not revisions:
        print("No project revisions")
    else:
        for revision in revisions:
            print(
                f"{revision.id}\t{revision.created_at}\t"
                f"files={len(revision.changes)}\tparent={revision.parent_id or '-'}"
            )
    return 0


def _handle_project_diff(context: CommandContext) -> int:
    args = context.args
    graph = context.graph
    status = graph.project_status(args.path)
    if status is None:
        print("Project not found", file=sys.stderr)
        return 1
    if args.revision:
        revision = graph.project_revision(args.revision)
    else:
        history = graph.project_history(args.path, limit=1)
        revision = history[0] if history else None
    project_id = str(status["project"]["id"])
    if revision is None or revision.project_id != project_id:
        print("Revision not found", file=sys.stderr)
        return 1
    if args.json:
        _print_json(revision.to_dict(include_manifest=False))
    else:
        print(f"Revision: {revision.id}")
        print(f"Parent: {revision.parent_id or '-'}")
        print(f"Tree: {revision.tree_hash}")
        for change in revision.changes:
            before = (change.old_sha256 or "-")[:12]
            after = (change.new_sha256 or "-")[:12]
            print(f"{change.status[0].upper()}\t{change.path}\t{before} -> {after}")
    return 0


def _handle_project_report(context: CommandContext) -> int:
    args = context.args
    files = context.graph.project_report(args.path, output_dir=args.output or context.config.reporting.output_dir)
    if args.json:
        _print_json(files.to_dict())
    else:
        print(f"Graph report: {files.graph_report}")
        print(f"Delta report: {files.graph_deltas}")
        print(f"Cache report: {files.cache_report}")
    return 0


def _handle_cache_status(context: CommandContext) -> int:
    args = context.args
    config = context.config
    status = context.graph.cache_status(
        args.path,
        max_file_size_bytes=_max_file_size_bytes(args, config),
        include_patterns=config.scan.include,
        exclude_patterns=config.scan.exclude,
        config_path=_effective_config_path(args),
        cache_enabled=config.cache.enabled,
        parsing_options=CompilationOptions.from_config(config),
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


def _handle_cache_clear(context: CommandContext) -> int:
    args = context.args
    result = context.graph.clear_cache(args.path)
    if args.json:
        _print_json(result)
    else:
        print(f"Project: {result['project_id']}")
        print(f"Cleared cache entries: {result['cleared_entries']}")
    return 0


def _handle_query_context(context: CommandContext) -> int:
    args = context.args
    try:
        request = QueryContextRequest.from_raw(
            text=args.query,
            top_k=args.top_k,
            max_depth=args.max_depth,
            max_items=args.max_items,
            mode=_query_context_mode_from_args(args),
            scopes=_query_context_scopes_from_args(args),
            include_archived=args.include_archived,
        )
        result = context.graph.query_context_result(request)
    except (TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        _print_json(result.to_dict())
    else:
        print(context.graph.query_context_service.render(result))
    return 0


def _handle_query_explore(context: CommandContext) -> int:
    args = context.args
    try:
        result = context.graph.query_explore(
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


def _handle_query_graph(context: CommandContext) -> int:
    args = context.args
    result = context.graph.query_graph(
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


def _handle_query_memories(context: CommandContext) -> int:
    args = context.args
    payload = context.graph.query_memories_payload(
        args.query,
        top_k=args.top_k,
        max_depth=args.max_depth,
        limit=args.limit,
        include_sources=not args.no_sources,
        filter_generic=not args.no_filter_generic,
        max_text_chars=args.max_text_chars,
        include_archived=args.include_archived,
    )
    if args.json:
        _print_json(payload)
    else:
        for item in payload["memories"]:
            print(f"{float(item['score']):.3f}\t{item['type']}\t{item['id']}\t{item['text']}")
    return 0


_MUTATING_REQL_COMMANDS = {"COMMUNITIES", "HUBS"}


def _query_access_mode(args: argparse.Namespace) -> AccessMode:
    statement = _normalize_reql_statement_arg(getattr(args, "statement", None))
    first = statement.split(None, 1)[0].rstrip(";").upper() if statement else ""
    return AccessMode.MUTATING if first in _MUTATING_REQL_COMMANDS else AccessMode.READ_ONLY


def _handle_query(context: CommandContext) -> int:
    args = context.args
    statement = _normalize_reql_statement_arg(args.statement)
    if not statement:
        print("REQL statement required as positional argument", file=sys.stderr)
        return 2
    result = context.graph.query(statement)
    if args.json:
        _print_json(result.to_dict())
    else:
        print(result.to_table())
    return 0


def _handle_locate(context: CommandContext) -> int:
    args = context.args
    payload = context.graph.locate(args.path, include_archived=args.include_archived)
    if args.json:
        _print_json(payload)
    else:
        for match in payload["matches"]:
            print(f"{match['relative_path']}\t{match['artifact_type']}\t{match['id']}")
    if not payload["matches"]:
        if not args.json:
            print(f"Path not found: {args.path}", file=sys.stderr)
        return 1
    return 0


def _handle_stats(context: CommandContext) -> int:
    graph = context.graph
    by_type = graph.store.node_type_counts()
    payload = {
        "nodes": graph.store.count_nodes(),
        "edges": graph.store.count_edges(),
        "node_types": by_type,
    }
    if context.args.json:
        _print_json(payload)
    else:
        print(f"Nodes: {payload['nodes']}")
        print(f"Edges: {payload['edges']}")
        for key, value in sorted(by_type.items()):
            print(f"  {key}: {value}")
    return 0


def _handle_export(context: CommandContext) -> int:
    args = context.args
    payload = context.graph.export_json()
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


def _handle_inspect(context: CommandContext) -> int:
    args = context.args
    result = context.graph.inspect_node(args.node_id, limit=args.limit)
    if not result["found"]:
        print("Node not found", file=sys.stderr)
        return 2
    _print_json(result)
    return 0



def _configure_declared_commands(
    subparsers_by_parent: dict[tuple[str, ...], argparse._SubParsersAction],
) -> None:
    for spec in COMMAND_SPECS:
        parent_path = spec.path[:-1]
        try:
            subparsers = subparsers_by_parent[parent_path]
        except KeyError as exc:
            rendered = " ".join(parent_path) or "<root>"
            raise ValueError(f"No parser group registered for declarative command parent: {rendered}") from exc
        command_parser = subparsers.add_parser(spec.path[-1], help=spec.help)
        spec.configure_parser(command_parser)
        command_parser.set_defaults(_command_spec_path=spec.path)


def _selected_command_spec(args: argparse.Namespace) -> CommandSpec | None:
    raw_path = getattr(args, "_command_spec_path", None)
    return _COMMAND_SPECS_BY_PATH.get(tuple(raw_path)) if raw_path is not None else None


def _command_access_mode(args: argparse.Namespace) -> AccessMode:
    spec = _selected_command_spec(args)
    if spec is None:
        raise ValueError("Graph-backed command is missing a CommandSpec")
    return spec.access_mode(args)


def _open(args: argparse.Namespace, config: REQLConfig, profile_logger: PerformanceLogger | None = None) -> MemoryGraph:
    read_only_command = _command_access_mode(args) is AccessMode.READ_ONLY
    defer_lexical_index = (
        str(getattr(args, "command", "")) == "project"
        and str(getattr(args, "project_command", "")) == "compile"
    )
    if read_only_command:
        if profile_logger:
            profile_logger.event("storage.open.start", category="lifecycle", path=str(args.storage), read_only=True)
            try:
                with profile_logger.span("storage.open", path=str(args.storage), read_only=True):
                    return MemoryGraph.open(Path(args.storage), config=config, profile_logger=profile_logger, read_only=True, lock_timeout_seconds=0.05)
            except StorageError as exc:
                if "missing REQL storage" not in str(exc):
                    raise
                profile_logger.event("storage.open.read_only_unavailable", category="lifecycle", reason=str(exc))
                with profile_logger.span("storage.open", path=str(args.storage), read_only=False):
                    graph = MemoryGraph.open(Path(args.storage), config=config, profile_logger=profile_logger)
                _checkpoint_opened_store_if_needed(graph, profile_logger)
                return graph
        try:
            return MemoryGraph.open(Path(args.storage), config=config, read_only=True, lock_timeout_seconds=0.05)
        except StorageError as exc:
            if "missing REQL storage" not in str(exc):
                raise
            graph = MemoryGraph.open(Path(args.storage), config=config)
            _checkpoint_opened_store_if_needed(graph, None)
            return graph
    if profile_logger:
        profile_logger.event("storage.open.start", category="lifecycle", path=str(args.storage), read_only=False)
        with profile_logger.span("storage.open", path=str(args.storage), read_only=False):
            graph = MemoryGraph.open(
                Path(args.storage),
                config=config,
                profile_logger=profile_logger,
                defer_lexical_index=defer_lexical_index,
            )
        _checkpoint_opened_store_if_needed(graph, profile_logger)
        return graph
    graph = MemoryGraph.open(Path(args.storage), config=config, defer_lexical_index=defer_lexical_index)
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
    build_path: str | Path = "."
    if getattr(args, "command", None) in {"project", "cache"} or (
        getattr(args, "command", None) == "storage" and getattr(args, "storage_command", None) == "clear"
    ):
        build_path = getattr(args, "path", ".")
    return str(_default_storage_path(build_path))


def _config_start_dir(args: argparse.Namespace) -> str | Path | None:
    if getattr(args, "command", None) in {"project", "cache"} or (
        getattr(args, "command", None) == "storage" and getattr(args, "storage_command", None) == "clear"
    ):
        return getattr(args, "path", None)
    return None


def _effective_config_path(args: argparse.Namespace) -> Path | None:
    return resolve_config_path(
        None,
        start_dir=_config_start_dir(args),
        env=os.environ,
    )


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
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS, help="Maximum rendered context items")
    parser.add_argument("--include-archived", action="store_true", help="Include archived graph records")
    parser.add_argument("--cleanup", action="store_true", help="Return only cleanup findings matching the query")
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
        choices=["all", "owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "structural_duplicates", "code"],
        help="Explore view to include; may be repeated. Defaults to all views.",
    )
    parser.add_argument("--owners-only", action="store_true", help="Shortcut for --view owners")
    parser.add_argument("--callers-only", action="store_true", help="Shortcut for --view callers")
    parser.add_argument("--public-surface-only", action="store_true", help="Shortcut for --view public_surface")
    parser.add_argument("--serialization-paths-only", action="store_true", help="Shortcut for --view serialization_paths")
    parser.add_argument("--docs-mentions-only", action="store_true", help="Shortcut for --view docs_mentions")
    parser.add_argument("--structural-duplicates-only", action="store_true", help="Shortcut for --view structural_duplicates")
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
        ("structural_duplicates_only", "structural_duplicates"),
        ("code_only", "code"),
    ]
    selected = [view for attr, view in shortcuts if bool(getattr(args, attr, False))]
    return selected or list(args.view or []) or None


def _query_context_mode_from_args(args: argparse.Namespace) -> str:
    return "cleanup" if bool(getattr(args, "cleanup", False)) else "informative"


def _query_context_scopes_from_args(args: argparse.Namespace) -> list[str] | None:
    scopes = [scope for attr, scope in (("code", "code"), ("docs", "docs"), ("test", "test")) if bool(getattr(args, attr, False))]
    return scopes or None


def _add_reql_statement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("statement", nargs="*", help="REQL statement")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result")


def _add_agent_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", dest="node_type", default=None, help="Filter by agent node type")
    parser.add_argument("--status", default=None, help="Filter by node status")
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


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        path=("project", "compile"),
        access=AccessMode.MUTATING,
        help="Scan and incrementally compile dirty artifacts",
        configure_parser=_configure_project_compile_parser,
        handler=_handle_project_compile,
    ),
    CommandSpec(
        path=("project", "status"),
        access=AccessMode.READ_ONLY,
        help="Show registered project artifact status",
        configure_parser=_configure_project_status_parser,
        handler=_handle_project_status,
    ),
    CommandSpec(
        path=("project", "explain"),
        access=AccessMode.READ_ONLY,
        help="Explain repository capabilities, architecture, workflows, and change starting points",
        configure_parser=_configure_project_explain_parser,
        handler=_handle_project_explain,
    ),
    CommandSpec(
        path=("project", "pipeline"),
        access=AccessMode.MUTATING,
        help="Export all detected project flows as Mermaid or interactive HTML",
        configure_parser=_configure_project_pipeline_parser,
        handler=_handle_project_pipeline,
    ),
    CommandSpec(
        path=("project", "history"),
        access=AccessMode.READ_ONLY,
        help="Show newest-first content-addressed project revisions",
        configure_parser=_configure_project_history_parser,
        handler=_handle_project_history,
    ),
    CommandSpec(
        path=("project", "diff"),
        access=AccessMode.READ_ONLY,
        help="Show file changes in a revision; defaults to the latest revision",
        configure_parser=_configure_project_diff_parser,
        handler=_handle_project_diff,
    ),
    CommandSpec(
        path=("project", "report"),
        access=AccessMode.MUTATING,
        help="Write project Markdown reports",
        configure_parser=_configure_project_report_parser,
        handler=_handle_project_report,
    ),
    CommandSpec(
        path=("cache", "status"),
        access=AccessMode.MUTATING,
        help="Show incremental cache status for a project path",
        configure_parser=_configure_cache_status_parser,
        handler=_handle_cache_status,
    ),
    CommandSpec(
        path=("cache", "clear"),
        access=AccessMode.MUTATING,
        help="Archive cache metadata for a project path",
        configure_parser=_configure_cache_clear_parser,
        handler=_handle_cache_clear,
    ),
    CommandSpec(
        path=("query_context",),
        access=AccessMode.READ_ONLY,
        help="Compose a deterministic context block for a query",
        configure_parser=_add_query_context_arguments,
        handler=_handle_query_context,
    ),
    CommandSpec(
        path=("query_explore",),
        access=AccessMode.READ_ONLY,
        help="Explore owners, callers, public surface, serialization paths, docs, and code",
        configure_parser=_add_query_explore_arguments,
        handler=_handle_query_explore,
    ),
    CommandSpec(
        path=("query_graph",),
        access=AccessMode.READ_ONLY,
        help="Retrieve a structured query-centered subgraph",
        configure_parser=_add_query_graph_arguments,
        handler=_handle_query_graph,
    ),
    CommandSpec(
        path=("query_memories",),
        access=AccessMode.READ_ONLY,
        help="Retrieve relevant memory texts for a query",
        configure_parser=_add_query_memories_arguments,
        handler=_handle_query_memories,
    ),
    CommandSpec(
        path=("query",),
        access=_query_access_mode,
        help="Execute a REQL statement",
        configure_parser=_add_reql_statement_arguments,
        handler=_handle_query,
    ),
    CommandSpec(
        path=("locate",),
        access=AccessMode.READ_ONLY,
        help="Resolve a known project-relative path without semantic ranking",
        configure_parser=_configure_locate_parser,
        handler=_handle_locate,
    ),
    CommandSpec(
        path=("stats",),
        access=AccessMode.READ_ONLY,
        help="Print graph statistics",
        configure_parser=_configure_stats_parser,
        handler=_handle_stats,
    ),
    CommandSpec(
        path=("export",),
        access=AccessMode.MUTATING,
        help="Export nodes and edges as JSON or standalone HTML",
        configure_parser=_configure_export_parser,
        handler=_handle_export,
    ),
    CommandSpec(
        path=("inspect",),
        access=AccessMode.READ_ONLY,
        help="Inspect a node and adjacent edges",
        configure_parser=_configure_inspect_parser,
        handler=_handle_inspect,
    ),
)
_COMMAND_SPECS_BY_PATH = {spec.path: spec for spec in COMMAND_SPECS}
if len(_COMMAND_SPECS_BY_PATH) != len(COMMAND_SPECS):
    raise ValueError("Duplicate declarative CLI command path")



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reql",
        description="Relational Entities Query Language memory graph engine",
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

    agent = sub.add_parser("agent", help="Dashboard-centric agent sessions, tasks, and notes")
    agent.add_argument("--agent", dest="agent_id", default=None, help="Use an agent id; defaults to REQL_AGENT_ID, then a stable activity-derived id")
    agent.add_argument(
        "--activity",
        dest="activity_id",
        default=None,
        help="Select a stable private workspace and session; defaults to REQL_AGENT_ACTIVITY_ID or CODEX_THREAD_ID",
    )
    agent.add_argument("--no-progress", action="store_true", help="Disable Agent Workspace progress messages on stderr")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_init = agent_sub.add_parser("init", help="Create or resume this agent's active session")
    agent_init.add_argument("--name", default=None, help="Name for a newly created session")
    agent_init.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_status = agent_sub.add_parser("status", help="Show private agent-memory status")
    agent_status.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_reset = agent_sub.add_parser("reset", help="Discard and recreate this agent's operational memory")
    agent_reset.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_note = agent_sub.add_parser("note", help="Add a private, directed, or public dashboard note")
    agent_note.add_argument("text", nargs="?", help="General note text")
    agent_note.add_argument("--agent", dest="target_agent_id", default=None, help="Deliver the note to another agent's private dashboard")
    agent_note.add_argument("--public", action="store_true", help="Publish the note to shared dashboard context")
    agent_note.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_task = agent_sub.add_parser("task", help="Task commands: add, done, list")
    agent_task_sub = agent_task.add_subparsers(dest="agent_task_command", required=True)
    agent_task_add = agent_task_sub.add_parser("add", help="Add an agent task")
    agent_task_add.add_argument("description")
    agent_task_add.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_task_done = agent_task_sub.add_parser("done", help="Mark an agent task as done")
    agent_task_done.add_argument("id")
    agent_task_done.add_argument("message", help="Completion message published to shared dashboard context")
    agent_task_done.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_task_list = agent_task_sub.add_parser("list", help="List open agent tasks")
    agent_task_list.add_argument("--all", dest="all_tasks", action="store_true", help="Include completed tasks")
    agent_task_list.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_search = agent_sub.add_parser("search", help="Search public and private dashboard history")
    agent_search.add_argument("query")
    agent_search.add_argument("--type", dest="node_type", default=None, help="Filter by agent node type")
    agent_search.add_argument("--status", default=None, help="Filter by node status")
    agent_search.add_argument("--limit", type=int, default=20, help="Maximum matches")
    agent_search.add_argument("--metadata", action="store_true", help="Include timestamps and stored operational metadata")
    agent_search.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_show = agent_sub.add_parser("show", help="Show an agent-owned item")
    agent_show.add_argument("id")
    agent_show.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_list = agent_sub.add_parser("list", help="List active agents")
    agent_list.add_argument("--all", dest="all_agents", action="store_true", help="Include finished and terminated agents")
    agent_list.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_dashboard = agent_sub.add_parser("dashboard", help="Read shared and selected private dashboards")
    agent_dashboard.add_argument("--limit", type=int, default=5, help="Maximum items per dashboard section (1-20)")
    agent_dashboard.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_finish = agent_sub.add_parser("finish", help="Publish final context, close the session, and mark this agent completed")
    agent_finish.add_argument("summary", help="Final message published to shared dashboard context")
    agent_finish.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_terminate = agent_sub.add_parser("terminate", help="Force-terminate a stale agent while preserving history")
    agent_terminate.add_argument("agent_id")
    agent_terminate.add_argument("--json", action="store_true", help="Print structured JSON result")
    agent_export = agent_sub.add_parser("export", help="Export private agent operational memory")
    agent_export.add_argument("--metadata", action="store_true", help="Include full workspace metadata and all stored nodes")
    agent_export.add_argument("--json", action="store_true", help="Print structured JSON result")

    config = sub.add_parser("config", help="Configuration commands: show, init, set")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="Print the effective configuration")
    config_init = config_sub.add_parser("init", help="Create a sample reql.conf if absent")
    config_init.add_argument("--path", default=PROJECT_CONFIG_FILENAME, help="Target project config file path")
    config_set = config_sub.add_parser("set", help="Add or update an option in ./reql.conf")
    config_set.add_argument("option", help="Dotted option name, such as scan.max_file_size_mb")
    config_set.add_argument("value", help="Value as text, JSON, number, or boolean")

    project = sub.add_parser("project", help="Compile, inspect, explain, and report on the current working directory")
    project.set_defaults(path=".")
    project_sub = project.add_subparsers(dest="project_command", required=True)

    project_watch_status = project_sub.add_parser("watch-status", help="Check watcher liveness without opening the graph")
    project_watch_status.add_argument("--json", action="store_true", help="Print structured JSON result")

    cache = sub.add_parser("cache", help="Cache commands: status, clear")
    cache.set_defaults(path=".")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)

    storage = sub.add_parser("storage", help="Storage commands: inspect, locks, compact, clear")
    storage.set_defaults(path=".")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    storage_clear = storage_sub.add_parser("clear", help="Rebuild storage from the current project and discard historical graph state")
    storage_clear.add_argument("path", nargs="?", default=".", help="Project path; defaults to the current directory")
    storage_clear.add_argument("--json", action="store_true", help="Print structured JSON result")
    storage_compact = storage_sub.add_parser("compact", help="Rewrite the block store into a compact generation")
    storage_compact.add_argument("--json", action="store_true", help="Print structured JSON result")
    storage_inspect = storage_sub.add_parser("inspect", help="Inspect block layout, compression, dense nodes, and indexes")
    storage_inspect.add_argument("--json", action="store_true", help="Print structured JSON result")
    storage_locks = storage_sub.add_parser("locks", help="Inspect lock owners, liveness, duration, and watcher state")
    storage_locks.add_argument("--recover-stale", action="store_true", help="Remove only locks proven stale; incomplete local locks require a safety grace period")
    storage_locks.add_argument("--json", action="store_true", help="Print structured JSON result")

    _configure_declared_commands(
        {
            (): sub,
            ("agent",): agent_sub,
            ("agent", "task"): agent_task_sub,
            ("cache",): cache_sub,
            ("config",): config_sub,
            ("project",): project_sub,
            ("storage",): storage_sub,
        }
    )

    _normalize_subparser_help(sub)
    _normalize_subparser_help(config_sub)
    _normalize_subparser_help(project_sub)
    _normalize_subparser_help(cache_sub)
    _normalize_subparser_help(storage_sub)
    _normalize_subparser_help(agent_sub)
    _normalize_subparser_help(agent_task_sub)

    return parser


def _max_file_size_bytes(args: argparse.Namespace, config: REQLConfig) -> int:
    value = getattr(args, "max_file_size_mb", None)
    if value is None:
        value = config.scan.max_file_size_mb
    return max(0, int(float(value) * 1024 * 1024))


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


def _project_pipeline_output_path(
    raw_path: str | None,
    *,
    project_root: str | Path,
    output_format: str,
) -> Path:
    if output_format == "html":
        filename = "pipeline.html"
        allowed_suffixes = {".html", ".htm"}
    elif output_format == "mermaid":
        filename = "pipeline.mmd"
        allowed_suffixes = {".mmd", ".mermaid"}
    else:
        raise ValueError(f"Unsupported pipeline output format: {output_format}")

    if raw_path is None:
        path = Path(project_root).expanduser().resolve(strict=False) / filename
    else:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_dir() or not path.suffix:
            path = path / filename
        elif path.suffix.casefold() not in allowed_suffixes:
            expected = ", ".join(sorted(allowed_suffixes))
            raise ValueError(
                f"Pipeline {output_format} output must use one of {expected}: {path}"
            )
        path = path.resolve(strict=False)
    return path


def _main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    profile_logger: PerformanceLogger | None = None
    command_spec = _selected_command_spec(args)

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

    args.storage = _resolve_storage_arg(args)

    try:
        config = load_effective_config(start_dir=_config_start_dir(args))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    profile_logger = _profile_logger_from_config(config, str(args.command))
    if profile_logger:
        profile_logger.event("cli.configured", category="lifecycle", argv=raw_argv)
        profile_logger.event("storage.resolved", category="lifecycle", path=str(args.storage))

    if args.command == "config" and args.config_command == "show":
        _print_json(config.to_dict())
        return 0

    if args.command == "config" and args.config_command == "set":
        try:
            path = set_local_config_option(args.option, args.value)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Updated {path}: {args.option}")
        return 0

    if args.command == "project" and args.project_command == "watch-status":
        payload = _project_watch_status(args.storage, args.path)
        if args.json:
            _print_json(payload)
        else:
            _print_project_watch_status(payload)
        return 0

    if args.command == "agent":
        from memory.agent import AgentIdentitySelectionError, AgentWorkspace

        if args.agent_command == "list":
            result = AgentWorkspace.list_registered_agents(args.storage, include_all=args.all_agents, config=config)
            if args.json:
                _print_json({"agents": result})
            else:
                for agent in result:
                    print(f"{agent['agent_id']}\t{agent['status']}\t{agent.get('last_activity_at') or '-'}")
            return 0
        if args.agent_command == "terminate":
            try:
                result = AgentWorkspace.terminate_agent(args.storage, args.agent_id, config=config)
            except ValueError as exc:
                _print_agent_command_error(exc, args)
                return 2
            if args.json:
                _print_json(result)
            else:
                print(f"Agent terminated: {result['agent_id']}")
            return 0
        if args.agent_command == "search":
            result = AgentWorkspace.search_dashboards(args.storage, args.query, limit=args.limit, config=config)
            if args.json:
                _print_json(result)
            else:
                for item in result["results"]:
                    print(f"{item.get('timestamp')}\t{item.get('agent_id')}\t{item.get('context')}")
            return 0

        agent_id = args.agent_id or os.environ.get("REQL_AGENT_ID")
        try:
            raw_workspace = AgentWorkspace(args.storage, agent_id=agent_id, activity_id=args.activity_id, config=config)
        except AgentIdentitySelectionError as exc:
            _print_agent_identity_error(exc, args)
            return 2
        workspace = ProgressingAgentWorkspace(
            raw_workspace,
            label=_agent_progress_label(args),
            enabled=not args.no_progress,
        )
        try:
            if args.agent_command == "init":
                result = workspace.init(name=args.name)
                if args.json:
                    _print_json(result)
                else:
                    print(f"Agent id: {result['agent_id']}")
                    print(f"Initialized private dashboard: {result['agent_storage']}")
                    print(f"Public dashboard: {result['dashboard_storage']}")
                    if session := result.get("session"):
                        print(f"Started session: {session['title']}")
                return 0
            if args.agent_command == "status":
                result = workspace.status()
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_status(result)
                return 0
            if args.agent_command == "reset":
                result = workspace.reset()
                if args.json:
                    _print_json(result)
                else:
                    print(f"Reset agent memory: {result['agent_storage']}")
                return 0
            if args.agent_command == "note":
                if args.text is not None:
                    if args.target_agent_id and args.public:
                        parser.error("agent note accepts either --agent AGENT_ID or --public, not both")
                    if args.target_agent_id:
                        result = workspace.send_note(args.target_agent_id, args.text)
                    elif args.public:
                        result = workspace.publish_note(args.text)
                    else:
                        result = workspace.add_note(args.text)
                else:
                    parser.error("agent note requires TEXT")
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
                    result = workspace.complete_task(args.id, args.message)
                    if args.json:
                        _print_json(result)
                    else:
                        _print_agent_node(result)
                    return 0
                if args.agent_task_command == "list":
                    result = workspace.list_tasks(include_all=args.all_tasks)
                    if args.json:
                        _print_json(result)
                    else:
                        for task in result["tasks"]:
                            _print_agent_node(task)
                    return 0
            if args.agent_command == "show":
                result = workspace.show(args.id)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_node({"node": result["node"]})
                return 0
            if args.agent_command == "dashboard":
                result = workspace.dashboard(limit=args.limit)
                if args.json:
                    _print_json(result)
                else:
                    _print_agent_dashboard(result)
                return 0
            if args.agent_command == "finish":
                result = workspace.finish(args.summary)
                if args.json:
                    _print_json(result)
                else:
                    print(
                        f"Agent finished: {result['agent_id']}"
                        f"\tclosed_session={result.get('closed_session_id') or 'none'}"
                    )
                    retention = result.get("retention") or {}
                    if retention.get("files_removed") or retention.get("records_removed"):
                        print(
                            "Retention: "
                            f"files={retention.get('files_removed', 0)}, "
                            f"records={retention.get('records_removed', 0)}, "
                            f"bytes_reclaimed={retention.get('bytes_reclaimed', 0)}"
                        )
                return 0
            if args.agent_command == "export":
                result = workspace.export(include_metadata=args.metadata)
                _print_json(result)
                return 0
        except ValueError as exc:
            _print_agent_command_error(exc, args)
            return 2

    if args.command == "storage":
        if args.storage_command == "locks":
            payload = inspect_store_locks(Path(args.storage), recover_stale=args.recover_stale)
            if args.json:
                _print_json(payload)
            else:
                _print_storage_locks(payload)
            return 0
        if args.storage_command == "clear":
            payload = clear_project_storage(
                args.storage,
                args.path,
                config=config,
                config_path=_effective_config_path(args),
                max_file_size_bytes=_max_file_size_bytes(args, config),
                parsing_options=CompilationOptions.from_config(config),
            )
            if args.json:
                _print_json(payload)
            else:
                _print_storage_clear(payload)
            return 0
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

    if command_spec is None:
        parser.error(f"Unknown command: {args.command}")
        return 2

    if args.command == "project" and args.project_command == "compile" and args.watch:
        return _handle_detached_project_watch(args, config, profile_logger)

    graph = _open(args, config, profile_logger=profile_logger)
    try:
        return command_spec.handler(
            CommandContext(
                args=args,
                config=config,
                graph=graph,
                profile_logger=profile_logger,
            )
        )
    finally:
        graph.close()


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and render expected storage failures without a traceback."""
    try:
        return _main(argv)
    except StorageError as exc:
        print(_format_storage_error(exc), file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Cannot complete this command: {exc}", file=sys.stderr)
        print("Next: run `reql --help` to review valid commands and arguments.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
