"""Exercise bounded agent lifecycle storage through the normal workspace API."""
from __future__ import annotations

import tempfile
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from api import MemoryGraph
from memory.agent import AgentWorkspace
from memory.cli import CommandContext, _handle_project_overview, _print_agent_dashboard, build_parser
from memory.config import default_config, merge_config


class AgentLifecycleTests(unittest.TestCase):
    """Protect summaries, active work, and canonical storage during cleanup."""

    def setUp(self) -> None:
        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.directory.cleanup)
        self.storage = Path(self.directory.name).resolve() / ".reql" / "memory.reql"
        self.storage.parent.mkdir()
        self.config = merge_config(default_config(), {"retention": {"agent_sessions": 2}})

    def workspace(self, agent_id: str, *, activity_id: str | None = None) -> AgentWorkspace:
        """Create an isolated workspace with a small public retention window."""
        return AgentWorkspace(self.storage, agent_id=agent_id, activity_id=activity_id, config=self.config)

    def test_finish_removes_private_data_and_preserves_summary_and_canonical_bytes(self) -> None:
        self.storage.write_bytes(b"canonical graph is never opened by agent commands")
        workspace = self.workspace("agent:worker")
        session = workspace.init("Work")["session"]["id"]
        task = workspace.add_task("Implement cleanup")["node"]
        workspace.complete_task(task["id"], "Cleanup tested")
        workspace.add_task("Unfinished private task")
        workspace.add_note("Private scratch")
        result = workspace.finish("Finished cleanup")
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["closed_session_id"], session)
        self.assertGreaterEqual(result["retention"]["files_removed"], 1)
        self.assertGreater(result["retention"]["bytes_reclaimed"], 0)
        self.assertFalse(workspace.exists())
        self.assertEqual(list(workspace.paths.agent_storage.parent.glob("*")), [])
        dashboard = workspace.public_dashboard()
        self.assertEqual(dashboard["agents"][0]["status"], "finished")
        self.assertEqual(dashboard["active_tasks"], [])
        self.assertEqual({item["content"] for item in dashboard["context"]}, {"Finished cleanup", "Cleanup tested"})
        self.assertEqual(self.storage.read_bytes(), b"canonical graph is never opened by agent commands")
        self.assertFalse(workspace.status()["exists"])

    def test_public_retention_is_project_wide_and_preserves_active_and_unregistered_stores(self) -> None:
        active = self.workspace("agent:active")
        active.init("Active")
        active.add_task("Keep working")
        unregistered = active.paths.agent_storage.parent / "unregistered.reql"
        unregistered.write_bytes(b"unregistered")
        for number in range(4):
            workspace = self.workspace(f"agent:worker-{number}")
            workspace.init(f"Work {number}")
            workspace.publish_note(f"Public result {number}")
            workspace.finish(f"Summary {number}")
        dashboard = active.public_dashboard()
        self.assertEqual({item["agent_id"] for item in dashboard["agents"]}, {"agent:active", "agent:worker-2", "agent:worker-3"})
        self.assertEqual({item["content"] for item in dashboard["context"]}, {"Public result 2", "Public result 3", "Summary 2", "Summary 3"})
        self.assertEqual(len(dashboard["active_tasks"]), 1)
        self.assertTrue(active.exists())
        self.assertEqual(unregistered.read_bytes(), b"unregistered")

    def test_reused_identity_retains_only_latest_completed_sessions(self) -> None:
        for number in range(4):
            workspace = self.workspace("agent:reused")
            workspace.init(f"Work {number}")
            workspace.publish_note(f"Public result {number}")
            workspace.finish(f"Summary {number}")
        dashboard = workspace.public_dashboard()
        self.assertEqual(len(dashboard["agents"]), 1)
        self.assertEqual({item["content"] for item in dashboard["context"]}, {"Public result 2", "Public result 3", "Summary 2", "Summary 3"})

    def test_zero_retention_releases_completed_agents_but_keeps_active_sessions(self) -> None:
        self.config = merge_config(self.config, {"retention": {"agent_sessions": 0}})
        active = self.workspace("agent:active")
        active.init("Active")
        active.publish_note("Active context")
        completed = self.workspace("agent:completed")
        completed.init("Completed")
        completed.finish("Discarded summary")
        self.assertEqual([item["agent_id"] for item in active.public_dashboard()["agents"]], [active.agent_id])
        self.assertEqual([item["content"] for item in active.public_dashboard()["context"]], ["Active context"])
        self.assertTrue(active.exists())
        self.assertFalse(completed.exists())

    def test_init_reconciles_legacy_completed_stores_and_defers_busy_readers(self) -> None:
        legacy = self.workspace("agent:legacy")
        legacy.init("Legacy")
        legacy.publish_note("Legacy shared context")
        # Reproduce the old finish behavior: mark completed but leave the store.
        legacy._register_agent(status="finished")
        reader = MemoryGraph.open(legacy.paths.agent_storage, read_only=True)
        current = self.workspace("agent:current")
        try:
            result = current.init("Current")
            self.assertEqual(result["retention"]["busy_agents"], [legacy.agent_id])
            self.assertTrue(legacy.exists())
        finally:
            reader.close()
        result = current.init("Current")
        self.assertEqual(result["retention"]["busy_agents"], [])
        self.assertFalse(legacy.exists())
        self.assertTrue(current.exists())
        self.assertTrue(AgentWorkspace.search_dashboards(self.storage, "Legacy shared context")["results"])

    def test_finish_preserves_other_activity_using_same_private_store(self) -> None:
        self.config = merge_config(self.config, {"retention": {"agent_sessions": 0}})
        first = self.workspace("agent:shared", activity_id="first")
        second = self.workspace("agent:shared", activity_id="second")
        first.init("First")
        first.add_task("First task")
        second.init("Second")
        second.add_task("Second task")
        result = first.finish("First completed")
        self.assertEqual(result["status"], "active")
        self.assertTrue(second.exists())
        self.assertEqual([item["content"] for item in second.public_dashboard()["active_tasks"]], ["Second task"])
        self.assertEqual(second.status()["current_session_title"], "Second")
        second.finish("Second completed")
        self.assertFalse(second.exists())
        self.assertEqual(second.public_dashboard()["agents"], [])

    def test_terminate_closes_all_activities_and_releases_private_store(self) -> None:
        workspace = self.workspace("agent:target", activity_id="target-thread")
        workspace.init("Target")
        workspace.add_task("Interrupted work")
        other = self.workspace("agent:target", activity_id="other-thread")
        other.init("Other activity")
        result = AgentWorkspace.terminate_agent(self.storage, workspace.agent_id, config=self.config)
        self.assertEqual(result["status"], "terminated")
        self.assertFalse(workspace.exists())
        self.assertEqual(workspace.public_dashboard()["active_tasks"], [])
        self.assertTrue(AgentWorkspace.search_dashboards(self.storage, "Agent terminated")["results"])

    def test_cleanup_does_not_follow_untrusted_registered_store_paths(self) -> None:
        legacy = self.workspace("agent:legacy")
        legacy.init("Legacy")
        legacy._register_agent(status="finished")
        self.storage.write_bytes(b"canonical")
        graph = MemoryGraph.open(legacy.paths.dashboard_storage)
        try:
            identity = next(node for node in graph.store.all_nodes() if node.type == "agent")
            properties = dict(identity.properties)
            properties["agent_storage"] = str(self.storage)
            graph.store.update_node_fields(identity.id, properties=properties)
        finally:
            graph.close()
        result = self.workspace("agent:current").init("Current")
        self.assertEqual(result["retention"]["skipped_agents"], [legacy.agent_id])
        self.assertTrue(legacy.exists())
        self.assertEqual(self.storage.read_bytes(), b"canonical")
        with self.assertRaisesRegex(ValueError, "distinct paths"):
            AgentWorkspace(self.storage, agent_id="agent:unsafe", agent_storage=self.storage)

    def test_cleanup_preserves_store_aliased_by_an_active_identity(self) -> None:
        legacy = self.workspace("agent:alias/worker")
        legacy.init("Legacy")
        legacy._register_agent(status="finished")
        active = self.workspace("agent:alias_worker")
        result = active.init("Active alias")
        self.assertEqual(legacy.paths.agent_storage, active.paths.agent_storage)
        self.assertEqual(result["retention"]["skipped_agents"], [legacy.agent_id])
        self.assertTrue(active.exists())

    def test_concurrent_lifecycles_leave_only_bounded_public_summaries(self) -> None:
        def complete(number: int) -> None:
            """Exercise independent command lifecycles on shared storage."""
            workspace = self.workspace(f"agent:parallel-{number}")
            workspace.init(f"Work {number}")
            workspace.finish(f"Summary {number}")

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(complete, range(8)))
        dashboard = AgentWorkspace.read_public_dashboard(self.storage)
        self.assertEqual(len(dashboard["agents"]), 2)
        self.assertEqual(len(dashboard["context"]), 2)
        self.assertEqual(list((self.storage.parent / "agents").glob("*")), [])

    def test_cli_dashboard_finish_and_multi_agent_overview(self) -> None:
        base = [sys.executable, str(Path("cli.py").resolve()), "agent", "--agent", "agent:cli", "--no-progress"]
        subprocess.run([*base, "init", "--name", "CLI work"], cwd=self.storage.parent.parent, capture_output=True, text=True, check=True)
        args = build_parser().parse_args(["agent", "reject", "Cache every query", "Stale results"])
        self.assertEqual((args.agent_command, args.approach, args.reason),
                         ("reject", "Cache every query", "Stale results"))
        workspace = self.workspace("agent:cli")
        workspace.reject_approach(args.approach, args.reason)
        completed = workspace.add_task("Try direct parsing")["node"]
        workspace.complete_task(completed["id"], "Parsing works")
        workspace.add_task("Add verification")
        rendered = StringIO()
        with redirect_stdout(rendered):
            _print_agent_dashboard(workspace.dashboard())
        output = rendered.getvalue()
        self.assertIn("Rejected:\n", output)
        self.assertIn("Cache every query\tStale results", output)
        self.assertIn("Done:\n", output)
        self.assertIn("Try direct parsing\tParsing works", output)
        self.assertIn("Open:\n", output)
        self.assertIn("Add verification", output)
        self.assertTrue(output.rstrip().endswith("reql project overview"))
        self.assertEqual(workspace.dashboard()["overview_command"], "reql project overview")
        self.assertEqual(workspace.operational_overview()["done_tasks"][0]["completion_message"], "Parsing works")
        other = self.workspace("agent:other")
        other.init("Other work")
        other.reject_approach("Global cache", "Mixed tenants")
        other.add_task("Add isolation tests")

        class Explanation:
            """Avoid opening the canonical graph at the presentation boundary."""
            def to_markdown(self) -> str:
                return "# Sample project"

        class Graph:
            """Supply the unrelated repository explanation for this CLI test."""
            def explain_project(self, path: str) -> Explanation:
                self.path = path
                return Explanation()

        args = build_parser().parse_args(["project", "overview"])
        args.storage = str(self.storage)
        output = StringIO()
        graph = Graph()
        with redirect_stdout(output):
            self.assertEqual(_handle_project_overview(CommandContext(args, self.config, graph)), 0)
        self.assertEqual(graph.path, ".")
        for text in ("# Sample project", "Cache every query", "Stale results", "Mixed tenants",
                     "Parsing works", "Global cache", "Try direct parsing", "Add isolation tests",
                     "agent:cli", "agent:other"):
            with self.subTest(text=text):
                self.assertIn(text, output.getvalue())
        self.assertEqual(len(AgentWorkspace.project_operational_overview(self.storage)["agents"]), 2)
        result = subprocess.run([*base, "finish", "CLI summary"], cwd=self.storage.parent.parent, capture_output=True, text=True, check=True)
        self.assertIn("Retention: files=", result.stdout)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(_handle_project_overview(CommandContext(args, self.config, graph)), 0)
        self.assertIn("Public summaries", output.getvalue())
        self.assertIn("CLI summary", output.getvalue())
        self.assertNotIn("No Agent Workspace history", output.getvalue())
        other.finish("Other work retained")
