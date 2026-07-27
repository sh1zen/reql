from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.agent import AgentWorkspace
from memory.agent import workspace as workspace_module
from memory.domain.models import MemoryEdge, MemoryNode


class AgentWorkspaceTests(unittest.TestCase):
    def _standard_graph(self, root: Path) -> Path:
        storage = root / ".reql" / "memory.reql"
        storage.parent.mkdir(parents=True, exist_ok=True)
        graph = MemoryGraph.open(storage)
        try:
            file_node = MemoryNode(
                id="artifact:app",
                type="SourceArtifact",
                label="app.py",
                text="def target(): return 1",
                properties={"relative_path": "app.py", "path": str(root / "app.py")},
            )
            other_file = MemoryNode(
                id="artifact:other",
                type="SourceArtifact",
                label="other.py",
                text="def other(): return 2",
                properties={"relative_path": "other.py", "path": str(root / "other.py")},
            )
            symbol_node = MemoryNode(
                id="function:target",
                type="Function",
                label="target",
                text="def target(): return 1",
                properties={"qualified_name": "app.target", "relative_path": "app.py", "line_start": 1},
            )
            graph.add_node(file_node)
            graph.add_node(other_file)
            graph.add_node(symbol_node)
            graph.add_edge(MemoryEdge(id="edge:defined", from_id="artifact:app", to_id="function:target", type="DEFINES"))
        finally:
            graph.close()
        return storage

    def _run_cli(self, storage: Path, *args: str) -> dict[str, object]:
        env = dict(os.environ)
        env["PYTHONPATH"] = "src"
        completed = subprocess.run(
            [sys.executable, "-m", "memory.cli", "--storage", str(storage), *args],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return json.loads(completed.stdout)

    def test_init_derives_standard_graph_references(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)

            result = workspace.init()
            status = workspace.status()
            exported = workspace.export(include_metadata=True)

            self.assertTrue(result["initialized"])
            self.assertEqual(result["derived_nodes"], 3)
            self.assertEqual(result["derived_relations"], 1)
            self.assertTrue(status["exists"])
            self.assertEqual(status["derived_nodes"], 3)
            by_id = {node["id"]: node for node in exported["nodes"]}
            self.assertEqual(by_id["artifact:app"]["type"], "file")
            self.assertEqual(by_id["artifact:app"]["source"], "standard")
            self.assertEqual(by_id["function:target"]["type"], "symbol")
            self.assertEqual(by_id["function:target"]["standard_type"], "Function")

    def test_agent_changes_do_not_modify_standard_graph(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)
            workspace.init()

            note = workspace.add_note("Read app.py and found target implementation")["node"]

            standard = MemoryGraph.open(storage)
            try:
                self.assertIsNone(standard.get_node(note["id"]))
                self.assertEqual(standard.store.count_nodes(), 3)
            finally:
                standard.close()

    def test_sync_refreshes_derived_graph_and_preserves_agent_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)
            workspace.init()
            task = workspace.add_task("Wire estimate_context_savings into the agent map")["node"]
            decision = workspace.add_decision("Refresh derived nodes instead of resetting agent memory")["node"]
            relation = workspace.link(task["id"], "artifact:app", "touches")["relation"]

            standard = MemoryGraph.open(storage)
            try:
                new_symbol = MemoryNode(
                    id="function:estimate_context_savings",
                    type="Function",
                    label="estimate_context_savings",
                    text="def estimate_context_savings(): return 42",
                    properties={
                        "qualified_name": "app.estimate_context_savings",
                        "relative_path": "app.py",
                        "line_start": 3,
                    },
                )
                standard.add_node(new_symbol)
                standard.add_edge(
                    MemoryEdge(
                        id="edge:estimate",
                        from_id="artifact:app",
                        to_id="function:estimate_context_savings",
                        type="DEFINES",
                    )
                )
                standard.store.remove_node("artifact:other")
            finally:
                standard.close()

            synced = workspace.sync()
            linked = workspace.link(task["id"], "function:estimate_context_savings", "implements")["relation"]
            exported = workspace.export(include_metadata=True)
            by_id = {node["id"]: node for node in exported["nodes"]}
            relation_ids = {item["id"] for item in exported["relations"]}

            self.assertTrue(synced["synced"])
            self.assertEqual(synced["derived_nodes"], 3)
            self.assertEqual(synced["preserved_agent_nodes"], 2)
            self.assertIn(task["id"], by_id)
            self.assertIn(decision["id"], by_id)
            self.assertIn("function:estimate_context_savings", by_id)
            self.assertNotIn("artifact:other", by_id)
            self.assertIn(relation["id"], relation_ids)
            self.assertIn(linked["id"], relation_ids)

    def test_add_complete_and_query_working_memory_items(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)
            workspace.init()

            note = workspace.add_note("Remember parser behavior in app.py")["node"]
            task = workspace.add_task("Update target implementation")["node"]
            done = workspace.complete_task(task["id"])["task"]
            decision = workspace.add_decision("Use the existing graph store instead of a new format")["node"]
            finding = workspace.add_finding("target has no callers in the sample graph")["node"]
            relation = workspace.link(task["id"], "artifact:app", "touches")["relation"]
            search = workspace.search("graph store format", node_type="decision")
            shown = workspace.show(task["id"])
            listed = workspace.list_items(node_type="task", status="done")
            mapped = workspace.map()
            exported = workspace.export(include_metadata=True)

            self.assertEqual(note["type"], "note")
            self.assertEqual(done["status"], "done")
            self.assertEqual(decision["type"], "decision")
            self.assertEqual(finding["type"], "finding")
            self.assertEqual(relation["from_id"], task["id"])
            self.assertEqual(relation["to_id"], "artifact:app")
            self.assertEqual(relation["relation"], "touches")
            self.assertEqual(search["results"][0]["node"]["id"], decision["id"])
            self.assertEqual(shown["kind"], "node")
            self.assertEqual(listed["nodes"][0]["id"], task["id"])
            self.assertEqual([item["id"] for item in listed["relations"]], [relation["id"]])
            self.assertNotIn("findings", mapped)
            self.assertNotIn("agent_findings", mapped)
            self.assertEqual([item["id"] for item in mapped["files"]], ["artifact:app"])
            self.assertEqual(exported["format"], "reql-agent-workspace-v1")
            self.assertTrue(any(item["id"] == relation["id"] for item in exported["relations"]))

    def test_link_many_and_batch_apply_multiple_agent_writes_per_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)
            workspace.init()
            task = workspace.add_task("Update target implementation")["node"]

            linked = workspace.link_many(task["id"], ["artifact:app", "function:target"], "touches")
            batch = workspace.batch(
                [
                    {"op": "decision.add", "text": "Batch agent writes to reduce lock churn", "as": "decision"},
                    {"op": "finding.add", "text": "link-many covers repeated relations", "as": "finding"},
                    {"op": "link", "from": task["id"], "to": "$decision", "relation": "implements"},
                    {"op": "link-many", "from": task["id"], "to": ["$finding", "artifact:other"], "relation": "related_to"},
                ]
            )
            exported = workspace.export(include_metadata=True)
            relation_pairs = {
                (item["from_id"], item["to_id"], item["relation"])
                for item in exported["relations"]
                if item["source"] == "agent"
            }

            self.assertEqual(linked["created"], 2)
            self.assertEqual(batch["operations"], 4)
            self.assertIn("decision", batch["aliases"])
            self.assertIn("finding", batch["aliases"])
            self.assertIn((task["id"], "artifact:app", "touches"), relation_pairs)
            self.assertIn((task["id"], "function:target", "touches"), relation_pairs)
            self.assertIn((task["id"], batch["aliases"]["decision"], "implements"), relation_pairs)
            self.assertIn((task["id"], batch["aliases"]["finding"], "related_to"), relation_pairs)
            self.assertIn((task["id"], "artifact:other", "related_to"), relation_pairs)

    def test_link_task_resolves_file_path_without_manual_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)
            workspace.init()
            task = workspace.add_task("Patch context savings")["node"]

            linked = workspace.link_task(task_id=task["id"], file_path="app.py")

            self.assertEqual(linked["task"]["id"], task["id"])
            self.assertEqual(linked["target"]["id"], "artifact:app")
            self.assertEqual(linked["relation"]["from_id"], task["id"])
            self.assertEqual(linked["relation"]["to_id"], "artifact:app")
            self.assertEqual(linked["relation"]["relation"], "touches")

    def test_export_defaults_to_compact_map_and_metadata_exports_full_graph(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)
            workspace.init()
            task = workspace.add_task("Patch compact agent export")["node"]
            decision = workspace.add_decision("Keep full export behind metadata flag")["node"]
            workspace.link(task["id"], decision["id"], "implements")
            workspace.link(task["id"], "artifact:app", "touches")

            compact = workspace.export()
            detailed = workspace.export(include_metadata=True)

            self.assertEqual(set(compact), {"format", "open_tasks", "decisions", "files", "symbols", "relations"})
            self.assertEqual([item["id"] for item in compact["open_tasks"]], [task["id"]])
            self.assertEqual([item["id"] for item in compact["decisions"]], [decision["id"]])
            self.assertNotIn("nodes", compact)
            self.assertIn("nodes", detailed)
            self.assertIn("agent_storage", detailed)

    def test_sessions_scope_agent_map_to_current_or_selected_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)
            workspace.init()
            historical = workspace.add_task("Historical task")["node"]
            first_session = workspace.start_session("First focused session")["session"]
            first_task = workspace.add_task("First session task")["node"]
            workspace.link(first_task["id"], "artifact:app", "touches")
            second_session = workspace.start_session("Second focused session")["session"]
            second_task = workspace.add_task("Second session task")["node"]
            workspace.link(second_task["id"], "function:target", "touches")

            current = workspace.map(session="current")
            first = workspace.map(session=first_session["id"])
            exported = workspace.export(include_metadata=True)
            by_id = {node["id"]: node for node in exported["nodes"]}

            self.assertEqual(current["filters"]["session"], second_session["id"])
            self.assertEqual([item["id"] for item in current["open_tasks"]], [second_task["id"]])
            self.assertEqual([item["id"] for item in current["symbols"]], ["function:target"])
            self.assertNotIn(historical["id"], {item["id"] for item in current["open_tasks"]})
            self.assertEqual(first["filters"]["session"], first_session["id"])
            self.assertEqual([item["id"] for item in first["open_tasks"]], [first_task["id"]])
            self.assertEqual([item["id"] for item in first["files"]], ["artifact:app"])
            self.assertEqual(by_id[first_session["id"]]["status"], "closed")
            self.assertEqual(by_id[second_session["id"]]["status"], "active")
            metadata = workspace.status()["metadata"]
            self.assertEqual(list(metadata["current_session_ids"].values()), [second_session["id"]])

    def test_current_sessions_are_isolated_by_activity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            first = AgentWorkspace(storage, activity_id="thread:first")
            second = AgentWorkspace(storage, activity_id="thread:second")
            first.init()

            first_session = first.start_session("First activity")["session"]
            second_session = second.start_session("Second activity")["session"]

            self.assertEqual(first.status()["current_session_id"], first_session["id"])
            self.assertEqual(second.status()["current_session_id"], second_session["id"])
            metadata = first.status()["metadata"]
            self.assertEqual(
                metadata["current_session_ids"],
                {
                    "thread:first": first_session["id"],
                    "thread:second": second_session["id"],
                },
            )

    def test_multiple_agents_keep_private_memory_and_share_bus_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            alpha = AgentWorkspace(storage, agent_id="alpha")
            beta = AgentWorkspace(storage, agent_id="beta")
            alpha_init = alpha.init()
            beta.init()
            alpha_task = alpha.add_task("Alpha task")["node"]
            alpha.link(alpha_task["id"], "artifact:app", "touches")
            beta_task = beta.add_task("Beta task")["node"]
            beta.link(beta_task["id"], "function:target", "touches")

            alpha_map = alpha.map()
            beta_map = beta.map()
            message = alpha.publish("Alpha found the serializer boundary", target="master")["message"]
            handoff = alpha.handoff("Alpha completed serializer review")["handoff"]
            bus = beta.bus()
            full_bus = beta.bus(include_payloads=True)

            self.assertEqual(alpha_init["agent_id"], "alpha")
            self.assertNotEqual(alpha_init["agent_storage"], str(beta.paths.agent_storage))
            self.assertEqual([item["id"] for item in alpha_map["open_tasks"]], [alpha_task["id"]])
            self.assertEqual([item["id"] for item in beta_map["open_tasks"]], [beta_task["id"]])
            self.assertNotIn(alpha_task["id"], {item["id"] for item in beta_map["open_tasks"]})
            self.assertEqual(message["agent_id"], "alpha")
            self.assertEqual(handoff["target_agent_id"], "master")
            self.assertIn("payload", handoff)
            self.assertEqual([item["id"] for item in handoff["payload"]["open_tasks"]], [alpha_task["id"]])
            self.assertIn("alpha", {item["agent_id"] for item in bus["agents"]})
            self.assertIn("beta", {item["agent_id"] for item in bus["agents"]})
            self.assertIn(handoff["id"], {item["id"] for item in bus["handoffs"]})
            compact_handoff = next(item for item in bus["handoffs"] if item["id"] == handoff["id"])
            full_handoff = next(item for item in full_bus["handoffs"] if item["id"] == handoff["id"])
            self.assertNotIn("payload", compact_handoff)
            self.assertEqual([item["id"] for item in full_handoff["payload"]["open_tasks"]], [alpha_task["id"]])

    def test_lock_contention_returns_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            workspace = AgentWorkspace(storage)
            workspace.init()
            locked = MemoryGraph.open(AgentWorkspace.default_agent_storage(storage))
            original_timeout = workspace_module.AGENT_LOCK_TIMEOUT_SECONDS
            original_read_timeout = workspace_module.AGENT_READ_LOCK_TIMEOUT_SECONDS
            original_attempts = workspace_module.AGENT_LOCK_RETRY_ATTEMPTS
            original_delay = workspace_module.AGENT_LOCK_RETRY_DELAY_SECONDS
            try:
                workspace_module.AGENT_LOCK_TIMEOUT_SECONDS = 0.01
                workspace_module.AGENT_READ_LOCK_TIMEOUT_SECONDS = 0.01
                workspace_module.AGENT_LOCK_RETRY_ATTEMPTS = 2
                workspace_module.AGENT_LOCK_RETRY_DELAY_SECONDS = 0.01
                with self.assertRaisesRegex(ValueError, r"Agent workspace is busy;.*lock wait budget of 0\.03s"):
                    workspace.add_note("parallel write")
            finally:
                locked.close()
                workspace_module.AGENT_LOCK_TIMEOUT_SECONDS = original_timeout
                workspace_module.AGENT_READ_LOCK_TIMEOUT_SECONDS = original_read_timeout
                workspace_module.AGENT_LOCK_RETRY_ATTEMPTS = original_attempts
                workspace_module.AGENT_LOCK_RETRY_DELAY_SECONDS = original_delay

    def test_cli_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            env = dict(os.environ)
            env["PYTHONPATH"] = "src"

            init = subprocess.run(
                [sys.executable, "-m", "memory.cli", "--storage", str(storage), "agent", "init", "--json"],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            add = subprocess.run(
                [sys.executable, "-m", "memory.cli", "--storage", str(storage), "agent", "task", "add", "Patch CLI", "--json"],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            status = subprocess.run(
                [sys.executable, "-m", "memory.cli", "--storage", str(storage), "agent", "--no-progress", "status", "--json"],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )

            self.assertEqual(json.loads(init.stdout)["derived_nodes"], 3)
            self.assertEqual(json.loads(add.stdout)["node"]["type"], "task")
            self.assertEqual(json.loads(status.stdout)["agent_nodes"], 1)
            self.assertIn("[agent] agent init: started", init.stderr)
            self.assertIn("[agent] agent init: completed in", init.stderr)
            self.assertEqual(status.stderr, "")

    def test_cli_agent_session_start_and_map_current_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            env = dict(os.environ)
            env["PYTHONPATH"] = "src"
            subprocess.run(
                [sys.executable, "-m", "memory.cli", "--storage", str(storage), "agent", "init", "--json"],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            session = subprocess.run(
                [sys.executable, "-m", "memory.cli", "--storage", str(storage), "agent", "session", "start", "CLI session", "--json"],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            task = subprocess.run(
                [sys.executable, "-m", "memory.cli", "--storage", str(storage), "agent", "task", "add", "Session task", "--json"],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            mapped = subprocess.run(
                [sys.executable, "-m", "memory.cli", "--storage", str(storage), "agent", "map", "--session", "current", "--json"],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )

            session_id = json.loads(session.stdout)["session"]["id"]
            task_id = json.loads(task.stdout)["node"]["id"]
            payload = json.loads(mapped.stdout)
            self.assertEqual(payload["filters"]["session"], session_id)
            self.assertEqual([item["id"] for item in payload["open_tasks"]], [task_id])

    def test_cli_agent_init_selects_bus_agent_and_handoff_uses_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            storage = self._standard_graph(Path(td))
            init_payload = self._run_cli(storage, "agent", "init", "--json")
            task_id = self._run_cli(storage, "agent", "task", "add", "CLI private task", "--json")["node"]["id"]
            handoff_payload = self._run_cli(storage, "agent", "handoff", "CLI worker done", "--json")["handoff"]
            bus_payload = self._run_cli(storage, "agent", "bus", "--json")
            full_bus_payload = self._run_cli(storage, "agent", "bus", "--include-payloads", "--json")
            self.assertTrue(init_payload["agent_id"].startswith("agent:"))
            self.assertNotEqual(init_payload["agent_storage"], str(AgentWorkspace.default_agent_storage(storage)))
            self.assertEqual(bus_payload["current_agent_id"], init_payload["agent_id"])
            self.assertIn(init_payload["agent_id"], {item["agent_id"] for item in bus_payload["agents"]})
            self.assertEqual(handoff_payload["agent_id"], init_payload["agent_id"])
            self.assertEqual([item["id"] for item in handoff_payload["payload"]["open_tasks"]], [task_id])
            compact_handoff = next(item for item in bus_payload["handoffs"] if item["id"] == handoff_payload["id"])
            full_handoff = next(item for item in full_bus_payload["handoffs"] if item["id"] == handoff_payload["id"])
            self.assertNotIn("payload", compact_handoff)
            self.assertEqual([item["id"] for item in full_handoff["payload"]["open_tasks"]], [task_id])

    def test_cli_agent_batch_file_and_inline_inputs_link_aliases(self) -> None:
        for input_mode in ("file", "inline"):
            with self.subTest(input_mode=input_mode), tempfile.TemporaryDirectory() as td:
                storage = self._standard_graph(Path(td))
                self._run_cli(storage, "agent", "init", "--json")

                if input_mode == "file":
                    batch_file = Path(td) / "agent-batch.json"
                    batch_file.write_text(
                        json.dumps(
                            {
                                "operations": [
                                    {"op": "task.add", "description": "Patch batch command", "as": "task"},
                                    {"op": "decision.add", "text": "Use a single agent workspace lock", "as": "decision"},
                                    {"op": "link", "from": "$task", "to": "$decision", "relation": "implements"},
                                    {
                                        "op": "link-many",
                                        "from": "$task",
                                        "to": ["artifact:app", "function:target"],
                                        "relation": "touches",
                                    },
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    args = ("agent", "batch", "--json", str(batch_file))
                else:
                    args = (
                        "agent",
                        "batch",
                        "--task",
                        "task=Patch inline batch command",
                        "--decision",
                        "decision=Use inline batch for small planning",
                        "--link",
                        "$task",
                        "implements",
                        "$decision",
                        "--touches",
                        "$task",
                        "artifact:app,function:target",
                        "--json",
                    )

                payload = self._run_cli(storage, *args)
                task_id = payload["aliases"]["task"]
                decision_id = payload["aliases"]["decision"]
                exported = AgentWorkspace(storage).export(include_metadata=True)
                relation_pairs = {
                    (item["from_id"], item["to_id"], item["relation"])
                    for item in exported["relations"]
                    if item["source"] == "agent"
                }

                self.assertEqual(payload["operations"], 4)
                self.assertIn((task_id, decision_id, "implements"), relation_pairs)
                self.assertIn((task_id, "artifact:app", "touches"), relation_pairs)
                self.assertIn((task_id, "function:target", "touches"), relation_pairs)


if __name__ == "__main__":
    unittest.main()
