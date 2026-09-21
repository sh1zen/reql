from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from memory.agent.workspace import AgentWorkspace
from memory.cli import _main, _print_agent_dashboard, _print_agent_map, build_parser
from memory.domain.models import MemoryEdge, MemoryNode


class AgentWorkspaceContextTests(unittest.TestCase):
    def _workspace(self, root: Path) -> tuple[AgentWorkspace, str]:
        standard_storage = root / "memory.reql"
        graph = MemoryGraph.open(standard_storage)
        try:
            graph.add_node(
                MemoryNode(
                    id="project:context-test",
                    type="Project",
                    label="Context Test",
                    text=str(root / "project"),
                    canonical_key=str(root / "project"),
                    properties={
                        "id": "project:context-test",
                        "name": "Context Test",
                        "root_path": str(root / "project"),
                        "status": "active",
                    },
                    status="active",
                )
            )
            graph.add_node(
                MemoryNode(
                    id="artifact:workspace",
                    type="SourceArtifact",
                    label="src/workspace.py",
                    text="file:///src/workspace.py",
                    canonical_key="project:context-test:src/workspace.py",
                    properties={
                        "relative_path": "src/workspace.py",
                        "project_id": "project:context-test",
                    },
                    status="active",
                )
            )
        finally:
            graph.close()

        workspace = AgentWorkspace(
            standard_storage,
            agent_id="context-test",
            agent_storage=root / "agent.reql",
            bus_storage=root / "bus.reql",
            activity_id="activity-test",
        )
        workspace.init()
        return workspace, "artifact:workspace"

    def test_map_contains_only_operational_learning_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace, _ = self._workspace(Path(temporary_directory))

            first_session = workspace.start_session("Initial investigation")["session"]
            completed_task = workspace.add_task("Identify the context owner")["node"]
            workspace.complete_task(completed_task["id"])
            decision = workspace.add_decision("Keep one authoritative project model")["node"]
            workspace.add_finding("Session nodes already retain timestamps")

            second_session = workspace.start_session("Implementation")["session"]
            open_task = workspace.add_task("Build the optimized context map")["node"]
            workspace.add_note("Keep the compatibility fields")
            workspace.link(open_task["id"], decision["id"], "implements")

            payload = workspace.map()

            self.assertEqual(payload["context_format"], "reql-agent-context-v3")
            context = payload["context"]
            self.assertNotIn("project", context)
            self.assertNotIn("files", payload)
            self.assertNotIn("symbols", payload)

            learned = context["learned"]
            self.assertEqual(learned["decisions"][0]["session_id"], first_session["id"])
            self.assertEqual(learned["findings"][0]["session_id"], first_session["id"])
            self.assertEqual(learned["notes"][0]["session_id"], second_session["id"])

            sessions = context["sessions"]
            self.assertEqual(sessions["current"]["id"], second_session["id"])
            self.assertEqual(sessions["previous"][0]["id"], first_session["id"])
            self.assertEqual(sessions["previous"][0]["completed_task_count"], 1)
            self.assertIn(
                "Identify the context owner",
                {item["title"] for item in sessions["previous"][0]["highlights"]},
            )

            self.assertEqual(payload["open_tasks"][0]["id"], open_task["id"])
            self.assertIn("decisions", payload)
            self.assertIn("relations", payload)

            focused = workspace.map(session="current")
            self.assertEqual(focused["context"]["sessions"]["current"]["id"], second_session["id"])
            self.assertEqual(focused["context"]["sessions"]["previous"], [])
            self.assertEqual(
                focused["context"]["learned"]["decisions"][0]["title"],
                "Keep one authoritative project model",
            )
            self.assertEqual(focused["context"]["learned"]["notes"][0]["session_id"], second_session["id"])

    def test_cli_map_labels_each_context_domain(self) -> None:
        payload = {
            "context": {
                "learned": {
                    "decisions": [
                        {"id": "decision:one", "type": "decision", "title": "Keep context distinct"}
                    ],
                    "findings": [],
                    "notes": [],
                    "risks": [],
                    "plans": [],
                },
                "sessions": {
                    "current": None,
                    "previous": [
                        {
                            "id": "session:old",
                            "title": "Previous pass",
                            "status": "closed",
                            "completed_task_count": 1,
                            "open_task_count": 0,
                            "highlights": [],
                        }
                    ],
                },
            },
            "open_tasks": [],
            "decisions": [],
            "relations": [],
        }
        output = StringIO()

        with redirect_stdout(output):
            _print_agent_map(payload)

        rendered = output.getvalue()
        self.assertNotIn("Project:\n", rendered)
        self.assertNotIn("Relevant files:\n", rendered)
        self.assertIn("Agent memory:\n", rendered)
        self.assertIn("Keep context distinct", rendered)
        self.assertIn("Current session:\n", rendered)
        self.assertIn("Previous sessions:\n", rendered)
        self.assertIn("Previous pass", rendered)

    def test_agent_command_surface_excludes_project_graph_operations(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(output):
            build_parser().parse_args(["agent", "--help"])

        rendered = output.getvalue()
        self.assertNotIn("link-task", rendered)
        self.assertNotIn("sync", rendered)
        self.assertIn("decision", rendered)
        self.assertIn("task", rendered)
        self.assertIn("session", rendered)
        self.assertIn("dashboard", rendered)
        self.assertIn("note", rendered)
        self.assertIn("Deprecated compatibility alias", rendered)

        parsed = build_parser().parse_args(["agent", "note", "add", "typed note"])
        self.assertEqual(parsed.agent_command, "note")
        self.assertEqual(parsed.agent_note_command, "add")
        with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
            build_parser().parse_args(["agent", "add", "legacy note"])

    def test_dashboard_coordinates_current_previous_and_parallel_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, _ = self._workspace(root)
            workspace.start_session("Discovery")
            completed = workspace.add_task("Find the command owner")["node"]
            workspace.complete_task(completed["id"])
            workspace.start_session("Dashboard implementation")
            task = workspace.add_task("Implement compact coordination")["node"]
            workspace.add_decision("Keep project facts in the canonical graph")

            peer = AgentWorkspace(
                root / "memory.reql",
                agent_id="peer",
                agent_storage=root / "peer.reql",
                bus_storage=root / "bus.reql",
            )
            peer.init()
            peer.start_session("Parser review")
            peer.add_task("Inspect parser ownership")
            peer.publish("parser owner found", target=workspace.agent_id)
            peer.publish("private peer message", target="someone-else")

            payload = workspace.dashboard(post="implementation started", kind="stage", limit=3)
            detailed = workspace.dashboard(limit=3, include_agent_details=True)

            self.assertEqual(payload["format"], "reql-agent-dashboard-v1")
            self.assertEqual(payload["session"]["title"], "Dashboard implementation")
            self.assertEqual(payload["previous_session"]["title"], "Discovery")
            self.assertEqual(payload["work"][0]["id"], task["id"])
            self.assertEqual(payload["posted"]["text"], "implementation started")
            self.assertIn("parser owner found", {item["text"] for item in payload["messages"]})
            self.assertNotIn("private peer message", {item["text"] for item in payload["messages"]})
            commands = {item["command"] for item in payload["drilldowns"]}
            self.assertIn(f"reql agent show {task['id']} --json", commands)
            self.assertIn('reql query_context --query "<task terms>" --code', commands)
            self.assertIn('reql agent finish "<compact outcome>"', commands)
            self.assertIn("peer", {item["agent_id"] for item in payload["working_agents"]})
            self.assertIn("peer", {item["agent_id"] for item in detailed["agent_details"]})

            output = StringIO()
            with redirect_stdout(output):
                _print_agent_dashboard(payload)
            rendered = output.getvalue()
            self.assertIn("agent context-test | session Dashboard implementation", rendered)
            self.assertIn("previous", rendered)
            self.assertIn("drill inspect active task", rendered)
            self.assertNotIn("updated_at", rendered)

            peer.finish("parser review complete")
            after_finish = workspace.dashboard(limit=3)
            self.assertNotIn("peer", {item["agent_id"] for item in after_finish["working_agents"]})
            self.assertIn("peer", {item["agent_id"] for item in after_finish["finished_agents"]})
            peer.start_session("Follow-up review")
            after_restart = workspace.dashboard(limit=3)
            self.assertIn("peer", {item["agent_id"] for item in after_restart["working_agents"]})

    def test_dashboard_rejects_long_posts_to_keep_shared_state_compact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace, _ = self._workspace(Path(temporary_directory))

            with self.assertRaisesRegex(ValueError, "limited to 240 characters"):
                workspace.dashboard(post="x" * 241)

    def test_batch_requires_typed_note_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace, _ = self._workspace(Path(temporary_directory))

            result = workspace.batch([{"op": "note.add", "text": "Typed note"}])
            self.assertEqual(result["results"][0]["node"]["type"], "note")
            with self.assertRaisesRegex(ValueError, "Unsupported batch operation: add"):
                workspace.batch([{"op": "add", "text": "Legacy note"}])

    def test_deprecated_alias_warns_without_contaminating_json_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            storage = str(Path(temporary_directory) / "memory.reql")
            common = ["--storage", storage, "agent", "--activity", "compat-test", "--no-progress"]
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(_main([*common, "init", "--json"]), 0)

            output = StringIO()
            errors = StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                result = _main([*common, "publish", "compatibility message", "--json"])

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["message"]["content"], "compatibility message")
            self.assertIn("deprecated", errors.getvalue())
            self.assertNotIn("warning", output.getvalue())

    def test_activity_identity_is_stable_and_initialization_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            standard = root / "memory.reql"
            graph = MemoryGraph.open(standard)
            graph.close()
            first = AgentWorkspace(standard, bus_storage=root / "bus.reql", activity_id="activity-test")
            first.init()
            task = first.add_task("Keep this task")
            second = AgentWorkspace(
                standard,
                bus_storage=root / "bus.reql",
                activity_id="activity-test",
            )
            self.assertEqual(first.agent_id, second.agent_id)
            self.assertEqual(second.selection_source, "activity")
            result = second.init()
            self.assertTrue(result["already_initialized"])
            self.assertEqual(second.show(task["node"]["id"])["node"]["title"], "Keep this task")

    def test_init_does_not_open_or_create_the_canonical_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            standard = root / "missing-memory.reql"
            workspace = AgentWorkspace(
                standard,
                agent_id="memory-only",
                agent_storage=root / "agent.reql",
                bus_storage=root / "bus.reql",
            )

            result = workspace.init()

            self.assertTrue(result["initialized"])
            self.assertFalse(standard.exists())
            self.assertNotIn("standard_storage", result)

    def test_init_migrates_legacy_canonical_copies_out_of_agent_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace, artifact_id = self._workspace(root)
            task = workspace.add_task("Preserve me")["node"]
            agent_graph = MemoryGraph.open(workspace.paths.agent_storage)
            try:
                agent_graph.add_node(
                    MemoryNode(
                        id=artifact_id,
                        type="file",
                        label="src/workspace.py",
                        properties={"source": "standard", "standard_type": "SourceArtifact"},
                    )
                )
                agent_graph.add_edge(
                    MemoryEdge(
                        id="agent-edge:legacy-code-link",
                        from_id=task["id"],
                        to_id=artifact_id,
                        type="touches",
                        properties={"source": "agent"},
                    )
                )
            finally:
                agent_graph.close()

            result = workspace.init()

            self.assertTrue(result["already_initialized"])
            self.assertEqual(result["removed_canonical_items"], 1)
            self.assertEqual(workspace.show(task["id"])["node"]["title"], "Preserve me")
            with self.assertRaisesRegex(ValueError, "Agent item not found"):
                workspace.show(artifact_id)
            with self.assertRaisesRegex(ValueError, "Link target not found in agent memory"):
                workspace.link(task["id"], artifact_id, "touches")

    def test_parallel_activities_are_isolated_and_visible_in_overview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            standard = root / "memory.reql"
            graph = MemoryGraph.open(standard)
            graph.close()
            left = AgentWorkspace(standard, bus_storage=root / "bus.reql", activity_id="left")
            right = AgentWorkspace(standard, bus_storage=root / "bus.reql", activity_id="right")
            left.init()
            right.init()
            left.add_task("Left only")
            right.add_task("Right only")

            self.assertNotEqual(left.agent_id, right.agent_id)
            self.assertEqual([item["title"] for item in left.map()["open_tasks"]], ["Left only"])
            self.assertEqual([item["title"] for item in right.map()["open_tasks"]], ["Right only"])
            overview = left.overview()
            self.assertEqual(overview["format"], "reql-agent-overview-v1")
            self.assertEqual({item["agent_id"] for item in overview["agents"]}, {left.agent_id, right.agent_id})

            with (
                patch.dict(os.environ, {"REQL_AGENT_ACTIVITY_ID": "", "CODEX_THREAD_ID": ""}),
                self.assertRaisesRegex(ValueError, "implicit selection is ambiguous"),
            ):
                AgentWorkspace(standard, bus_storage=root / "bus.reql")


if __name__ == "__main__":
    unittest.main()
