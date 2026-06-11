"""Guided project launcher for the REQL memory graph runtime.

Run it from the project root:

    python launcher.py

By default it opens a guided terminal menu. Use ``cli.py`` or the installed
``reql`` command for command-line workflows.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import shlex
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from textwrap import wrap
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
STORAGE_DIR = Path.cwd() / ".reql"
DEFAULT_STORAGE = STORAGE_DIR / "memory.reql"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory.reporting.html_graph import write_graph_html  # noqa: E402
from api import MemoryGraph  # noqa: E402


@dataclass(slots=True)
class OptionSpec:
    flag: str
    key: str
    takes_value: bool = True
    default: Any = None
    cast: Any = str


@dataclass(slots=True)
class MenuAction:
    key: str
    label: str
    hint: str
    action: Callable[[], int | None]


class Launcher:
    def __init__(self, storage_path: Path, user_id: str = "default", *, debug: bool = False) -> None:
        self.storage_path = storage_path
        self.user_id = user_id
        self.debug = debug
        self.graph = MemoryGraph.open(storage_path)

    def close(self) -> None:
        self.graph.close()

    def reopen(self, storage_path: Path) -> None:
        self.graph.close()
        self.storage_path = storage_path
        self.graph = MemoryGraph.open(storage_path)

    def run_menu(self) -> int:
        return GuidedMenu(self).run()

    def _consolidate(self, tail: str) -> int:
        options, extra = _parse_options(
            tail,
            [
                OptionSpec("--max-events", "max_events", default=500, cast=int),
                OptionSpec("--json", "json", takes_value=False, default=False, cast=bool),
            ],
        )
        _reject_extra(extra)
        result = self.graph.consolidate(user_id=self.user_id, max_events=options["max_events"])
        if options["json"]:
            _print_json(result.to_dict())
        else:
            print(f"Created nodes: {len(result.created_nodes)}")
            print(f"Updated nodes: {len(result.updated_nodes)}")
            print(f"Created edges: {len(result.created_edges)}")
            print(f"Archived nodes: {len(result.archived_nodes)}")
        return 0

    def _stats(self, tail: str) -> int:
        options, extra = _parse_options(
            tail,
            [OptionSpec("--json", "json", takes_value=False, default=False, cast=bool)],
        )
        _reject_extra(extra)
        nodes = self.graph.store.all_nodes(self.user_id)
        edges = self.graph.store.all_edges(self.user_id)
        by_type: dict[str, int] = {}
        for node in nodes:
            by_type[node.type] = by_type.get(node.type, 0) + 1
        payload = {"user": self.user_id, "nodes": len(nodes), "edges": len(edges), "node_types": by_type}
        if options["json"]:
            _print_json(payload)
        else:
            print(f"Nodes: {payload['nodes']}")
            print(f"Edges: {payload['edges']}")
            for key, value in sorted(by_type.items()):
                print(f"  {key}: {value}")
        return 0

    def _export(self, tail: str) -> int:
        payload = self.graph.export_json(user_id=self.user_id)
        options, extra = _parse_options(
            tail,
            [
                OptionSpec("--html", "html", takes_value=False, default=False, cast=bool),
                OptionSpec("--json", "json", takes_value=False, default=False, cast=bool),
            ],
        )
        if options["html"]:
            html_path = write_graph_html(payload, _graph_html_path(extra or None))
            print(html_path)
            if options["json"]:
                json_path = html_path.with_name("graph.json")
                json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json_path)
        else:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            path = extra.strip()
            if options["json"]:
                json_path = _graph_json_path(path or None)
                json_path.parent.mkdir(parents=True, exist_ok=True)
                json_path.write_text(text, encoding="utf-8")
                print(json_path)
            elif path:
                Path(path).write_text(text, encoding="utf-8")
                print(path)
            else:
                print(text)
        return 0

    def _watch(self, tail: str) -> int:
        options, path = _parse_options(
            tail,
            [
                OptionSpec("--interval", "interval", default=2.0, cast=float),
                OptionSpec("--watch-interval", "interval", default=2.0, cast=float),
                OptionSpec("--debounce", "debounce", default=0.25, cast=float),
                OptionSpec("--watch-debounce", "debounce", default=0.25, cast=float),
                OptionSpec("--iterations", "iterations", default=None, cast=int),
                OptionSpec("--watch-iterations", "iterations", default=None, cast=int),
            ],
        )
        path = path.strip() or "."
        print(f"Watching {Path(path).expanduser().resolve(strict=False)}")
        exit_code = 0
        try:
            for event in self.graph.watch_project(
                path,
                user_id=self.user_id,
                interval_seconds=options["interval"],
                debounce_seconds=options["debounce"],
                max_iterations=options["iterations"],
            ):
                print(
                    f"Watch poll {event.iteration}: "
                    f"dirty={event.dirty_artifacts} deleted={event.deleted_artifacts} total={event.total_artifacts}"
                )
                if event.result is None:
                    print("No changes detected")
                    continue
                run = event.result.run
                print(f"Run: {run.id}")
                print(f"Status: {run.status}")
                print(f"Changed: {run.files_changed}")
                print(f"Deleted: {run.files_deleted}")
                print(f"Delta: {event.result.delta.id}")
                if event.errors:
                    exit_code = 1
                    for error in event.errors:
                        print(f"Error: {error}", file=sys.stderr)
        except KeyboardInterrupt:
            print("Watch stopped")
            return 130
        return exit_code


class GuidedMenu:
    def __init__(self, launcher: Launcher) -> None:
        self.launcher = launcher
        self.last_code = 0

    def run(self) -> int:
        while True:
            _clear_screen_hint()
            self._header()
            actions = [
                MenuAction("1", "Guided new memory", "Create a memory, initialize storage, and add the first content.", self.new_memory),
                MenuAction("2", "Available memories", "List memories in .reql/, then open, inspect, or delete them.", self.memories_menu),
                MenuAction("3", "Open memory / switch user", "Choose a REQL file and logical tenant to use.", self.session),
                MenuAction("4", "Ingest text", "Archive raw text into the graph.", self.ingest_text),
                MenuAction("5", "Search and compose context", "Find memories and prepare compact context for agents.", self.retrieve_menu),
                MenuAction("6", "Project and code", "Compile a folder and inspect cache, deltas, and reports.", self.project_menu),
                MenuAction("7", "Guided REQL query", "Run a predefined REQL query or a custom graph query.", self.query_menu),
                MenuAction("8", "Analysis and maintenance", "Show stats, hubs, communities, surprises, and cleanup tools.", self.analysis_menu),
                MenuAction("9", "Reports and export", "Generate Markdown reports, JSON, or interactive graph.html.", self.export_menu),
                MenuAction("10", "MCP server for Codex/Claude", "Configure or start the stdio server for MCP clients.", self.mcp_wizard),
            ]
            for item in actions:
                _print_menu_action(item)
            print("   0. Exit")
            choice = _prompt("Select", "0")
            if choice in {"0", "q", "quit", "exit"}:
                return self.last_code
            selected = next((item for item in actions if item.key == choice), None)
            if selected is None:
                print("Invalid option.")
                _pause()
                continue
            code = self._run(selected.label, selected.action)
            if code not in {None, 0, 130}:
                self.last_code = int(code)
            if code == 130:
                return self.last_code
            _pause()

    def _header(self) -> None:
        print("REQL")
        print("=" * 78)
        print("Unified guided menu for local memory, projects, analysis, and the MCP server.")
        print(f"Storage:  {self.launcher.storage_path}")
        print(f"User:     {self.launcher.user_id}")
        try:
            nodes = len(self.launcher.graph.store.all_nodes(self.launcher.user_id))
            edges = len(self.launcher.graph.store.all_edges(self.launcher.user_id))
            print(f"Status:   {nodes} nodes, {edges} edges")
        except Exception:
            print("Status:   storage not initialized")
        print("-" * 78)

    def _run(self, label: str, action: Callable[[], int | None]) -> int | None:
        print()
        print(label)
        print("-" * len(label))
        try:
            return action()
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
            return None
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            if self.launcher.debug:
                traceback.print_exc()
            return 1

    def new_memory(self) -> int:
        default_name = "memory"
        name = _prompt("Memory name", default_name)
        default_db = STORAGE_DIR / f"{_slug(name or default_name)}.reql"
        storage_path = Path(_prompt("File storage REQL", str(default_db)))
        user_id = _prompt("User/tenant", self.launcher.user_id or "default")
        if storage_path != self.launcher.storage_path:
            self.launcher.reopen(storage_path)
        self.launcher.user_id = user_id or "default"
        self.launcher.graph.store.initialize()
        print(f"Memory ready: {self.launcher.storage_path}")

        first_text = _prompt("First text to store, blank to skip", "")
        if first_text:
            result = self.launcher.graph.ingest(first_text, user_id=self.launcher.user_id)
            print(f"Created {len(result.created_nodes)} nodes and {len(result.created_edges)} edges.")

        if _confirm("Compile a project/folder into this memory now?", False):
            path = _prompt("Project path", ".")
            self._compile_project_path(path)

        if _confirm("Show how to connect this memory to Codex/Claude through MCP now?", True):
            self._print_mcp_connection(read_only=True)
        return 0

    def memories_menu(self) -> int:
        while True:
            memories = _discover_memories()
            print()
            print("Available memories")
            print("------------------")
            if memories:
                self._print_memory_table(memories)
            else:
                print(f"No .reql files found in {STORAGE_DIR}")
            print()
            print("  1. Open memory")
            print("  2. Memory details")
            print("  3. Delete memory")
            print("  4. Create new memory")
            print("  0. Back to main menu")
            choice = _prompt("Select", "0")
            if choice == "0":
                return 0
            if choice == "1":
                selected = self._select_memory(memories)
                if selected is None:
                    continue
                self.launcher.reopen(selected)
                print(f"Memory opened: {selected}")
                continue
            if choice == "2":
                selected = self._select_memory(memories)
                if selected is not None:
                    self._show_memory_details(selected)
                continue
            if choice == "3":
                selected = self._select_memory(memories)
                if selected is not None:
                    self._delete_memory(selected)
                continue
            if choice == "4":
                self.new_memory()
                continue
            print("Invalid option.")

    def _print_memory_table(self, memories: list[Path]) -> None:
        active = _resolve(self.launcher.storage_path)
        for index, path in enumerate(memories, start=1):
            marker = "*" if _resolve(path) == active else " "
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{marker} {index:>2}. {path.name:<28} {_format_bytes(stat.st_size):>9}  {modified}")
        print("* = active memory")

    def _select_memory(self, memories: list[Path]) -> Path | None:
        if not memories:
            print("No memories are available.")
            return None
        raw = _prompt("Memory number", "1")
        try:
            index = int(raw)
        except ValueError:
            print("Invalid number.")
            return None
        if index < 1 or index > len(memories):
            print("Number out of range.")
            return None
        return memories[index - 1]

    def _show_memory_details(self, path: Path) -> None:
        stat = path.stat()
        print(f"File: {path}")
        print(f"Size: {_format_bytes(stat.st_size)}")
        print(f"Modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
        print("Format: REQL block storage")

        if _resolve(path) == _resolve(self.launcher.storage_path):
            nodes = len(self.launcher.graph.store.all_nodes(self.launcher.user_id))
            edges = len(self.launcher.graph.store.all_edges(self.launcher.user_id))
            print(f"Content for active user '{self.launcher.user_id}': {nodes} nodes, {edges} edges")

    def _delete_memory(self, path: Path) -> int:
        if not _is_managed_memory(path):
            print("For safety, the menu only deletes .reql files inside .reql/.", file=sys.stderr)
            return 2
        confirm = _prompt(f"Type '{path.name}' to delete permanently", "")
        if confirm != path.name:
            print("Deletion cancelled.")
            return 0

        delete_paths = [path]
        active_deleted = _resolve(path) == _resolve(self.launcher.storage_path)
        if active_deleted:
            fallback = _fallback_memory_after_delete(path)
            self.launcher.reopen(fallback)
            self.launcher.graph.store.initialize()
            print(f"The deleted memory was active. Session moved to: {fallback}")

        for item in delete_paths:
            if item.exists():
                item.unlink()
                print(f"Deleted: {item}")
        return 0

    def session(self) -> int:
        storage_path = Path(_prompt("File storage REQL", str(self.launcher.storage_path)))
        user_id = _prompt("User/tenant", self.launcher.user_id)
        if storage_path != self.launcher.storage_path:
            self.launcher.reopen(storage_path)
        self.launcher.user_id = user_id or "default"
        print(f"Active session: {self.launcher.storage_path} / {self.launcher.user_id}")
        return 0

    def ingest_text(self) -> int:
        text = _prompt("Text to ingest", "")
        if not text:
            print("No text entered.")
            return 2
        result = self.launcher.graph.ingest(text, user_id=self.launcher.user_id)
        print(f"RawEvent: {result.raw_event.id}")
        print(f"Created nodes: {len(result.created_nodes)}")
        print(f"Created edges: {len(result.created_edges)}")
        return 0

    def retrieve_menu(self) -> int:
        query = _prompt("What do you want to retrieve?", "")
        if not query:
            print("Empty query.")
            return 2
        top_k = _prompt_int("Maximum results", 12)
        depth = _prompt_int("Graph depth", 3)
        subgraph = self.launcher.graph.retrieve(query, user_id=self.launcher.user_id, top_k=top_k, max_depth=depth)
        print(f"Storage: {self.launcher.storage_path}")
        print(f"User: {self.launcher.user_id}")
        if not subgraph.ranked_nodes:
            print("Results found: 0")
            print("No memory found for this search.")
            print("Suggestions: make sure the right memory is open, enter text, or compile a project before searching.")
            return 0

        if _confirm("Do you want agent-ready text context?", True):
            context = self.launcher.graph.retrieval.compose_context(subgraph).strip()
            if context:
                print(f"Results found: {self._count_context_results(context)}")
                print(context)
            else:
                print(f"Results found: {len(subgraph.ranked_nodes)}")
                print("No structured context block is available; showing the most relevant raw results.")
                self._print_ranked_results(subgraph.ranked_nodes)
            return 0
        print(f"Results found: {len(subgraph.ranked_nodes)}")
        self._print_ranked_results(subgraph.ranked_nodes)
        return 0

    @staticmethod
    def _count_context_results(context: str) -> int:
        return sum(1 for line in context.splitlines() if line.startswith("- "))

    @staticmethod
    def _print_ranked_results(ranked_nodes: list[Any]) -> None:
        for item in ranked_nodes:
            node = item.node
            label = node.label or node.text or node.canonical_key or node.id
            print(f"{item.score:.3f}\t{node.type}\t{node.id}\t{label}")
        print(f"({len(ranked_nodes)} results)")

    def project_menu(self) -> int:
        actions = {
            "1": ("Compile project", self.project_compile),
            "2": ("Update project", self.project_update),
            "3": ("Project status", self.project_status),
            "4": ("Cache status", self.cache_status),
            "5": ("List deltas", self.delta_list),
            "6": ("Project report", self.project_report),
            "7": ("Watch project", self.project_watch),
        }
        return self._submenu("Project and code", actions)

    def project_compile(self) -> int:
        path = _prompt("Project path", ".")
        return self._compile_project_path(path)

    def project_update(self) -> int:
        path = _prompt("Project path", ".")
        return self._update_project_path(path)

    def project_watch(self) -> int:
        path = _prompt("Project path", ".")
        interval = _prompt_int("Interval seconds", 2)
        return self.launcher._watch(f"{_quote(path)} --interval {interval}")

    def _compile_project_path(self, path: str) -> int:
        result = self.launcher.graph.compile_project(path, user_id=self.launcher.user_id)
        return self._print_compile_result(result)

    def _update_project_path(self, path: str) -> int:
        result = self.launcher.graph.update_project(path, user_id=self.launcher.user_id)
        return self._print_compile_result(result)

    def _print_compile_result(self, result: Any) -> int:
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
        for error in run.errors:
            print(f"Error: {error}", file=sys.stderr)
        return 0 if not run.errors else 1

    def project_status(self) -> int:
        path = _prompt("Project path", ".")
        status = self.launcher.graph.project_status(path, user_id=self.launcher.user_id)
        if status is None:
            print("Project not found", file=sys.stderr)
            return 1
        project = status["project"]
        print(f"Project: {project['label']}")
        print(f"Root: {project['properties'].get('root_path')}")
        print(f"Artifacts: {status['artifacts']}")
        for artifact_type, count in sorted(status["counts_by_type"].items()):
            print(f"  {artifact_type}: {count}")
        return 0

    def cache_status(self) -> int:
        path = _prompt("Project path", ".")
        status = self.launcher.graph.cache_status(path, user_id=self.launcher.user_id)
        print(f"Project: {status['project']['name']}")
        print(f"Total artifacts: {status['total_artifacts']}")
        print(f"Cached artifacts: {status['cached_artifacts']}")
        print(f"Dirty artifacts: {status['dirty_artifacts']}")
        print(f"Deleted artifacts: {status['deleted_artifacts']}")
        return 0

    def delta_list(self) -> int:
        limit = _prompt_int("Delta count", 20)
        for delta in self.launcher.graph.list_deltas(user_id=self.launcher.user_id, limit=limit):
            print(f"{delta.created_at}\t{delta.id}\t{delta.run_id}\tnodes={len(delta.affected_node_ids)}")
        return 0

    def project_report(self) -> int:
        path = _prompt("Project path", ".")
        output = _prompt("Report output directory", "reports")
        files = self.launcher.graph.project_report(path, output_dir=output, user_id=self.launcher.user_id)
        print(f"Graph report: {files.graph_report}")
        print(f"Delta report: {files.graph_deltas}")
        print(f"Cache report: {files.cache_report}")
        return 0

    def query_menu(self) -> int:
        presets = [
            "PROJECTS",
            "ARTIFACTS WHERE artifact_type = 'code' LIMIT 20",
            "FRAGMENTS LIMIT 20",
            "FIND nodes WHERE type = 'Claim' LIMIT 10",
            "HUBS LIMIT 20",
            "SURPRISES LIMIT 20",
        ]
        print("Predefined queries:")
        for index, statement in enumerate(presets, start=1):
            print(f"  {index}. {statement}")
        raw = _prompt("Choose a query or write REQL", "1")
        statement = presets[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(presets) else raw
        result = self.launcher.graph.query(statement, user_id=self.launcher.user_id)
        print(result.to_table())
        return 0

    def analysis_menu(self) -> int:
        actions = {
            "1": ("Stats", self.stats),
            "2": ("Communities", self.communities),
            "3": ("Hubs", self.hubs),
            "4": ("Surprises", self.surprises),
            "5": ("Consolidation", self.consolidate),
        }
        return self._submenu("Analysis and maintenance", actions)

    def stats(self) -> int:
        return self.launcher._stats("")

    def communities(self) -> int:
        limit = _prompt_int("Limit", 20)
        result = self.launcher.graph.detect_communities(user_id=self.launcher.user_id, limit=limit)
        for node in result.community_nodes[:limit]:
            print(f"{node.id}\t{node.label}\tsize={node.properties.get('size')}\tsalience={node.salience:.3f}")
        return 0

    def hubs(self) -> int:
        limit = _prompt_int("Limit", 20)
        result = self.launcher.graph.analyze_hubs(user_id=self.launcher.user_id, limit=limit)
        for hub in result.hubs:
            print(f"{hub.hub_rank}\t{hub.node_type}\t{hub.node_id}\tscore={hub.hub_score:.3f}\t{hub.label}")
        return 0

    def surprises(self) -> int:
        limit = _prompt_int("Limit", 20)
        result = self.launcher.graph.detect_surprises(user_id=self.launcher.user_id, limit=limit)
        for surprise in result.surprises:
            print(f"{surprise.status}\t{surprise.id}\tscore={surprise.surprise_score:.3f}\t{surprise.title}")
        return 0

    def consolidate(self) -> int:
        max_events = _prompt_int("Maximum events", 500)
        return self.launcher._consolidate(f"--max-events {max_events}")

    def export_menu(self) -> int:
        actions = {
            "1": ("Memory report", self.memory_report),
            "2": ("Export JSON", self.export_json),
            "3": ("Export graph.html", self.export_html),
        }
        return self._submenu("Reports and export", actions)

    def memory_report(self) -> int:
        persist = _confirm("Persist the report in the graph?", False)
        print(self.launcher.graph.report(user_id=self.launcher.user_id, persist=persist))
        return 0

    def export_json(self) -> int:
        path = _prompt("File JSON", "graph.json")
        return self.launcher._export(_quote(path))

    def export_html(self) -> int:
        path = _prompt("HTML file or directory", "graph.html")
        json_flag = " --json" if _confirm("Also write graph.json?", False) else ""
        return self.launcher._export(f"{_quote(path)} --html{json_flag}")

    def mcp_wizard(self) -> int:
        print("The REQL MCP server supports local stdio and HTTP with an API key for network sharing.")
        read_only = _confirm("Use read-only mode for agents?", True)
        http_transport = _confirm("Use HTTP transport with host/port?", False)
        host = "127.0.0.1"
        port = 8765
        api_key = ""
        if http_transport:
            host = _prompt("HTTP host (127.0.0.1 local, 0.0.0.0 network)", host)
            port = _prompt_int("HTTP port", port)
            api_key = _prompt("HTTP API key (required)", "")
            if not api_key:
                print("An API key is required for HTTP transport.", file=sys.stderr)
                return 2
        self._print_mcp_connection(read_only=read_only, http_transport=http_transport, host=host, port=port)
        print()
        print("Use this storage_path in MCP tool arguments:")
        print(f"  {self.launcher.storage_path.resolve().as_posix()}")
        print()
        print("Example tool argument:")
        _print_json({"storage_path": self.launcher.storage_path.resolve().as_posix(), "query": "context for the current task", "user_id": self.launcher.user_id})
        if _confirm("Start the MCP server in this terminal now?", False):
            from mcp.server import main as mcp_main

            args = ["--read-only"] if read_only else []
            if http_transport:
                args.extend(["--transport", "http", "--host", host, "--port", str(port), "--api-key", api_key])
            return mcp_main(args)
        return 0

    def _print_mcp_connection(self, *, read_only: bool, http_transport: bool = False, host: str = "127.0.0.1", port: int = 8765) -> None:
        args_list = '["--read-only"]' if read_only else "[]"
        suffix = " --read-only" if read_only else ""
        if http_transport:
            print("Manual command:")
            print(f"  reql-mcp --transport http --host {host} --port {port}{suffix}")
            print("  # Set REQL_MCP_API_KEY or add --api-key <key>")
            print()
            print("Endpoint HTTP:")
            print(f"  http://{host}:{port}/mcp")
            print("Required header:")
            print("  Authorization: Bearer <api-key>")
            return
        print("Manual command:")
        print(f"  reql-mcp{suffix}")
        print()
        print("Codex config.toml:")
        print("[mcp_servers.reql]")
        print('command = "reql-mcp"')
        print(f"args = {args_list}")
        print()
        print("Claude Desktop:")
        print('{')
        print('  "mcpServers": {')
        print('    "reql": {')
        print('      "command": "reql-mcp",')
        print(f'      "args": {args_list}')
        print('    }')
        print('  }')
        print('}')

    def _submenu(self, title: str, actions: dict[str, tuple[str, Callable[[], int | None]]]) -> int:
        while True:
            print()
            print(title)
            print("-" * len(title))
            for key, (label, _) in actions.items():
                print(f"  {key}. {label}")
            print("  0. Back to main menu")
            choice = _prompt("Select", "0")
            if choice == "0":
                return 0
            selected = actions.get(choice)
            if selected is None:
                print("Invalid option.")
                continue
            label, action = selected
            code = self._run(label, action)
            if code not in {None, 0}:
                return int(code)
            _pause()


def _discover_memories() -> list[Path]:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(path for path in STORAGE_DIR.glob("*.reql") if path.is_file())


def _memory_sidecars(path: Path) -> list[Path]:
    return []


def _is_managed_memory(path: Path) -> bool:
    try:
        resolved = path.resolve()
        storage = STORAGE_DIR.resolve()
    except OSError:
        return False
    return resolved.suffix.casefold() == ".reql" and resolved.parent == storage


def _fallback_memory_after_delete(deleted_path: Path) -> Path:
    fallback = DEFAULT_STORAGE
    if _resolve(fallback) == _resolve(deleted_path):
        fallback = STORAGE_DIR / "memory-new.reql"
    return fallback


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _clear_screen_hint() -> None:
    print()


def _print_menu_action(item: MenuAction) -> None:
    prefix = f"  {item.key:>2}. {item.label:<30} "
    continuation = " " * len(prefix)
    lines = wrap(item.hint, width=76) or [""]
    print(prefix + lines[0])
    for line in lines[1:]:
        print(continuation + line)


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    if value:
        return value
    return default or ""


def _prompt_int(label: str, default: int) -> int:
    while True:
        raw = _prompt(label, str(default))
        try:
            return int(raw)
        except ValueError:
            print("Enter an integer.")


def _confirm(label: str, default: bool) -> bool:
    default_text = "y" if default else "n"
    raw = _prompt(f"{label} (y/n)", default_text).casefold()
    return raw in {"s", "si", "y", "yes"}


def _pause() -> None:
    input("\nPress Enter to continue...")


def _quote(value: str) -> str:
    return shlex.quote(value)


def _slug(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "-" for char in value.strip()]
    slug = "".join(chars).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "memory"


def _parse_options(tail: str, specs: list[OptionSpec]) -> tuple[dict[str, Any], str]:
    by_flag = {spec.flag: spec for spec in specs}
    values = {spec.key: spec.default for spec in specs}
    tokens = shlex.split(tail)
    remainder: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        spec = by_flag.get(token)
        if spec is None:
            remainder.append(token)
            i += 1
            continue
        if spec.takes_value:
            if i + 1 >= len(tokens):
                raise ValueError(f"{spec.flag} requires a value")
            raw = tokens[i + 1]
            values[spec.key] = spec.cast(raw)
            i += 2
        else:
            values[spec.key] = True
            i += 1
    return values, " ".join(remainder)


def _reject_extra(extra: str) -> None:
    if extra.strip():
        raise ValueError(f"unexpected argument(s): {extra}")


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


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guided Memory launcher",
    )
    parser.add_argument("--storage", default=str(DEFAULT_STORAGE), help=f"REQL block storage path (default: {DEFAULT_STORAGE})")
    parser.add_argument("--user", default="default", help="Logical user/tenant id")
    parser.add_argument("--debug", action="store_true", help="Print tracebacks for command errors")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    launcher = Launcher(Path(args.storage), user_id=args.user, debug=args.debug)
    try:
        return launcher.run_menu()
    finally:
        launcher.close()


if __name__ == "__main__":
    raise SystemExit(main())

