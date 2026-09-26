"""Regression coverage for rejected approaches in Agent Workspace."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from memory.agent import AgentWorkspace
from memory.cli import CommandContext, _handle_project_overview, _print_agent_dashboard, build_parser
from memory.config import default_config


class RejectedApproachTests(unittest.TestCase):
    """Exercise durable rejection history through the normal workspace API."""

    def test_rejections_survive_sessions_and_are_searchable_by_reason(self) -> None:
        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            storage = Path(directory) / "memory.reql"
            workspace = AgentWorkspace(storage, agent_id="agent:test")
            workspace.init("Tuesday")
            entry = workspace.reject_approach("Cache every query", "Stale results after source edits")["node"]
            task = workspace.add_task("Try direct parsing")["node"]
            workspace.complete_task(task["id"], "Parsing works")
            self.assertEqual(entry["reason"], "Stale results after source edits")
            self.assertEqual(entry["session_title"], "Tuesday")
            workspace.finish("Tuesday work ended")

            resumed = AgentWorkspace(storage, agent_id="agent:test")
            resumed.init("Wednesday")
            dashboard = resumed.dashboard()["private"]
            self.assertEqual(dashboard["rejected"][0]["id"], entry["id"])
            self.assertEqual(dashboard["done"][0]["id"], task["id"])
            self.assertEqual(dashboard["done"][0]["completion_message"], "Parsing works")
            overview = resumed.operational_overview()
            self.assertEqual(overview["rejected_approaches"][0]["reason"], entry["reason"])
            results = AgentWorkspace.search_dashboards(storage, "stale source edits")["results"]
            self.assertTrue(any(item.get("id") == entry["id"] for item in results))
            resumed.finish("Wednesday work ended")

    def test_rejection_requires_approach_and_reason(self) -> None:
        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            workspace = AgentWorkspace(Path(directory) / "memory.reql", agent_id="agent:test")
            workspace.init("Validation")
            with self.assertRaisesRegex(ValueError, "reason must not be empty"):
                workspace.reject_approach("Cache every query", " ")
            with self.assertRaisesRegex(ValueError, "content must not be empty"):
                workspace.reject_approach(" ", "Stale results")
            workspace.finish("Validation done")

    def test_dashboard_shows_rejected_done_open_and_overview_command(self) -> None:
        args = build_parser().parse_args(["agent", "reject", "Cache every query", "Stale results"])
        self.assertEqual((args.agent_command, args.approach, args.reason),
                         ("reject", "Cache every query", "Stale results"))
        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            workspace = AgentWorkspace(Path(directory) / "memory.reql", agent_id="agent:test")
            workspace.init("Tuesday")
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
            workspace.finish("Rendered")

    def test_project_overview_combines_project_and_all_agents_history(self) -> None:
        class Explanation:
            def to_dict(self) -> dict[str, str]:
                return {"project": "sample"}

            def to_markdown(self) -> str:
                return "# Sample project"

        class Graph:
            def explain_project(self, path: str) -> Explanation:
                self.path = path
                return Explanation()

        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            storage = Path(directory) / ".reql" / "memory.reql"
            workspace = AgentWorkspace(storage, agent_id="agent:test")
            workspace.init("Tuesday")
            workspace.reject_approach("Cache every query", "Stale results")
            task = workspace.add_task("Try direct parsing")["node"]
            workspace.complete_task(task["id"], "Parsing works")
            other = AgentWorkspace(storage, agent_id="agent:other")
            other.init("Wednesday")
            other.reject_approach("Global cache", "Mixed tenants")
            other.add_task("Add isolation tests")
            args = build_parser().parse_args(["project", "overview"])
            args.storage = str(storage)
            graph = Graph()
            rendered = StringIO()
            with redirect_stdout(rendered):
                self.assertEqual(_handle_project_overview(CommandContext(args, default_config(), graph)), 0)
            self.assertEqual(graph.path, ".")
            self.assertIn("# Sample project", rendered.getvalue())
            self.assertIn("Cache every query — Stale results", rendered.getvalue())
            self.assertIn("Global cache — Mixed tenants", rendered.getvalue())
            self.assertIn("Try direct parsing — Parsing works", rendered.getvalue())
            self.assertIn("Add isolation tests", rendered.getvalue())
            self.assertIn("agent:test", rendered.getvalue())
            self.assertIn("agent:other", rendered.getvalue())
            self.assertEqual(len(AgentWorkspace.project_operational_overview(storage)["agents"]), 2)
            workspace.finish("Overview rendered")
            other.finish("Other work retained")
