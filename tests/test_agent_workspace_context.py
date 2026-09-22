"""Dashboard-centred Agent Workspace integration tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory.agent.workspace import AgentWorkspace
from memory.cli import build_parser


class AgentWorkspaceContextTests(unittest.TestCase):
    def _workspace(self, root: Path, agent_id: str) -> AgentWorkspace:
        return AgentWorkspace(root / ".reql" / "memory.reql", agent_id=agent_id)

    def test_init_registers_an_active_session_and_public_agent(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp") as directory:
            workspace = self._workspace(Path(directory), "agent:alpha")
            result = workspace.init(name="Parser work")
            self.assertTrue(result["session"])
            public = workspace.public_dashboard()
            agent = next(item for item in public["agents"] if item["agent_id"] == "agent:alpha")
            self.assertEqual(agent["status"], "active")
            self.assertTrue(agent["session_started_at"])

    def test_notes_support_private_directed_and_public_delivery(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp") as directory:
            root = Path(directory)
            alpha = self._workspace(root, "agent:alpha")
            beta = self._workspace(root, "agent:beta")
            alpha.init()
            beta.init()
            alpha.add_note("private reminder")
            alpha.send_note("agent:beta", "check the new parser result")
            alpha.publish_note("all agents should use parse_document_v2")
            self.assertIn("private reminder", {item["content"] for item in alpha.private_dashboard()["private_notes"]})
            self.assertIn("check the new parser result", {item["content"] for item in beta.private_dashboard()["external_notes"]})
            self.assertIn("all agents should use parse_document_v2", {item["content"] for item in alpha.public_dashboard()["context"]})

    def test_task_completion_keeps_private_task_and_publishes_message(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp") as directory:
            workspace = self._workspace(Path(directory), "agent:alpha")
            workspace.init()
            task = workspace.add_task("Refactor parser")["node"]
            self.assertEqual(workspace.public_dashboard()["active_tasks"][0]["task_id"], task["id"])
            workspace.complete_task(task["id"], "Parser refactor completed and tests pass.")
            completed = next(item for item in workspace.private_dashboard()["tasks"] if item["id"] == task["id"])
            self.assertEqual(completed["status"], "done")
            public = workspace.public_dashboard()
            self.assertEqual(public["active_tasks"], [])
            self.assertIn("Parser refactor completed and tests pass.", {item["content"] for item in public["context"] if item["message_type"] == "task_completion"})

    def test_finish_preserves_private_history_and_publishes_final_context(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp") as directory:
            workspace = self._workspace(Path(directory), "agent:alpha")
            workspace.init()
            workspace.add_note("retain this")
            workspace.finish("Implementation complete; verify incremental indexing next.")
            self.assertTrue(workspace.paths.agent_storage.exists())
            self.assertEqual(workspace.private_dashboard()["agent"]["status"], "finished")
            self.assertIn("Implementation complete; verify incremental indexing next.", {item["content"] for item in workspace.public_dashboard()["context"] if item["message_type"] == "finish"})

    def test_listing_search_and_termination_use_dashboard_history(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp") as directory:
            root = Path(directory)
            alpha = self._workspace(root, "agent:alpha")
            beta = self._workspace(root, "agent:beta")
            alpha.init()
            beta.init()
            beta.add_note("incremental index requires verification")
            storage = root / ".reql" / "memory.reql"
            self.assertEqual({item["agent_id"] for item in AgentWorkspace.list_registered_agents(storage)}, {"agent:alpha", "agent:beta"})
            matches = AgentWorkspace.search_dashboards(storage, "incremental")
            self.assertEqual(matches["results"][0]["agent_id"], "agent:beta")
            AgentWorkspace.terminate_agent(storage, "agent:beta")
            self.assertEqual(beta.private_dashboard()["agent"]["status"], "terminated")
            self.assertIn("agent:beta", {item["agent_id"] for item in AgentWorkspace.list_registered_agents(storage, include_all=True)})

    def test_obsolete_commands_are_not_exposed(self) -> None:
        parser = build_parser()
        for arguments in (("agent", "bus"), ("agent", "map"), ("agent", "handoff", "message")):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                parser.parse_args(arguments)

    def test_new_cli_surface_has_required_commands(self) -> None:
        parser = build_parser()
        self.assertTrue(parser.parse_args(["agent", "list"]))
        self.assertTrue(parser.parse_args(["agent", "terminate", "agent:alpha"]))
        done = parser.parse_args(["agent", "task", "done", "task:1", "done message"])
        self.assertEqual(done.message, "done message")
        note = parser.parse_args(["agent", "note", "--agent", "agent:beta", "message"])
        self.assertEqual(note.target_agent_id, "agent:beta")


if __name__ == "__main__":
    unittest.main()
