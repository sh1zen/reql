from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
from memory.config import load_config
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
    def test_agent_command_progress_reports_heartbeat_and_late_completion(self) -> None:
        from memory.agent.progress import AgentCommandProgress

        stream = io.StringIO()
        with AgentCommandProgress(
            "agent sync",
            initial_delay_seconds=0.01,
            interval_seconds=0.01,
            late_threshold_seconds=0.02,
            stream=stream,
        ):
            time.sleep(0.035)

        output = stream.getvalue()
        self.assertIn("[agent] agent sync: started", output)
        self.assertIn("still running after", output)
        self.assertIn("late completion; result is final", output)

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

    def test_query_explore_reports_structural_template_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "memory.reql"
            graph = MemoryGraph.open(db)
            try:
                nodes = (
                    MemoryNode(id="file:profile", type="File", label="profile.php", text="profile card template", properties={"relative_path": "app/Views/profile.php", "context_scope": "code"}),
                    MemoryNode(id="fragment:profile", type="SourceFragment", label="profile card", text='<section class="profile"><header><h2>Profile</h2></header><div><p>Name</p><input name="name"></div></section>', properties={"relative_path": "app/Views/profile.php", "line_start": 1, "line_end": 1, "context_scope": "code"}),
                    MemoryNode(id="file:account", type="File", label="account.php", text="account template", properties={"relative_path": "app/Views/account.php", "context_scope": "code"}),
                    MemoryNode(id="fragment:account", type="SourceFragment", label="account card", text='<section class="account"><header><h2>Account</h2></header><div><p>Email</p><input name="email"></div></section>', properties={"relative_path": "app/Views/account.php", "line_start": 1, "line_end": 1, "context_scope": "code"}),
                    MemoryNode(id="file:nav", type="File", label="nav.php", text="navigation template", properties={"relative_path": "app/Views/nav.php", "context_scope": "code"}),
                    MemoryNode(id="fragment:nav", type="SourceFragment", label="navigation", text='<nav><ul><li><a href="/">Home</a></li></ul></nav>', properties={"relative_path": "app/Views/nav.php", "line_start": 1, "line_end": 1, "context_scope": "code"}),
                )
                for node in nodes:
                    graph.add_node(node)
                graph.add_edge(MemoryEdge(id="edge:profile-fragment", from_id="file:profile", to_id="fragment:profile", type="CONTAINS_FRAGMENT"))
                graph.add_edge(MemoryEdge(id="edge:account-fragment", from_id="file:account", to_id="fragment:account", type="CONTAINS_FRAGMENT"))
                graph.add_edge(MemoryEdge(id="edge:nav-fragment", from_id="file:nav", to_id="fragment:nav", type="CONTAINS_FRAGMENT"))
            finally:
                graph.close()

            result = subprocess.run(
                [sys.executable, "-m", "memory.cli", "--storage", str(db), "query_explore", "--query", "profile card", "--structural-duplicates-only", "--json"],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": "src"},
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["views"], ["structural_duplicates"])
            rows = payload["sections"]["structural_duplicates"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                {rows[0]["source"]["location"], rows[0]["duplicate"]["location"]},
                {"app/Views/profile.php", "app/Views/account.php"},
            )
            self.assertGreater(rows[0]["similarity"], 0.8)
            self.assertTrue(any(pattern.startswith("sequence:") for pattern in rows[0]["shared_patterns"]))

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
            self.assertEqual(json.loads(context_result.stdout)["query"], "read_query")
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any('"name":"storage.open.read_only_fallback"' in line for line in lines))
            self.assertTrue(any('"read_only":true' in line and '"name":"storage.open"' in line for line in lines))

    def test_project_status_opens_read_only_with_an_existing_reader(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            db = Path(td) / "memory.reql"
            graph = MemoryGraph.open(db)
            try:
                graph.compile_project(root)
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
                        "project",
                        "status",
                        str(root),
                        "--json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            finally:
                held_reader.close()

            payload = json.loads(result.stdout)
            self.assertEqual(payload["project"]["properties"]["root_path"], root.resolve().as_posix())
            self.assertGreaterEqual(payload["artifacts"], 1)

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

    def test_storage_locks_and_snapshot_query_work_while_writer_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "memory.reql"
            graph = MemoryGraph.open(db)
            graph.add_node(MemoryNode(id="snapshot-topic", type="Topic", label="Snapshot topic"))
            graph.close()

            writer = MemoryGraph.open(db)
            try:
                locks_result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "memory.cli",
                        "--storage",
                        str(db),
                        "storage",
                        "locks",
                        "--json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                snapshot_result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "memory.cli",
                        "--storage",
                        str(db),
                        "--snapshot",
                        "stats",
                        "--json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            finally:
                writer.close()

            locks = json.loads(locks_result.stdout)
            self.assertTrue(locks["locked"])
            self.assertTrue(locks["writer"]["process_alive"])
            self.assertTrue(locks["writer"]["command"])
            self.assertTrue(locks["snapshot_available"])
            stats = json.loads(snapshot_result.stdout)
            self.assertGreaterEqual(stats["nodes"], 1)

    def test_locate_resolves_extensionless_case_insensitive_document_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            plugin = root / "extensions" / "wp-optimizer"
            plugin.mkdir(parents=True)
            (plugin / "README.txt").write_text("=== WP Optimizer ===\n\n== Description ==\nExact path marker.\n", encoding="utf-8")
            hidden_docs = root / ".github"
            hidden_docs.mkdir()
            (hidden_docs / "README.md").write_text("# Hidden docs\n", encoding="utf-8")
            db = Path(td) / "memory.reql"
            graph = MemoryGraph.open(db)
            try:
                graph.compile_project(root)
                with patch.object(graph.retrieval, "retrieve", side_effect=AssertionError("locate must not use semantic retrieval")):
                    hidden_payload = graph.locate("./.github\\readme")
                with self.assertRaisesRegex(ValueError, "must not traverse"):
                    graph.locate("../readme")
            finally:
                graph.close()

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "--storage",
                    str(db),
                    "locate",
                    "extensions/wp-optimizer/readme",
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )

            payload = json.loads(result.stdout)
            self.assertEqual(payload["normalized_path"], "extensions/wp-optimizer/readme")
            self.assertEqual(len(payload["matches"]), 1)
            self.assertEqual(payload["matches"][0]["relative_path"], "extensions/wp-optimizer/README.txt")
            self.assertEqual(payload["matches"][0]["context_scope"], "docs")
            self.assertEqual(hidden_payload["normalized_path"], ".github/readme")
            self.assertEqual(hidden_payload["matches"][0]["relative_path"], ".github/README.md")

    def test_query_context_cleanup_include_risky_flag_expands_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "memory.reql"
            graph = MemoryGraph.open(db)
            try:
                graph.add_node(
                    MemoryNode(
                        id="finding:safe-cli",
                        type="StaticAnalysisFinding",
                        label="unused_variable: safe_cli",
                        text="safe cli cleanup target",
                        properties={
                            "relative_path": "app.py",
                            "finding_type": "unused_variable",
                            "symbol_name": "safe_cli",
                            "line_start": 2,
                            "line_end": 2,
                            "cleanup_priority": "high",
                            "cleanup_rank": 3,
                            "confidence": 0.8,
                            "removal_safety": "safe",
                            "removal_reason": "unused_variable is local to this artifact with high confidence and no public-surface signal.",
                            "validation_reason": "",
                            "blocking_signals": [],
                        },
                    )
                )
                graph.add_node(
                    MemoryNode(
                        id="finding:risky-cli",
                        type="StaticAnalysisFinding",
                        label="possibly_unused_function: risky_cli",
                        text="risky cli cleanup target",
                        properties={
                            "relative_path": "app.py",
                            "finding_type": "possibly_unused_function",
                            "symbol_name": "risky_cli",
                            "line_start": 5,
                            "line_end": 6,
                            "cleanup_priority": "low",
                            "cleanup_rank": 1,
                            "confidence": 0.4,
                            "removal_safety": "risky",
                            "removal_reason": "possibly_unused_function has no detected local usage, but removal needs validation before editing.",
                            "validation_reason": "Validate public API before removing this symbol.",
                            "blocking_signals": ["public_api"],
                        },
                    )
                )
            finally:
                graph.close()

            base = [sys.executable, "-m", "memory.cli", "--storage", str(db), "query_context", "--query", "cli cleanup target", "--cleanup", "--json"]
            default_result = subprocess.run(base, check=True, capture_output=True, text=True)
            risky_result = subprocess.run(base + ["--include-risky"], check=True, capture_output=True, text=True)

            default_payload = json.loads(default_result.stdout)
            risky_payload = json.loads(risky_result.stdout)
            self.assertEqual({item["id"] for item in default_payload["cleanup_candidates"]}, {"finding:safe-cli"})
            self.assertEqual(default_payload["cleanup_filter"]["mode"], "safe_remove")
            self.assertEqual({item["id"] for item in risky_payload["cleanup_candidates"]}, {"finding:safe-cli", "finding:risky-cli"})
            self.assertEqual(risky_payload["cleanup_filter"]["mode"], "include_risky")

    def test_project_exclude_creates_and_appends_config_scan_exclude_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()

            first = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "project",
                    "exclude",
                    "build/",
                    "secrets/*.json",
                    "--path",
                    str(root),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            first_payload = json.loads(first.stdout)
            config_path = root / "reql.conf"
            self.assertTrue(first_payload["created"])
            self.assertEqual(first_payload["path"], str(config_path))
            self.assertEqual(first_payload["added"], ["secrets/*.json"])
            self.assertEqual(first_payload["skipped"], ["build/"])
            first_exclude = load_config(config_path).scan.exclude
            self.assertIn(".git/", first_exclude)
            self.assertIn("build/", first_exclude)
            self.assertIn("secrets/*.json", first_exclude)

            second = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "project",
                    "exclude",
                    "build/",
                    "tmp/",
                    "--path",
                    str(root),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            second_payload = json.loads(second.stdout)
            self.assertFalse(second_payload["created"])
            self.assertEqual(second_payload["added"], ["tmp/"])
            self.assertEqual(second_payload["skipped"], ["build/"])
            effective_excludes = load_config(config_path).scan.exclude
            self.assertIn("build/", effective_excludes)
            self.assertIn("secrets/*.json", effective_excludes)
            self.assertIn("tmp/", effective_excludes)

            unsafe_root = Path(td) / "unsafe-project"
            unsafe_root.mkdir()

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory.cli",
                    "project",
                    "exclude",
                    "**",
                    "--path",
                    str(unsafe_root),
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("refusing dangerous scan.exclude pattern", result.stderr)
            self.assertFalse((unsafe_root / "reql.conf").exists())

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

    def test_clean_project_compile_still_records_a_validated_run_and_delta(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def build():\n    return 'ok'\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_app.py").write_text("from app import build\n\ndef test_build():\n    assert build() == 'ok'\n", encoding="utf-8")
            command = [sys.executable, "-m", "memory.cli", "project", "compile", str(root)]

            subprocess.run(command, check=True, capture_output=True, text=True)
            second = subprocess.run(command, check=True, capture_output=True, text=True)

            self.assertIn("Changed: 0", second.stdout)
            self.assertIn("Run:", second.stdout)
            self.assertIn("Delta:", second.stdout)

            (root / "app.py").write_text("def build():\n    return 'changed'\n", encoding="utf-8")
            changed = subprocess.run(command, check=True, capture_output=True, text=True)

            self.assertIn("Changed: 1", changed.stdout)
            self.assertIn("Delta:", changed.stdout)
            self.assertIn("Changed files (1):", changed.stdout)
            self.assertIn("Updated symbols", changed.stdout)
            self.assertIn("Associated tests (1):", changed.stdout)
            self.assertIn("tests/test_app.py", changed.stdout)

    def test_cache_status_defaults_to_current_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def cached_project():\n    return 'ok'\n", encoding="utf-8")

            subprocess.run(
                [sys.executable, "-m", "memory.cli", "project", "compile", "."],
                check=True,
                capture_output=True,
                text=True,
                cwd=root,
            )
            result = subprocess.run(
                [sys.executable, "-m", "memory.cli", "cache", "status"],
                check=True,
                capture_output=True,
                text=True,
                cwd=root,
            )

            self.assertIn("Project:", result.stdout)
            self.assertIn("Total artifacts:", result.stdout)

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
            self.assertIn("REQL Project", codex_project_skill)
            self.assertIn("Installed for: codex (project-local).", codex_project_skill)
            self.assertIn(str(command_path), codex_project_skill)
            self.assertIn("local deterministic repository index", codex_project_skill)
            self.assertIn("optional durable planning for complex work", codex_project_skill)
            self.assertLess(len(codex_project_skill), 9000)
            workflow_lines = [line for line in codex_project_skill.splitlines() if line.partition(".")[0].isdigit()]
            self.assertEqual(len(workflow_lines), 6)
            self.assertIn("## Core Commands", codex_project_skill)
            self.assertIn("## Core Coding Workflow", codex_project_skill)
            self.assertIn("## Special-Case References", codex_project_skill)
            self.assertIn("project status .", codex_project_skill)
            self.assertIn("Project not found", codex_project_skill)
            self.assertIn("one-file or exact-symbol edit", codex_project_skill)
            self.assertIn("do not initialize Agent Workspace", codex_project_skill)
            self.assertIn("`Local configuration required`", codex_project_skill)
            self.assertIn("file spans", codex_project_skill)
            self.assertIn("impact", codex_project_skill)
            self.assertIn("--code", codex_project_skill)
            self.assertIn("--docs", codex_project_skill)
            self.assertIn("--test", codex_project_skill)
            self.assertIn("references/bootstrap.md", codex_project_skill)
            self.assertIn("references/agent-workspace.md", codex_project_skill)
            self.assertIn('query_context --query "<terms from user request>"', codex_project_skill)
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
            self.assertIn("Before the final response for any task that changed files", update_watch_reference)
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
            self.assertIn("### 3. Quick Review", agent_reference)
            self.assertIn("### 4. Code Linking", agent_reference)
            self.assertIn("### 5. Write", agent_reference)
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
            self.assertIn("Installed for: claude (project-local).", claude_project_skill)

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

    def test_install_user_scope_writes_profile_instructions_for_supported_agents(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            fake_home = Path(td) / "home"
            command_dir = Path(td) / "bin"
            fake_home.mkdir()
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
                    "codex",
                "claude",
                "gemini",
                "opencode",
                    "kilo",
                    "openclaw",
                    "hermes",
                    "kimi",
                    "antigravity",
                    "agents",
                "cursor",
                "copilot",
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
            self.assertEqual(payload["scope"], "user")

            profile_instruction_paths = [
                fake_home / "AGENTS.md",
                fake_home / ".claude" / "CLAUDE.md",
                fake_home / "GEMINI.md",
                fake_home / ".config" / "opencode" / "AGENTS.md",
                fake_home / ".kilocode" / "AGENTS.md",
                fake_home / ".openclaw" / "AGENTS.md",
                fake_home / ".hermes" / "AGENTS.md",
                fake_home / ".kimi" / "AGENTS.md",
                fake_home / ".antigravity" / "AGENTS.md",
                fake_home / ".agents" / "AGENTS.md",
            ]
            for path in profile_instruction_paths:
                self.assertTrue(path.exists(), str(path))
                content = path.read_text(encoding="utf-8")
                self.assertIn("When the user types `/reql`", content)
                self.assertIn("project status .", content)
                self.assertIn("generated `references/` file", content)
                self.assertIn("document processing runs in the local compiler", content)
                self.assertIn("document processing", content)
                rule_lines = [line for line in content.splitlines() if line.startswith("- ")]
                self.assertLessEqual(len(rule_lines), 8)
                self.assertNotIn("query_explore --query", content)
                self.assertNotIn("agent batch", content)

            self.assertTrue((fake_home / ".cursor" / "rules" / "reql.mdc").exists())
            self.assertTrue((fake_home / ".github" / "instructions" / "reql.instructions.md").exists())
            self.assertTrue((fake_home / ".agents" / "skills" / "reql-agent" / "SKILL.md").exists())
            self.assertFalse((fake_home / ".config" / "agents" / "skills" / "reql-agent" / "SKILL.md").exists())
            opencode_document_semantics = (
                fake_home / ".config" / "opencode" / "skills" / "reql-agent" / "references" / "document-semantics.md"
            ).read_text(encoding="utf-8")
            opencode_agent_reference = (
                fake_home / ".config" / "opencode" / "skills" / "reql-agent" / "references" / "agent-workspace.md"
            ).read_text(encoding="utf-8")
            self.assertIn("Deterministic document processor", opencode_document_semantics)
            self.assertIn("RawEvent", opencode_document_semantics)
            self.assertNotIn("host `@agent` dispatch path", opencode_document_semantics)
            self.assertIn("Agent Workspace", opencode_agent_reference)

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

    def test_install_without_detected_profiles_lists_disks_instead_of_defaulting_to_codex(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            fake_home = Path(td) / "home"
            command_dir = Path(td) / "bin"
            fake_home.mkdir()
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
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("No supported coding-agent profiles were detected.", result.stderr)
            self.assertIn("Available disks:", result.stderr)
            self.assertIn("reql install codex --user", result.stderr)
            self.assertFalse((fake_home / "AGENTS.md").exists())
            self.assertFalse((fake_home / ".codex" / "skills" / "reql-agent" / "SKILL.md").exists())

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

    def test_interactive_install_retries_autodetect_on_selected_path_before_asking_platform(self) -> None:
        from memory import cli as cli_mod

        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            selected_disk = Path(td) / "agent-disk"
            command_dir = Path(td) / "bin"
            selected_home = cli_mod._home_dir_for_disk(selected_disk)
            (selected_home / ".cursor" / "rules").mkdir(parents=True)
            stdin = _InteractiveInput("1\n")
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
                        "--command-dir",
                        str(command_dir),
                        "--no-hooks",
                        "--json",
                    ]
                )

            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["platforms"], ["cursor"])
            self.assertEqual(payload["scope"], "user")
            self.assertTrue((selected_home / ".cursor" / "rules" / "reql.mdc").exists())
            self.assertIn("Detected platforms: cursor", stderr.getvalue())
            self.assertNotIn("Platform to install", stderr.getvalue())

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

    def test_interactive_uninstall_retries_autodetect_on_selected_path_before_asking_platform(self) -> None:
        from agents.install import install_agent_files
        from memory import cli as cli_mod

        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            selected_disk = Path(td) / "agent-disk"
            command_dir = Path(td) / "bin"
            selected_disk.mkdir()
            selected_home = cli_mod._home_dir_for_disk(selected_disk)
            (selected_home / ".cursor" / "rules").mkdir(parents=True)
            install_agent_files(
                ["cursor"],
                project=False,
                home_dir=selected_home,
                command_dir=command_dir,
                hooks=False,
            )
            stdin = _InteractiveInput("1\n")
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
                        "--command-dir",
                        str(command_dir),
                        "--json",
                    ]
                )

            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["platforms"], ["cursor"])
            self.assertEqual(payload["scope"], "user")
            self.assertFalse((selected_home / ".cursor" / "rules" / "reql.mdc").exists())
            self.assertIn("Detected platforms: cursor", stderr.getvalue())
            self.assertNotIn("Platform to uninstall", stderr.getvalue())

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

    def test_copilot_auto_detect_ignores_generic_github_directory(self) -> None:
        from agents.install import detect_platforms

        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            fake_home = Path(td) / "home"
            project = Path(td) / "repo"
            fake_home.mkdir()
            (project / ".cursor" / "rules").mkdir(parents=True)
            (project / ".github").mkdir()

            original_env = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PATH")}
            try:
                os.environ["HOME"] = str(fake_home)
                os.environ["USERPROFILE"] = str(fake_home)
                os.environ["PATH"] = ""
                if fake_home.drive:
                    os.environ["HOMEDRIVE"] = fake_home.drive
                    os.environ["HOMEPATH"] = str(fake_home)[len(fake_home.drive) :]

                platforms = detect_platforms(project=True, project_dir=project)
                self.assertNotIn("codex", platforms)
                self.assertIn("cursor", platforms)
                self.assertNotIn("copilot", platforms)

                (project / ".github" / "copilot-instructions.md").write_text("# Copilot\n", encoding="utf-8")
                platforms = detect_platforms(project=True, project_dir=project)
                self.assertIn("copilot", platforms)
            finally:
                for name, value in original_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_codex_auto_detect_ignores_codex_directory_and_command(self) -> None:
        from agents.install import detect_platforms

        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            fake_home = Path(td) / "home"
            project = Path(td) / "repo"
            fake_bin = Path(td) / "bin"
            fake_home.mkdir()
            fake_bin.mkdir()
            (fake_home / ".codex").mkdir()
            (project / ".codex").mkdir(parents=True)
            command_name = "codex.cmd" if sys.platform.startswith("win") else "codex"
            command_path = fake_bin / command_name
            command_path.write_text("@echo off\n" if sys.platform.startswith("win") else "#!/bin/sh\n", encoding="utf-8")
            command_path.chmod(0o755)

            original_env = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PATH")}
            try:
                os.environ["HOME"] = str(fake_home)
                os.environ["USERPROFILE"] = str(fake_home)
                os.environ["PATH"] = str(fake_bin)
                if fake_home.drive:
                    os.environ["HOMEDRIVE"] = fake_home.drive
                    os.environ["HOMEPATH"] = str(fake_home)[len(fake_home.drive) :]

                platforms = detect_platforms(project=True, project_dir=project)
                self.assertNotIn("codex", platforms)

                (project / ".codex" / "skills").mkdir()
                platforms = detect_platforms(project=True, project_dir=project)
                self.assertIn("codex", platforms)
            finally:
                for name, value in original_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

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
            query_context_payload = json.loads(query_context_json.stdout)
            self.assertNotIn("context", query_context_payload)
            self.assertIn(query_context_payload["kind"], {"code", "general"})
            self.assertEqual(query_context_payload["query_mode"], "informative")
            self.assertIn("followups", query_context_payload)
            query_context_code_json = subprocess.run(
                base + ["query_context", "--query", "water_office_plant", "--top-k", "5", "--code", "--json"],
                check=True,
                capture_output=True,
                text=True,
            )
            query_context_code_payload = json.loads(query_context_code_json.stdout)
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
            self.assertEqual(json.loads(query_context_cleanup_json.stdout)["query_mode"], "cleanup")
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
