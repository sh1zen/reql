from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.config import load_config
from memory.domain.models import MemoryEdge, MemoryNode


class CLITests(unittest.TestCase):
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
            self.assertTrue(any("targeted_reads" in item["instruction"] for item in payload["sections"]["code"]["usage_guidance"]))
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
            )

            self.assertIn("Function", result.stdout)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any('"name":"storage.open.read_only_fallback"' in line for line in lines))
            self.assertTrue(any('"read_only":true' in line and '"name":"storage.open"' in line for line in lines))

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
            config_path = root / "conf.yaml"
            self.assertTrue(first_payload["created"])
            self.assertEqual(first_payload["path"], str(config_path))
            self.assertEqual(first_payload["added"], ["build/", "secrets/*.json"])
            first_exclude = load_config(config_path).scan.exclude
            self.assertIn(".tmp/", first_exclude)
            self.assertEqual(first_exclude[-2:], ["build/", "secrets/*.json"])

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
            self.assertEqual(load_config(config_path).scan.exclude[-3:], ["build/", "secrets/*.json", "tmp/"])

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
            self.assertFalse((unsafe_root / "conf.yaml").exists())

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

    def test_project_compile_loads_project_local_conf_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "conf.yaml").write_text("cache:\n  enabled: false\n", encoding="utf-8")
            (root / "app.py").write_text("def local_config_compile():\n    return 'ok'\n", encoding="utf-8")

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
            self.assertTrue((project / ".codex" / "skills" / "reql-project" / "SKILL.md").exists())
            self.assertTrue((project / ".claude" / "skills" / "reql-project" / "SKILL.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-project" / "agents" / "openai.yaml").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-project" / "references" / "bootstrap.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-project" / "references" / "query.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-project" / "references" / "update-watch.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-project" / "references" / "reports-exports.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-project" / "references" / "document-semantics.md").exists())
            self.assertTrue((project / ".claude" / "CLAUDE.md").exists())
            self.assertTrue((project / ".codex" / "skills" / "reql-project" / ".reql_agent_version").exists())
            self.assertTrue((project / ".claude" / "skills" / "reql-project" / ".reql_agent_version").exists())
            claude_settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertIn("REQL_AGENT_HOOK_V1", json.dumps(claude_settings))
            self.assertIn("do not duplicate that context with broad", json.dumps(claude_settings))
            self.assertIn("once after modifying project files", json.dumps(claude_settings))
            self.assertIn("feature, behavior, file, command, error, field, endpoint, API, or symbol terms", json.dumps(claude_settings))
            self.assertIn("preserve the user", json.dumps(claude_settings))
            self.assertIn("language, identifiers, and exact errors", json.dumps(claude_settings))
            self.assertNotIn('--query "current task"', json.dumps(claude_settings))
            codex_project_skill = (project / ".codex" / "skills" / "reql-project" / "SKILL.md").read_text(encoding="utf-8")
            claude_project_skill = (project / ".claude" / "skills" / "reql-project" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("REQL Project", codex_project_skill)
            self.assertIn("Installed for: codex (project-local).", codex_project_skill)
            self.assertIn(str(command_path), codex_project_skill)
            self.assertIn("Use this skill for project mode", codex_project_skill)
            self.assertIn("project status .", codex_project_skill)
            self.assertIn("Project not found", codex_project_skill)
            self.assertIn("immediately run", codex_project_skill)
            self.assertIn("project compile . --watch", codex_project_skill)
            self.assertIn("Do not ask before the required one-shot", codex_project_skill)
            self.assertNotIn("ask for approval and start one", codex_project_skill)
            self.assertIn("Never exclude framework/source roots", codex_project_skill)
            self.assertIn("Do not run multiple `project exclude` commands in parallel", codex_project_skill)
            self.assertIn('query "HUBS LIMIT 20"', codex_project_skill)
            self.assertNotIn("reql \"HUBS LIMIT 20\"", codex_project_skill)
            self.assertIn("Use REQL as the repository context index", codex_project_skill)
            self.assertIn("Do not run broad `rg`", codex_project_skill)
            self.assertIn("`find`, `grep -R`", codex_project_skill)
            self.assertIn("file-scoped `rg`/symbol searches", codex_project_skill)
            self.assertIn("Document processing runs locally", codex_project_skill)
            self.assertIn("file spans", codex_project_skill)
            self.assertIn("targeted reads", codex_project_skill)
            self.assertIn("--code", codex_project_skill)
            self.assertIn("--docs", codex_project_skill)
            self.assertIn("--test", codex_project_skill)
            self.assertNotIn('query_context --query "<terms from user request>" --edit --json', codex_project_skill)
            self.assertIn('query_context --query "<terms from user request>" --cleanup', codex_project_skill)
            self.assertIn('query_context --query "<exact term>"', codex_project_skill)
            self.assertIn("RETURN id,type,text,score,relative_path,line_start,line_end", codex_project_skill)
            self.assertIn("source/code text with exact locations", codex_project_skill)
            self.assertIn("Choose the query_context mode explicitly", codex_project_skill)
            self.assertIn("`informative`", codex_project_skill)
            self.assertNotIn("`--edit`", codex_project_skill)
            self.assertIn("`--cleanup`", codex_project_skill)
            self.assertIn("For unused-code or dead-code requests", codex_project_skill)
            self.assertIn("After modifying project files", codex_project_skill)
            self.assertIn("project compile .` once before finishing", codex_project_skill)
            self.assertIn("Before the final response for any task that changed files", codex_project_skill)
            self.assertIn("Reference Routing", codex_project_skill)
            self.assertIn("references/bootstrap.md", codex_project_skill)
            self.assertIn('query_context --query "<terms from user request>"', codex_project_skill)
            self.assertIn('query_explore --query "<terms from user request>"', codex_project_skill)
            self.assertIn('query_memories --query "<terms from user request>"', codex_project_skill)
            self.assertIn('query_graph --query "<terms from user request>"', codex_project_skill)
            self.assertNotIn('retrieve --query "<terms from user request>"', codex_project_skill)
            bootstrap_reference = (project / ".codex" / "skills" / "reql-project" / "references" / "bootstrap.md").read_text(encoding="utf-8")
            query_reference = (project / ".codex" / "skills" / "reql-project" / "references" / "query.md").read_text(encoding="utf-8")
            update_watch_reference = (project / ".codex" / "skills" / "reql-project" / "references" / "update-watch.md").read_text(encoding="utf-8")
            openai_yaml = (project / ".codex" / "skills" / "reql-project" / "agents" / "openai.yaml").read_text(encoding="utf-8")
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
            self.assertIn("display_name: REQL Project", openai_yaml)
            generated_skill_text = "\n".join(
                [
                    codex_project_skill,
                    bootstrap_reference,
                    query_reference,
                    update_watch_reference,
                    (project / ".codex" / "skills" / "reql-project" / "references" / "reports-exports.md").read_text(encoding="utf-8"),
                    (project / ".codex" / "skills" / "reql-project" / "references" / "document-semantics.md").read_text(encoding="utf-8"),
                ]
            )
            document_semantics_reference = (project / ".codex" / "skills" / "reql-project" / "references" / "document-semantics.md").read_text(encoding="utf-8")
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
            self.assertEqual([path.name for path in (project / ".codex" / "skills").iterdir()], ["reql-project"])
            self.assertEqual([path.name for path in (project / ".claude" / "skills").iterdir()], ["reql-project"])
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
            self.assertFalse((project / ".codex" / "skills" / "reql-project" / "SKILL.md").exists())
            self.assertFalse((project / ".codex" / "skills" / "reql-project" / "references" / "bootstrap.md").exists())
            self.assertFalse((project / ".claude" / "skills" / "reql-project" / "SKILL.md").exists())
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
                self.assertIn("broad `rg`", content)
                self.assertIn("project compile .` once before finishing", content)
                self.assertIn("Before the final response for any task that changed files", content)
                self.assertIn("project status .", content)
                self.assertIn("Project not found", content)
                self.assertIn("document processing runs in the local compiler", content)

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
                fake_home / ".config" / "agents" / "AGENTS.md",
            ]
            for path in profile_instruction_paths:
                self.assertTrue(path.exists(), str(path))
                content = path.read_text(encoding="utf-8")
                self.assertIn("When the user types `/reql`", content)
                self.assertIn("project status .", content)
                self.assertIn("Dirty `.reql/`", content)
                self.assertIn("reports/GRAPH_REPORT.md", content)
                self.assertIn("document processing runs in the local compiler", content)
                self.assertIn("Document processing", content)

            self.assertTrue((fake_home / ".cursor" / "rules" / "reql.mdc").exists())
            self.assertTrue((fake_home / ".github" / "instructions" / "reql.instructions.md").exists())
            opencode_document_semantics = (
                fake_home / ".config" / "opencode" / "skills" / "reql-project" / "references" / "document-semantics.md"
            ).read_text(encoding="utf-8")
            self.assertIn("Deterministic document processor", opencode_document_semantics)
            self.assertIn("RawEvent", opencode_document_semantics)
            self.assertNotIn("host `@agent` dispatch path", opencode_document_semantics)

    def test_install_without_platforms_auto_detects_agent_profiles(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            fake_home = Path(td) / "home"
            command_dir = Path(td) / "bin"
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".cursor").mkdir(parents=True)
            env = dict(os.environ)
            env["HOME"] = str(fake_home)
            env["USERPROFILE"] = str(fake_home)
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
            self.assertIn("codex", payload["platforms"])
            self.assertIn("cursor", payload["platforms"])
            self.assertTrue((fake_home / "AGENTS.md").exists())
            self.assertTrue((fake_home / ".cursor" / "rules" / "reql.mdc").exists())

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
