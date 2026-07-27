from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
from memory.domain.models import MemoryEdge, MemoryNode


class _InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class _InterruptingInput(io.StringIO):
    def isatty(self) -> bool:
        return True

    def readline(self, *args: object, **kwargs: object) -> str:
        raise KeyboardInterrupt


class CLITests(unittest.TestCase):

    def test_project_explain_command_spec_controls_parser_access_and_snapshot(self) -> None:
        from memory import cli as cli_mod

        args = cli_mod.build_parser().parse_args(
            ["--snapshot", "project", "explain", "repo", "--focus", "checkout", "--json"]
        )
        spec = cli_mod._selected_command_spec(args)

        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.path, ("project", "explain"))
        self.assertIs(spec.access, cli_mod.AccessMode.READ_ONLY)
        self.assertTrue(spec.snapshot)
        self.assertIs(cli_mod._command_access_mode(args), cli_mod.AccessMode.READ_ONLY)
        self.assertEqual(args.path, "repo")
        self.assertEqual(args.focus, "checkout")
        self.assertTrue(args.json)

    def test_command_spec_rejects_snapshot_for_mutating_command(self) -> None:
        from memory import cli as cli_mod

        with self.assertRaisesRegex(ValueError, "Only read-only commands"):
            cli_mod.CommandSpec(
                path=("example",),
                access=cli_mod.AccessMode.MUTATING,
                snapshot=True,
                help="Example",
                configure_parser=lambda parser: None,
                handler=lambda context: 0,
            )

    def test_query_command_spec_resolves_access_from_statement(self) -> None:
        from memory import cli as cli_mod

        parser = cli_mod.build_parser()
        read_args = parser.parse_args(["query", "MATCH (n) RETURN n"])
        write_args = parser.parse_args(["query", "HUBS LIMIT 5"])

        self.assertIs(cli_mod._command_access_mode(read_args), cli_mod.AccessMode.READ_ONLY)
        self.assertIs(cli_mod._command_access_mode(write_args), cli_mod.AccessMode.MUTATING)

    def test_snapshot_is_rejected_when_dynamic_query_access_is_mutating(self) -> None:
        from memory import cli as cli_mod

        stderr = io.StringIO()
        with patch.object(cli_mod.sys, "stderr", stderr):
            result = cli_mod.main(["--snapshot", "query", "HUBS LIMIT 5"])

        self.assertEqual(result, 2)
        self.assertIn("--snapshot is only valid", stderr.getvalue())

    def test_compile_summary_labels_semantic_symbol_changes(self) -> None:
        from memory import cli as cli_mod

        summary = type(
            "Summary",
            (),
            {"changed_files": [], "updated_symbols": [], "associated_tests": []},
        )()
        stdout = io.StringIO()

        with patch.object(cli_mod.sys, "stdout", stdout):
            cli_mod._print_compile_summary(summary)

        self.assertIn("Changed symbols (0):", stdout.getvalue())
        self.assertNotIn("Updated symbols", stdout.getvalue())

    def test_storage_lock_error_is_reported_without_traceback(self) -> None:
        from memory import cli as cli_mod

        stdout = io.StringIO()
        stderr = io.StringIO()
        storage_path = r"C:\Program Files (x86)\project\.reql\memory.reql"
        message = (
            f"REQL block store is locked for write: {storage_path}; "
            "pid=20324; process_alive=true; stale=false"
        )

        with (
            patch.object(cli_mod.sys, "stdout", stdout),
            patch.object(cli_mod.sys, "stderr", stderr),
            patch.object(cli_mod, "_open", side_effect=cli_mod.StorageError(message)),
        ):
            rc = cli_mod.main(["project", "compile", "."])

        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "reql is locked for write: to fix any possible stale: "
            f'reql --storage "{storage_path}" storage locks --recover-stale\n',
        )
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("pid=20324", stderr.getvalue())

    def test_query_accepts_split_retrieve_statement_words(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            db = Path(td) / "memory.reql"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "--storage",
                    str(db),
                    "query",
                    "RETRIEVE",
                    "console_scripts",
                    "entry",
                    "points",
                    "reql",
                    "reql-mcp",
                    "cli.py",
                    "src.cli",
                    "pyproject",
                    "argparse",
                    "subcommands",
                    "project",
                    "cache",
                    "query",
                    "retrieve",
                    "LIMIT",
                    "20",
                    "RETURN",
                    "id,type,text,score,relative_path,line_start,line_end",
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_inspect_node_resolves_location_and_neighbors(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            db = Path(td) / "memory.reql"
            graph = MemoryGraph.open(db)
            try:
                source = MemoryNode(
                    id="artifact:notes",
                    type="SourceArtifact",
                    label="notes.md",
                    properties={"path": str(Path(td) / "notes.md"), "relative_path": "notes.md", "artifact_id": "artifact:notes"},
                )
                function = MemoryNode(
                    id="function:plant",
                    type="Function",
                    label="office plant",
                    text="def water_office_plant(): ...",
                    properties={"path": "notes.md", "relative_path": "notes.md", "line_start": 3, "line_end": 3},
                )
                graph.add_node(source)
                graph.add_node(function)
                graph.add_edge(
                    MemoryEdge(
                        id="edge:source",
                        from_id="function:plant",
                        to_id="artifact:notes",
                        type="DEFINED_IN",
                        properties={"source_file": "notes.md", "line_start": 3, "line_end": 3, "artifact_id": "artifact:notes"},
                    )
                )
            finally:
                graph.close()

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "--storage",
                    str(db),
                    "inspect",
                    "--node-id",
                    "function:plant",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            self.assertTrue(payload["found"])
            self.assertEqual(payload["node"]["id"], "function:plant")
            self.assertEqual(payload["location"]["path"], "notes.md")
            self.assertEqual(payload["location"]["line_start"], 3)
            self.assertTrue(any(item["other_id"] == "artifact:notes" for item in payload["neighbors"]))
            self.assertTrue(any(item["location"]["relative_path"] == "notes.md" for item in payload["sources"]))

    def test_query_explore_returns_dependency_views(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            db = Path(td) / "memory.reql"
            graph = MemoryGraph.open(db)
            try:
                module = MemoryNode(
                    id="module:api",
                    type="Module",
                    label="api",
                    text="module owner for target_api",
                    properties={"name": "api", "relative_path": "src/api.py", "line_start": 1, "line_end": 20},
                )
                target = MemoryNode(
                    id="function:target-api",
                    type="Function",
                    label="target_api",
                    text="def target_api(): return json payload",
                    properties={"name": "target_api", "qualified_name": "api.target_api", "relative_path": "src/api.py", "line_start": 5, "line_end": 7},
                )
                caller = MemoryNode(
                    id="function:caller",
                    type="Function",
                    label="call_target_api",
                    text="def call_target_api(): target_api()",
                    properties={"name": "call_target_api", "qualified_name": "api.call_target_api", "relative_path": "src/api.py", "line_start": 10, "line_end": 12},
                )
                payload = MemoryNode(
                    id="variable:payload",
                    type="Variable",
                    label="json_payload",
                    text="json serialization payload",
                    properties={"name": "json_payload", "relative_path": "src/api.py", "line_start": 6, "line_end": 6},
                )
                docs = MemoryNode(
                    id="fragment:docs-target-api",
                    type="SourceFragment",
                    label="target_api docs",
                    text="Documentation mentions target_api serialization behavior.",
                    properties={"relative_path": "docs/API.md", "line_start": 3, "line_end": 4},
                )
                for node in (module, target, caller, payload, docs):
                    graph.add_node(node)
                graph.add_edge(MemoryEdge(id="edge:owner", from_id=module.id, to_id=target.id, type="DEFINES"))
                graph.add_edge(MemoryEdge(id="edge:caller", from_id=caller.id, to_id=target.id, type="CALLS", properties={"line_start": 11, "line_end": 11}))
                graph.add_edge(MemoryEdge(id="edge:payload", from_id=target.id, to_id=payload.id, type="READS", properties={"evidence": "json payload"}))
                graph.add_edge(MemoryEdge(id="edge:docs", from_id=docs.id, to_id=target.id, type="REFERENCES"))
            finally:
                graph.close()

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "--storage",
                    str(db),
                    "query_explore",
                    "--query",
                    "target_api serialization",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["kind"], "query_explore")
            self.assertTrue(any(item["owner"]["id"] == "module:api" for item in payload["sections"]["owners"]))
            self.assertTrue(any(item["caller"]["id"] == "function:caller" for item in payload["sections"]["callers"]))
            self.assertTrue(any(item["surface"]["id"] == "function:target-api" for item in payload["sections"]["public_surface"]))
            self.assertTrue(any(item["node"]["id"] == "variable:payload" for item in payload["sections"]["serialization_paths"]))
            self.assertTrue(any(item["mention"]["id"] == "fragment:docs-target-api" for item in payload["sections"]["docs_mentions"]))
            self.assertNotIn("usage_guidance", payload["sections"]["code"])
            self.assertTrue(any(item["node_id"] == "function:target-api" for item in payload["sections"]["code"]["read_plan"]))
            self.assertTrue(any(item["node_id"] == "function:target-api" for item in payload["sections"]["code"]["targeted_reads"]))

            owners_only = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "--storage",
                    str(db),
                    "query_explore",
                    "--query",
                    "target_api",
                    "--owners-only",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            owners_payload = json.loads(owners_only.stdout)
            self.assertEqual(owners_payload["views"], ["owners"])
            self.assertEqual(set(owners_payload["sections"]), {"owners"})

    def test_query_opens_read_only_for_concurrent_reads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "memory.reql"
            log = Path(td) / "query-profile.jsonl"
            config_path = Path(td) / "conf.yaml"
            log_path_text = str(log).replace("\\", "/")
            config_path.write_text(
                "diagnostics:\n"
                "  enabled: true\n"
                f'  path: "{log_path_text}"\n',
                encoding="utf-8",
            )
            graph = MemoryGraph.open(db)
            try:
                graph.add_node(MemoryNode(id="function:read-query", type="Function", label="read_query", text="def read_query(): ..."))
            finally:
                graph.close()

            held_reader = MemoryGraph.open(db, read_only=True)
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "memory.cli",
                        "--storage",
                        str(db),
                        "--config",
                        str(config_path),
                        "query",
                        "FIND nodes WHERE type = 'Function' LIMIT 5",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                context_result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "memory.cli",
                        "--storage",
                        str(db),
                        "query_context",
                        "--query",
                        "read_query",
                        "--json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            finally:
                held_reader.close()

            self.assertIn("Function", result.stdout)
            self.assertEqual(json.loads(context_result.stdout)["payload"]["query"], "read_query")
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any('"name":"storage.open.read_only_fallback"' in line for line in lines))
            self.assertTrue(any('"read_only":true' in line and '"name":"storage.open"' in line for line in lines))

    def test_project_history_and_diff_expose_latest_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            source = root / "module.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            db = Path(td) / "memory.reql"
            graph = MemoryGraph.open(db)
            try:
                first = graph.compile_project(root)
                source.write_text("VALUE = 2\n", encoding="utf-8")
                second = graph.compile_project(root)
            finally:
                graph.close()

            history_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "--storage",
                    str(db),
                    "project",
                    "history",
                    str(root),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            diff_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "--storage",
                    str(db),
                    "project",
                    "diff",
                    str(root),
                    "--revision",
                    second.revision.id,
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )

            history = json.loads(history_result.stdout)
            diff = json.loads(diff_result.stdout)
            self.assertEqual([item["id"] for item in history], [second.revision.id, first.revision.id])
            self.assertEqual(diff["parent_id"], first.revision.id)
            self.assertEqual(diff["changes"][0]["path"], "module.py")
            self.assertEqual(diff["changes"][0]["status"], "modified")

    def test_project_compile_without_storage_writes_under_build_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def build():\n    return 'ok'\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-m", "memory.cli", "project", "compile", str(root)],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("Delta:", result.stdout)
            self.assertTrue((root / ".reql" / "memory.reql").exists())

    def test_project_watch_status_reads_lock_without_opening_graph(self) -> None:
        from memory import cli as cli_mod

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            lock_payload = {
                "writer": {
                    "watcher": True,
                    "process_alive": True,
                    "stale": False,
                    "pid": 4321,
                    "host": "test-host",
                    "created_at": "2026-07-18T10:00:00+00:00",
                    "duration_seconds": 12.5,
                    "command": "reql project compile . --watch",
                }
            }
            stdout = io.StringIO()
            with (
                patch.object(cli_mod.sys, "stdout", stdout),
                patch.object(cli_mod, "inspect_store_locks", return_value=lock_payload) as inspect_locks,
                patch.object(cli_mod, "_open", side_effect=AssertionError("watch status must not open the graph")),
            ):
                rc = cli_mod.main(["project", "watch-status", str(root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual(payload["status"], "running")
            self.assertTrue(payload["running"])
            self.assertEqual(payload["pid"], 4321)
            self.assertEqual(payload["project_path"], str(root.resolve()))
            inspect_locks.assert_called_once_with(root / ".reql" / "memory.reql")

    def test_project_watch_status_classifies_non_running_lock_states(self) -> None:
        from memory import cli as cli_mod

        cases = (
            ({"writer": None}, "stopped", False, False),
            (
                {"writer": {"watcher": True, "process_alive": False, "stale": True}},
                "stale",
                False,
                False,
            ),
            (
                {"writer": {"watcher": True, "process_alive": None, "stale": False}},
                "unknown",
                None,
                False,
            ),
            (
                {"writer": {"watcher": False, "process_alive": True, "stale": False}},
                "stopped",
                False,
                True,
            ),
        )
        for locks, expected_status, expected_running, expected_blocked in cases:
            with self.subTest(status=expected_status, blocked=expected_blocked):
                with patch.object(cli_mod, "inspect_store_locks", return_value=locks):
                    payload = cli_mod._project_watch_status("memory.reql", ".")
                self.assertEqual(payload["status"], expected_status)
                self.assertEqual(payload["running"], expected_running)
                self.assertEqual(payload["blocked_by_other_writer"], expected_blocked)

    def test_project_compile_loads_project_local_reql_conf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "reql.conf").write_text(
                "cache:\n  enabled: false\nscan:\n  use_gitignore: true\n  exclude:\n    - local-excluded.py\n",
                encoding="utf-8",
            )
            (root / ".gitignore").write_text("git-excluded.py\n", encoding="utf-8")
            (root / "app.py").write_text("def local_config_compile():\n    return 'ok'\n", encoding="utf-8")
            (root / "git-excluded.py").write_text("def excluded_by_git():\n    return 'no'\n", encoding="utf-8")
            (root / "local-excluded.py").write_text("def excluded_by_reql():\n    return 'no'\n", encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "memory.cli", "project", "compile", str(root)],
                check=True,
                capture_output=True,
                text=True,
                cwd=Path(td),
            )

            graph = MemoryGraph.open(root / ".reql" / "memory.reql")
            try:
                functions = [node for node in graph.store.all_nodes() if node.type == "Function"]
            finally:
                graph.close()

            self.assertTrue(any(node.properties.get("name") == "local_config_compile" for node in functions))
            self.assertFalse(any(node.properties.get("name") == "excluded_by_git" for node in functions))
            self.assertFalse(any(node.properties.get("name") == "excluded_by_reql" for node in functions))

    def test_install_project_agents_creates_codex_and_claude_files_idempotently(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            project = Path(td) / "repo"
            command_dir = Path(td) / "bin"
            project.mkdir()
            (project / "AGENTS.md").write_text("# Existing instructions\n", encoding="utf-8")
            base = [
                sys.executable,
                "-m",
                "memory.cli",
                "install",
                "codex",
                "claude",
                "--project-dir",
                str(project),
                "--command-dir",
                str(command_dir),
                "--json",
            ]

            first = subprocess.run(base, check=True, capture_output=True, text=True)
            payload = json.loads(first.stdout)
            self.assertEqual(payload["platforms"], ["codex", "claude"])
            self.assertEqual(payload["scope"], "project")
            command_name = "reql.cmd" if sys.platform.startswith("win") else "reql"
            command_path = command_dir / command_name
            self.assertTrue(command_path.exists())
            self.assertTrue(any(action["kind"] == "command" and action["status"] == "created" for action in payload["actions"]))
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / "SKILL.md").exists())
            self.assertTrue((project / ".claude" / "skills" / "reql-agent" / "SKILL.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / "agents" / "openai.yaml").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / "references" / "bootstrap.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / "references" / "query.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / "references" / "update-watch.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / "references" / "reports-exports.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / "references" / "document-semantics.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / "references" / "agent-workspace.md").exists())
            self.assertTrue((project / ".claude" / "CLAUDE.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-agent" / ".reql_version").exists())
            self.assertTrue((project / ".claude" / "skills" / "reql-agent" / ".reql_version").exists())
            claude_settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertIn("REQL_AGENT_HOOK_V1", json.dumps(claude_settings))
            self.assertIn("do not duplicate that context with broad", json.dumps(claude_settings))
            self.assertIn("once after modifying project files", json.dumps(claude_settings))
            self.assertIn("feature, behavior, file, command, error, field, endpoint, API, or symbol terms", json.dumps(claude_settings))
            self.assertIn("preserve the user", json.dumps(claude_settings))
            self.assertIn("language, identifiers, and exact errors", json.dumps(claude_settings))
            self.assertNotIn('--query "current task"', json.dumps(claude_settings))
            codex_project_skill = (project / ".codex" / "skills" / "reql-agent" / "SKILL.md").read_text(encoding="utf-8")
            claude_project_skill = (project / ".claude" / "skills" / "reql-agent" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("REQL Fast Path", codex_project_skill)
            self.assertNotIn("Installed for:", codex_project_skill)
            self.assertNotIn(str(command_path), codex_project_skill)
            skill_lines = codex_project_skill.splitlines()
            self.assertGreaterEqual(len(skill_lines), 20)
            self.assertLessEqual(len(skill_lines), 30)
            workflow_lines = [line for line in codex_project_skill.splitlines() if line.partition(".")[0].isdigit()]
            self.assertEqual(len(workflow_lines), 5)
            self.assertIn("# REQL Fast Path", codex_project_skill)
            self.assertIn("## Load only when triggered", codex_project_skill)
            self.assertIn("## Rules", codex_project_skill)
            self.assertIn("project status .", codex_project_skill)
            self.assertIn("files, owners, line ranges, and tests", codex_project_skill)
            self.assertIn("Confidence: insufficient", codex_project_skill)
            self.assertIn("--code", codex_project_skill)
            self.assertIn("--docs", codex_project_skill)
            self.assertIn("--test", codex_project_skill)
            self.assertIn("references/bootstrap.md", codex_project_skill)
            self.assertIn("references/agent-workspace.md", codex_project_skill)
            self.assertIn('query_context --query "<user terms>"', codex_project_skill)
            self.assertNotIn("## Core Commands", codex_project_skill)
            self.assertNotIn("Project not found", codex_project_skill)
            self.assertNotIn("one-file or exact-symbol edit", codex_project_skill)
            self.assertNotIn("`Local configuration required`", codex_project_skill)
            self.assertNotIn("agent status", codex_project_skill)
            self.assertNotIn("project compile . --watch", codex_project_skill)
            self.assertNotIn("query_explore --query", codex_project_skill)
            self.assertNotIn("RETRIEVE ", codex_project_skill)
            self.assertNotIn("agent batch", codex_project_skill)
            self.assertNotIn("reql-mcp", codex_project_skill)
            for reference_path in (
                "references/bootstrap.md",
                "references/query.md",
                "references/update-watch.md",
                "references/reports-exports.md",
                "references/document-semantics.md",
                "references/agent-workspace.md",
            ):
                self.assertEqual(codex_project_skill.count(reference_path), 1)
            bootstrap_reference = (project / ".codex" / "skills" / "reql-agent" / "references" / "bootstrap.md").read_text(encoding="utf-8")
            query_reference = (project / ".codex" / "skills" / "reql-agent" / "references" / "query.md").read_text(encoding="utf-8")
            update_watch_reference = (project / ".codex" / "skills" / "reql-agent" / "references" / "update-watch.md").read_text(encoding="utf-8")
            openai_yaml = (project / ".codex" / "skills" / "reql-agent" / "agents" / "openai.yaml").read_text(encoding="utf-8")
            agent_reference = (project / ".codex" / "skills" / "reql-agent" / "references" / "agent-workspace.md").read_text(encoding="utf-8")
            self.assertIn("Fast path: existing graph", bootstrap_reference)
            self.assertIn("Project not found", bootstrap_reference)
            self.assertIn("Raw tool limits", bootstrap_reference)
            self.assertIn("custom scanners, or ad hoc crawlers", bootstrap_reference)
            self.assertIn(str(command_path), bootstrap_reference)
            self.assertIn("query_graph --query", query_reference)
            self.assertIn("Code-Scoped Workflow", query_reference)
            self.assertIn("Free-form Query Shape", query_reference)
            self.assertIn("Query Types", query_reference)
            self.assertIn("Informative:", query_reference)
            self.assertNotIn("Edit:", query_reference)
            self.assertIn("Cleanup:", query_reference)
            self.assertIn("REQL is not an LLM", query_reference)
            self.assertIn("3-8 informative terms", query_reference)
            self.assertIn("empty, placeholder, or context-dependent pronoun queries", query_reference)
            self.assertIn("Keep the user's language instead of translating", query_reference)
            self.assertIn("feature, behavior, file, command, error, field, endpoint, API, or symbol terms", query_reference)
            self.assertIn("use `--code`, `--docs`, and `--test`", query_reference)
            self.assertIn('query_context --query "graphify"', query_reference)
            self.assertIn("read only the missing spans", query_reference)
            self.assertIn("read_plan", query_reference)
            self.assertIn("change_chain", query_reference)
            self.assertIn("test_targets", query_reference)
            self.assertIn("Confidence: insufficient", query_reference)
            self.assertIn("one targeted `rg`", query_reference)
            self.assertIn("clear one-file or exact-symbol edit", query_reference)
            self.assertIn("skip Agent Workspace", query_reference)
            self.assertIn("Do not read entire files unless the line ranges are missing", query_reference)
            self.assertIn("--view owners --view code", query_reference)
            self.assertIn("Start without `--json`", query_reference)
            self.assertIn("Use `--json` only when another tool or script needs structured fields", query_reference)
            self.assertIn("Raw REQL Statements", query_reference)
            self.assertIn('Use raw `reql query "..."` statements', query_reference)
            self.assertIn("deterministic rows instead of a synthesized context block", query_reference)
            self.assertIn("Keep raw queries bounded", query_reference)
            self.assertIn("Raw tool limits", query_reference)
            self.assertIn("If a raw search starts expanding across unrelated directories", query_reference)
            self.assertIn("source_for", query_reference)
            self.assertIn("direction", query_reference)
            self.assertIn("retrieve exact locations", query_reference)
            self.assertIn("raw REQL rows", query_reference)
            self.assertIn("compact source/memory text rows", query_reference)
            self.assertIn("explicit custom REQL columns or source locations are needed", query_reference)
            self.assertIn("Unused-Code Cleanup", query_reference)
            self.assertIn("FINDINGS WHERE finding_type IN", query_reference)
            self.assertIn("StaticAnalysisFinding", query_reference)
            self.assertIn("framework callbacks", query_reference)
            self.assertIn("project watch-status . --json", codex_project_skill)
            self.assertIn("wait for the watcher to refresh the graph", codex_project_skill)
            self.assertIn("otherwise run", codex_project_skill)
            self.assertNotIn("Changed files, watcher, cache, or deltas", codex_project_skill)
            self.assertIn("Watch/compile troubleshooting", codex_project_skill)
            self.assertIn("Load this only when post-edit refresh does not behave as expected", update_watch_reference)
            self.assertIn("project watch-status . --json", update_watch_reference)
            self.assertIn("do not inspect `ps`, `Get-CimInstance`", update_watch_reference)
            self.assertIn("## Final change classification", update_watch_reference)
            self.assertIn("`Versioned functional changes`", update_watch_reference)
            self.assertIn("the user's personal `config.json` must supply it", update_watch_reference)
            self.assertIn("`Local configuration required: none`", update_watch_reference)
            self.assertIn("display_name: REQL Project", openai_yaml)
            self.assertIn("agent memory", openai_yaml)
            self.assertIn("Agent Workspace", agent_reference)
            self.assertIn("agent status", agent_reference)
            self.assertIn("agent reset", agent_reference)
            self.assertIn("does not modify `.reql/memory.reql`", agent_reference)
            self.assertIn("planning layer when a project is too large", agent_reference)
            self.assertIn("Required Agent Workflow", agent_reference)
            self.assertIn("### 1. Plan", agent_reference)
            self.assertIn("### 2. Task Build", agent_reference)
            self.assertNotIn("Quick Review", agent_reference)
            self.assertIn("### 3. Code Linking", agent_reference)
            self.assertIn("### 4. Write", agent_reference)
            self.assertIn("### 5. Handoff To Master", agent_reference)
            self.assertIn("Do not run `agent map` before or after ordinary edits", agent_reference)
            self.assertIn("Use the map only to recover after context loss, thread compaction, a handoff, or a long pause", agent_reference)
            before_recovery, recovery_section = agent_reference.split("## Recover Context", 1)
            self.assertNotIn("\nreql agent map", before_recovery)
            self.assertEqual(recovery_section.count("\nreql agent map"), 3)
            self.assertIn("assemble the implementation from the task graph", agent_reference)
            self.assertIn("After `reql project compile .` adds new files, run `reql agent sync` before linking", agent_reference)
            self.assertIn("After compile with new files, run sync before linking new standard nodes", agent_reference)
            self.assertIn("code notes, files, symbols", agent_reference)
            self.assertNotIn("Plan: use", codex_project_skill)
            self.assertNotIn("Task build:", codex_project_skill)
            self.assertNotIn("Quick review:", codex_project_skill)
            generated_skill_text = "\n".join(
                [
                    codex_project_skill,
                    bootstrap_reference,
                    query_reference,
                    update_watch_reference,
                    (project / ".codex" / "skills" / "reql-agent" / "references" / "reports-exports.md").read_text(encoding="utf-8"),
                    (project / ".codex" / "skills" / "reql-agent" / "references" / "document-semantics.md").read_text(encoding="utf-8"),
                    agent_reference,
                ]
            )
            document_semantics_reference = (project / ".codex" / "skills" / "reql-agent" / "references" / "document-semantics.md").read_text(encoding="utf-8")
            self.assertIn("Deterministic document processor", document_semantics_reference)
            self.assertIn("RawEvent", document_semantics_reference)
            self.assertIn("CO_OCCURS_WITH", document_semantics_reference)
            self.assertNotIn("Coding-agent bridge contract", document_semantics_reference)
            self.assertNotIn("surprise detection", generated_skill_text.casefold())
            self.assertNotIn('--query "question"', generated_skill_text)
            self.assertNotIn('--query "current task"', generated_skill_text)
            self.assertNotIn("incremental compile deleted files", generated_skill_text)
            self.assertNotIn("storage payload serialization", generated_skill_text)
            self.assertNotIn("agent skill query guidance", generated_skill_text)
            self.assertNotIn("document code linker references", generated_skill_text)
            self.assertNotIn("document-semantic config keys", generated_skill_text)
            self.assertNotIn("MCP read only tools", generated_skill_text)
            self.assertNotIn("delta list", generated_skill_text)
            self.assertNotIn("delta show DELTA_ID", generated_skill_text)
            self.assertNotIn("communities --limit", generated_skill_text)
            self.assertNotIn("hubs --limit", generated_skill_text)
            self.assertNotIn("hubs --type", generated_skill_text)
            self.assertNotIn("explain hub NODE_ID", generated_skill_text)
            self.assertNotIn("where is this", generated_skill_text)
            self.assertNotIn("dove sta questo", generated_skill_text)
            self.assertEqual(sorted(path.name for path in (project / ".codex" / "skills").iterdir()), ["reql-agent"])
            self.assertEqual(sorted(path.name for path in (project / ".claude" / "skills").iterdir()), ["reql-agent"])
            self.assertIn("REQL Fast Path", claude_project_skill)
            self.assertLessEqual(len(claude_project_skill.splitlines()), 30)

            shim_env = dict(os.environ)
            shim_env.pop("PYTHONPATH", None)
            db = Path(td) / "shim.reql"
            shim_project = Path(td) / "shim-project"
            shim_project.mkdir()
            (shim_project / "shim.py").write_text("def shim_smoke_test():\n    return 'ok'\n", encoding="utf-8")
            shim_compile = subprocess.run(
                [
                    str(command_path),
                    "--storage",
                    str(db),
                    "project",
                    "compile",
                    str(shim_project),
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=Path(td),
                env=shim_env,
            )
            self.assertIn("Delta:", shim_compile.stdout)

            second = subprocess.run(base, check=True, capture_output=True, text=True)
            second_payload = json.loads(second.stdout)
            self.assertTrue(all(action["status"] == "unchanged" for action in second_payload["actions"]))
            self.assertEqual((project / "AGENTS.md").read_text(encoding="utf-8").count("REQL-INSTALL:START"), 1)
            self.assertEqual((project / ".claude" / "CLAUDE.md").read_text(encoding="utf-8").count("REQL-INSTALL:START"), 1)

            uninstall = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "uninstall",
                    "codex",
                    "claude",
                    "--project-dir",
                    str(project),
                    "--command-dir",
                    str(command_dir),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            uninstall_payload = json.loads(uninstall.stdout)
            self.assertEqual(uninstall_payload["scope"], "project")
            self.assertFalse(command_path.exists())
            self.assertFalse((project / ".codex" / "skills" / "reql-agent" / "SKILL.md").exists())
            self.assertFalse((project / ".codex" / "skills" / "reql-agent" / "references" / "bootstrap.md").exists())
            self.assertFalse((project / ".codex" / "skills" / "reql-agent" / "references" / "agent-workspace.md").exists())
            self.assertFalse((project / ".claude" / "skills" / "reql-agent" / "SKILL.md").exists())
            self.assertFalse((project / ".claude" / "settings.json").exists())
            self.assertNotIn("REQL-INSTALL:START", (project / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertIn("Existing instructions", (project / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertTrue(any(action["kind"] == "hook" and action["status"] == "removed" for action in uninstall_payload["actions"]))

    def test_install_project_agent_rules_use_platform_specific_formatters(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            project = Path(td) / "repo"
            command_dir = Path(td) / "bin"
            project.mkdir()

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "install",
                    "cursor",
                    "copilot",
                    "kilo",
                    "--project-dir",
                    str(project),
                    "--command-dir",
                    str(command_dir),
                    "--no-hooks",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["platforms"], ["cursor", "copilot", "kilo"])

            cursor_rule = (project / ".cursor" / "rules" / "reql.mdc").read_text(encoding="utf-8")
            copilot_instruction = (project / ".github" / "instructions" / "reql.instructions.md").read_text(encoding="utf-8")
            kilo_rule = (project / ".kilocode" / "rules" / "reql.md").read_text(encoding="utf-8")

            self.assertIn("alwaysApply: true", cursor_rule)
            self.assertIn('applyTo: "**"', copilot_instruction)
            self.assertNotIn("applyTo:", kilo_rule)
            self.assertIn("Kilo Code", kilo_rule)
            for content in (cursor_rule, copilot_instruction, kilo_rule):
                self.assertIn("REQL-INSTALL:START", content)
                self.assertIn("project status .", content)
                self.assertIn("project compile .", content)
                self.assertIn("broad repository scans", content)
                self.assertIn("run documented tests", content)
                self.assertIn("bootstrap with `project compile .` only when the project is missing", content)
                self.assertIn("updated symbols, associated tests, and test results", content)
                self.assertIn("document processing runs in the local compiler", content)
                self.assertEqual(content.count("project status ."), 1)
                self.assertLessEqual(len([line for line in content.splitlines() if line.startswith("- ")]), 8)

    def test_install_without_platforms_auto_detects_agent_profiles(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            fake_home = Path(td) / "home"
            command_dir = Path(td) / "bin"
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".cursor" / "rules").mkdir(parents=True)
            env = dict(os.environ)
            env["HOME"] = str(fake_home)
            env["USERPROFILE"] = str(fake_home)
            env["PATH"] = ""
            if fake_home.drive:
                env["HOMEDRIVE"] = fake_home.drive
                env["HOMEPATH"] = str(fake_home)[len(fake_home.drive) :]

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "install",
                    "--user",
                    "--command-dir",
                    str(command_dir),
                    "--no-hooks",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            payload = json.loads(result.stdout)
            self.assertNotIn("codex", payload["platforms"])
            self.assertIn("cursor", payload["platforms"])
            self.assertFalse((fake_home / "AGENTS.md").exists())
            self.assertTrue((fake_home / ".cursor" / "rules" / "reql.mdc").exists())

            uninstall = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "uninstall",
                    "--user",
                    "--command-dir",
                    str(command_dir),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            uninstall_payload = json.loads(uninstall.stdout)
            self.assertEqual(uninstall_payload["scope"], "user")
            self.assertNotIn("codex", uninstall_payload["platforms"])
            self.assertIn("cursor", uninstall_payload["platforms"])
            self.assertFalse((fake_home / ".cursor" / "rules" / "reql.mdc").exists())

    def test_interactive_install_prompts_for_target_then_proceeds(self) -> None:
        from memory import cli as cli_mod

        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            initial_project = Path(td) / "initial"
            selected_disk = Path(td) / "agent-disk"
            command_dir = Path(td) / "bin"
            initial_project.mkdir()
            selected_disk.mkdir()
            stdin = _InteractiveInput("1\ncodex\n")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch.object(cli_mod.sys, "stdin", stdin),
                patch.object(cli_mod.sys, "stdout", stdout),
                patch.object(cli_mod.sys, "stderr", stderr),
                patch.object(cli_mod, "_available_disk_roots", return_value=[str(selected_disk)]),
            ):
                rc = cli_mod.main(
                    [
                        "install",
                        "--project-dir",
                        str(initial_project),
                        "--command-dir",
                        str(command_dir),
                        "--no-hooks",
                        "--json",
                    ]
                )

            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["platforms"], ["codex"])
            self.assertEqual(payload["scope"], "user")
            selected_home = cli_mod._home_dir_for_disk(selected_disk)
            self.assertTrue((selected_home / ".codex" / "skills" / "reql-agent" / "SKILL.md").exists())
            self.assertFalse((initial_project / ".codex" / "skills" / "reql-agent" / "SKILL.md").exists())
            self.assertIn("Available disks:", stderr.getvalue())
            self.assertIn("No supported profiles found at", stderr.getvalue())
            self.assertNotIn("Project path", stderr.getvalue())

    def test_interactive_uninstall_prompts_for_target_then_proceeds(self) -> None:
        from agents.install import install_agent_files
        from memory import cli as cli_mod

        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            initial_project = Path(td) / "initial"
            selected_disk = Path(td) / "agent-disk"
            command_dir = Path(td) / "bin"
            initial_project.mkdir()
            selected_disk.mkdir()
            selected_home = cli_mod._home_dir_for_disk(selected_disk)
            install_agent_files(
                ["codex"],
                project=False,
                home_dir=selected_home,
                command_dir=command_dir,
                hooks=False,
            )
            stdin = _InteractiveInput("1\ncodex\n")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch.object(cli_mod.sys, "stdin", stdin),
                patch.object(cli_mod.sys, "stdout", stdout),
                patch.object(cli_mod.sys, "stderr", stderr),
                patch.object(cli_mod, "_available_disk_roots", return_value=[str(selected_disk)]),
            ):
                rc = cli_mod.main(
                    [
                        "uninstall",
                        "--project-dir",
                        str(initial_project),
                        "--command-dir",
                        str(command_dir),
                        "--json",
                    ]
                )

            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["platforms"], ["codex"])
            self.assertEqual(payload["scope"], "user")
            self.assertFalse((selected_home / ".codex" / "skills" / "reql-agent" / "SKILL.md").exists())
            self.assertIn("Available disks:", stderr.getvalue())
            self.assertIn("No supported profiles found at", stderr.getvalue())
            self.assertIn("Platform to uninstall", stderr.getvalue())

    def test_interactive_install_and_uninstall_interrupt_without_traceback(self) -> None:
        from memory import cli as cli_mod

        for command in ("install", "uninstall"):
            with self.subTest(command=command):
                stdout = io.StringIO()
                stderr = io.StringIO()

                with (
                    patch.object(cli_mod.sys, "stdin", _InterruptingInput()),
                    patch.object(cli_mod.sys, "stdout", stdout),
                    patch.object(cli_mod.sys, "stderr", stderr),
                    patch.object(cli_mod, "_available_disk_roots", return_value=["C:\\"]),
                ):
                    rc = cli_mod.main([command, "--json"])

                self.assertEqual(rc, 130)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn(f"{command.capitalize()} cancelled.", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_project_install_auto_detect_does_not_use_user_scope_codex_profile(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            fake_home = Path(td) / "home"
            command_dir = Path(td) / "bin"
            project = Path(td) / "repo"
            user_codex_skill = fake_home / ".codex" / "skills" / "some-real-skill"
            project.mkdir()
            user_codex_skill.mkdir(parents=True)
            (user_codex_skill / "SKILL.md").write_text("# User Codex Skill\n", encoding="utf-8")
            env = dict(os.environ)
            env["HOME"] = str(fake_home)
            env["USERPROFILE"] = str(fake_home)
            env["PATH"] = ""
            if fake_home.drive:
                env["HOMEDRIVE"] = fake_home.drive
                env["HOMEPATH"] = str(fake_home)[len(fake_home.drive) :]

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "install",
                    "--project-dir",
                    str(project),
                    "--command-dir",
                    str(command_dir),
                    "--no-hooks",
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("No supported coding-agent profiles were detected.", result.stderr)
            self.assertFalse((project / ".codex" / "skills" / "reql-agent" / "SKILL.md").exists())

    def test_cli_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "memory.reql"
            base = [sys.executable, "-m", "memory.cli", "--storage", str(db)]
            project = Path(td) / "project"
            project.mkdir()
            (project / "plant.py").write_text(
                "def water_office_plant():\n    return 'office plant watered'\n",
                encoding="utf-8",
            )
            ingest = subprocess.run(
                base + ["project", "compile", str(project)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Delta:", ingest.stdout)
            query_context = subprocess.run(
                base + ["query_context", "--query", "water_office_plant", "--top-k", "5"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("water_office_plant", query_context.stdout)
            query_context_json = subprocess.run(
                base + ["query_context", "--query", "water_office_plant", "--top-k", "5", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            query_context_envelope = json.loads(query_context_json.stdout)
            query_context_payload = query_context_envelope["payload"]
            self.assertNotIn("context", query_context_payload)
            self.assertIn(query_context_payload["kind"], {"code", "general"})
            self.assertEqual(query_context_payload["query_mode"], "informative")
            self.assertIn("followups", query_context_payload)
            self.assertEqual(query_context_envelope["schema_version"], 1)
            self.assertEqual(len(query_context_envelope["graph_revision"]), 64)
            query_context_code_json = subprocess.run(
                base + ["query_context", "--query", "water_office_plant", "--top-k", "5", "--code", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            query_context_code_payload = json.loads(query_context_code_json.stdout)["payload"]
            self.assertEqual(query_context_code_payload["query_mode"], "informative")
            self.assertEqual(query_context_code_payload["scopes"], ["code"])
            query_context_edit_json = subprocess.run(
                base + ["query_context", "--query", "water_office_plant", "--top-k", "5", "--edit", "--json"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(query_context_edit_json.returncode, 0)
            query_context_cleanup_json = subprocess.run(
                base + ["query_context", "--query", "water_office_plant", "--top-k", "5", "--cleanup", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(query_context_cleanup_json.stdout)["payload"]["query_mode"],
                "cleanup",
            )
            query_graph = subprocess.run(
                base + ["query_graph", "--query", "water_office_plant", "--top-k", "5", "--max-depth", "2", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            query_graph_payload = json.loads(query_graph.stdout)
            self.assertIn("seed_nodes", query_graph_payload)
            self.assertIn("edges", query_graph_payload)
            self.assertIn("sources", query_graph_payload)
            self.assertIn("REQL Query Graph", query_graph_payload["context"])
            query_memories = subprocess.run(
                base + ["query_memories", "--query", "water_office_plant", "--limit", "5", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            query_memories_payload = json.loads(query_memories.stdout)
            self.assertEqual(query_memories_payload["query"], "water_office_plant")
            self.assertIn("memories", query_memories_payload)
            self.assertIn("ranked_nodes", query_memories_payload)
            self.assertIn("nodes", query_memories_payload)
            self.assertIn("edges", query_memories_payload)
            self.assertIn("seed_node_ids", query_memories_payload)
            self.assertIn("trace_id", query_memories_payload)
            self.assertGreater(query_memories_payload["count"], 0)
            self.assertTrue(any("water_office_plant" in item["text"] or "water office plant" in item["text"] for item in query_memories_payload["memories"]))
            query = subprocess.run(
                base + ["query", "FIND nodes WHERE type = 'Function' LIMIT 10"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Function", query.stdout)
            stats = subprocess.run(
                base + ["stats", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            stats_payload = json.loads(stats.stdout)
            self.assertGreater(stats_payload["nodes"], 0)
            self.assertGreater(stats_payload["edges"], 0)
            self.assertIn("Function", stats_payload["node_types"])
            deltas_query = subprocess.run(
                base + ["query", "DELTAS LIMIT 10", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            deltas_payload = json.loads(deltas_query.stdout)
            self.assertEqual(deltas_payload["command"], "DELTAS")
            self.assertGreaterEqual(deltas_payload["row_count"], 1)
            communities_query = subprocess.run(
                base + ["query", "COMMUNITIES LIMIT 20", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(communities_query.stdout)["command"], "COMMUNITIES")
            hubs_query = subprocess.run(
                base + ["query", "HUBS LIMIT 20", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(hubs_query.stdout)["command"], "HUBS")
            graph_out = Path(td) / "reql-graph-out"
            html = subprocess.run(
                base + ["export", "--html", "--json", "--out", str(graph_out)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("graph.html", html.stdout)
            self.assertTrue((graph_out / "graph.html").exists())
            self.assertTrue((graph_out / "graph.json").exists())
            self.assertIn("REQL Memory Graph", (graph_out / "graph.html").read_text(encoding="utf-8"))
            json_out = Path(td) / "json-out"
            json_export = subprocess.run(
                base + ["export", "--json", "--out", str(json_out)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("graph.json", json_export.stdout)
            self.assertFalse(json_export.stdout.lstrip().startswith("{"))
            json_payload = json.loads((json_out / "graph.json").read_text(encoding="utf-8"))
            self.assertEqual(json_payload["format"], "reql-memory-export-v1")
            storage_inspect = subprocess.run(
                base + ["storage", "inspect", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            storage_payload = json.loads(storage_inspect.stdout)
            self.assertGreater(storage_payload["blocks"]["total"], 0)
            self.assertGreater(storage_payload["records"]["total"], 0)
            self.assertIn("ratio", storage_payload["compression"])
            self.assertIn("dense_nodes", storage_payload)
            self.assertIn("index_stats", storage_payload)
            self.assertIn("wal", storage_payload)
            self.assertIn("root_index", storage_payload)
            self.assertIn("space_map", storage_payload)
            storage_compact = subprocess.run(
                base + ["storage", "compact", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            compact_payload = json.loads(storage_compact.stdout)
            self.assertGreater(compact_payload["generation_id_after"], compact_payload["generation_id_before"])
            self.assertGreaterEqual(compact_payload["records_after"], storage_payload["records"]["total"])


if __name__ == "__main__":
    unittest.main()
