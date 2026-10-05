"""Real persisted, multi-session engineering workflows through supported APIs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from api import MemoryGraph
from memory.agent import AgentWorkspace
from memory.config import default_config, merge_config
from memory.domain.models import MemoryNode
from memory.services.coordination import CoordinationStore, MAX_REVISIONS, render_work_context
from mcp.tools import query_context as mcp_context


class CoordinationWorkflowTests(unittest.TestCase):
    """Validate continuation, parallelism, changed plans, focused context and overview."""

    def setUp(self) -> None:
        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root / ".reql" / "memory.reql"
        self.work = CoordinationStore(self.storage.with_name("agent-dashboard.reql"))

    def agent(self, name: str) -> AgentWorkspace:
        """Start a separate identity/activity without shared private selection."""
        agent = AgentWorkspace(self.storage, agent_id=f"agent:{name}", activity_id=name)
        agent.init(name)
        return agent

    def update(self, agent: AgentWorkspace, item: dict, **fields: object) -> dict:
        """Reconcile an observed revision, as a resumed coding agent would."""
        return agent.record(item["kind"], item["content"], record_id=item["id"],
                            expected_revision=item["revision"], **fields)["node"]

    def test_A_session_continuation_survives_private_cleanup_and_zero_message_retention(self) -> None:
        first = self.agent("A")
        first.config = merge_config(default_config(), {"retention": {"agent_sessions": 0}})
        goal = first.record("goal", "Deliver bounded serializer", key="serializer")["node"]
        decision = first.add_decision("Keep schema version stable", rationale="Existing consumers depend on it",
                                     parent=goal["id"], files=["src/codec.py"])["node"]
        task = first.add_task("Implement serializer", parent=goal["id"], depends_on=[decision["id"]],
                              files=["src/codec.py", "src/client.py"])["node"]
        first.complete_task(task["id"], "Both consumers updated; codec tests pass")
        pending = first.add_task("Validate malformed payload", parent=goal["id"], files=["src/codec.py"],
                                 next_action="Add the missing malformed-input regression")["node"]
        question = first.record("question", "Should empty payload be legal?", parent=pending["id"],
                                files=["src/codec.py"])["node"]
        first.add_note("Scratch must not be published")
        first.finish("Serializer implemented; malformed-input handling remains")
        self.assertFalse(first.exists())
        self.assertEqual(first.public_dashboard()["context"], [])
        second = self.agent("B")
        recovered = self.work.context(files=["src/codec.py"], limit=8)
        ids = {item["id"] for item in recovered["records"]}
        self.assertTrue({goal["id"], decision["id"], task["id"], pending["id"], question["id"]}.issubset(ids))
        rendered = render_work_context(recovered)
        self.assertIn("Existing consumers", rendered)
        self.assertIn("malformed-input regression", rendered)
        self.assertNotIn("Scratch must not", rendered)
        self.assertIn(pending["id"], {item["id"] for item in self.work.context("malformed-input regression")["records"]})
        self.assertEqual(self.work.show(task["id"])["status"], "done")
        self.assertEqual(self.work.show(pending["id"])["origin_agent_id"], "agent:A")
        resumed = self.update(second, self.work.show(pending["id"]), status="in_progress")
        self.assertEqual(resumed["agent_id"], "agent:B")
        self.assertEqual(resumed["history"][-1]["agent_id"], "agent:A")

    def test_B_parallel_workstreams_expose_conflicts_and_integration_dependencies(self) -> None:
        parser, client = self.agent("parser"), self.agent("client")
        one = parser.add_decision("Return nullable result", rationale="Parser needs absent fields", workstream="parse")["node"]
        two = client.add_decision("Require complete result", rationale="UI needs complete fields", workstream="client",
                                  contradicts=[one["id"]])["node"]
        parse_task = parser.add_task("Parser result contract", workstream="parse")["node"]
        ui_task = client.add_task("Update UI consumer", workstream="client", depends_on=[parse_task["id"]])["node"]
        integration = client.add_task("Integrate result contract", workstream="integration",
                                      depends_on=[parse_task["id"], ui_task["id"], two["id"]])["node"]
        parser.complete_task(parse_task["id"], "Parser contract tests passed")
        parser.finish("Parser complete")
        client.finish("Consumer and integration remain")
        context = self.work.context(record_id=integration["id"], limit=8)
        items = {item["id"]: item for item in context["records"]}
        self.assertEqual(items[ui_task["id"]]["execution_state"], "ready")
        self.assertEqual(items[integration["id"]]["waiting_on"], [ui_task["id"]])
        self.assertIn(one["id"], items[two["id"]]["conflicts"])
        self.assertIn(two["id"], items[one["id"]]["conflicts"])
        overview = self.work.overview()
        self.assertEqual(set(overview["workstreams"]), {"parse", "client", "integration"})
        self.assertTrue(overview["sections"]["blockers"])

    def test_C_plan_replacement_preserves_cause_and_flags_obsolete_work(self) -> None:
        agent = self.agent("plan")
        old = agent.add_decision("Use cached parser", rationale="Reduce latency", files=["src/parser.py"])["node"]
        task = agent.add_task("Integrate cached parser", depends_on=[old["id"]], files=["src/parser.py"])["node"]
        new = agent.add_decision("Use direct parser", rationale="Cache returns stale results after edits",
                                 supersedes=[old["id"]], files=["src/parser.py"])["node"]
        current = self.work.context("parser", files=["src/parser.py"])
        ids = [item["id"] for item in current["records"]]
        self.assertIn(new["id"], ids)
        self.assertNotIn(old["id"], ids)
        current_task = next(item for item in current["records"] if item["id"] == task["id"])
        self.assertEqual(current_task["execution_state"], "needs_review")
        with self.assertRaisesRegex(ValueError, "obsolete"):
            agent.complete_task(task["id"], "Cannot pretend this plan is current")
        historical = self.work.context("parser", include_history=True)
        self.assertIn(old["id"], {item["id"] for item in historical["records"]})
        prior = self.work.show(old["id"])
        self.assertEqual(prior["status"], "superseded")
        self.assertEqual(prior["replaced_by"], new["id"])
        self.assertIn("stale", prior["replacement_reason"])
        self.assertEqual(prior["history"][-1]["status"], "active")
        with self.assertRaisesRegex(ValueError, "Already superseded"):
            agent.add_decision("Use yet another parser", rationale="Competing stale plan", supersedes=[old["id"]])
        updated = self.update(agent, self.work.show(task["id"]), depends_on=[new["id"]], rationale="Reconcile with direct parser")
        agent.complete_task(updated["id"], "Direct parser integration verified")

    def test_D_file_anchors_and_causal_links_beat_unrelated_recent_history(self) -> None:
        agent = self.agent("focus")
        constraint = agent.record("constraint", "Keep wire format stable", files=["src/codec.py"])["node"]
        failure = agent.record("failure", "Memoization dropped fields", rationale="Snapshot differs from current input")["node"]
        task = agent.add_task("Fix encoding", files=["src/codec.py"], depends_on=[constraint["id"], failure["id"]])["node"]
        decision = agent.add_decision("Preserve empty values", rationale="Wire contract requires round trips", parent=task["id"])["node"]
        for number in range(15):
            agent.record("change", f"UI styling improvement {number}", status="done", files=["src/ui.py"])
        result = self.work.context(files=["src/codec.py"], limit=6)
        ids = {item["id"] for item in result["records"]}
        self.assertTrue({constraint["id"], failure["id"], task["id"], decision["id"]}.issubset(ids))
        self.assertFalse(any("UI styling" in item["content"] for item in result["records"]))
        self.assertLessEqual(len(result["records"]), 6)
        self.assertLessEqual(len(render_work_context(result, max_chars=1700)), 1700)
        self.assertEqual(self.work.context("nonexistent-component")["records"], [])

    def test_E_overview_derives_next_steps_from_current_execution(self) -> None:
        agent = self.agent("overview")
        goal = agent.record("goal", "Improve context retrieval")["node"]
        one = agent.add_task("Index scope", parent=goal["id"], workstream="retrieval")["node"]
        two = agent.add_task("Wire context", parent=goal["id"], depends_on=[one["id"]], workstream="retrieval")["node"]
        agent.add_decision("Keep local deterministic core", rationale="No provider dependencies", parent=goal["id"])
        agent.record("question", "Which callers depend on envelope?", parent=two["id"])
        before = self.work.overview()
        self.assertEqual([item["id"] for item in before["sections"]["next"]], [one["id"]])
        agent.complete_task(one["id"], "Index tests pass")
        agent.finish("Index done; integration remains")
        after = CoordinationStore(self.work.path).overview()
        self.assertEqual([item["id"] for item in after["sections"]["next"]], [two["id"]])
        self.assertEqual(after["sections"]["goals"][0]["id"], goal["id"])
        self.assertEqual(after["sections"]["completed"][0]["id"], one["id"])
        self.assertTrue(after["sections"]["direction"])
        self.assertTrue(after["sections"]["blockers"])
        self.assertTrue(after["sections"]["checkpoints"])
        checkpoint = after["sections"]["checkpoints"][0]
        self.assertIn(goal["id"], checkpoint["summarizes"])
        self.assertEqual(checkpoint["waiting_on"], [])

    def test_duplicate_revision_conflict_and_atomic_supersession(self) -> None:
        agent = self.agent("atomic")
        decision = agent.add_decision("Use streaming", rationale="Bound allocations", key="transfer")["node"]
        before = self.work.path.read_bytes()
        duplicate = agent.add_decision("Use streaming", rationale="Bound allocations", key="transfer")
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["node"]["revision"], 1)
        self.assertEqual(self.work.path.read_bytes(), before)
        updated = self.update(agent, decision, rationale="Bound allocations and latency")
        with self.assertRaisesRegex(ValueError, "revision changed"):
            self.update(agent, decision, rationale="Stale competing assumption")
        with self.assertRaisesRegex(ValueError, "Invalid"):
            agent.add_decision("Use buffering", rationale="New evidence", supersedes=[decision["id"], "absent"])
        self.assertEqual(self.work.show(decision["id"])["status"], "active")
        self.assertEqual(self.work.show(decision["id"])["revision"], updated["revision"])
        first_goal = agent.record("goal", "First feature")["node"]
        second_goal = agent.record("goal", "Second feature")["node"]
        first_tests = agent.add_task("Add regression tests", parent=first_goal["id"])["node"]
        second_tests = agent.add_task("Add regression tests", parent=second_goal["id"])["node"]
        self.assertNotEqual(first_tests["id"], second_tests["id"])
        self.assertEqual(agent.add_task("Add regression tests", parent=first_goal["id"])["node"]["id"], first_tests["id"])

    def test_parallel_writers_reconcile_one_revision_without_lost_update(self) -> None:
        agent = self.agent("parallel")
        item = agent.add_task("Shared task")["node"]

        def change(number: int) -> str:
            """Race observed revisions through independent storage opens."""
            try:
                self.work.put("task", item["content"], agent_id=f"agent:{number}", session_id="parallel",
                              record_id=item["id"], expected_revision=1, status="in_progress",
                              rationale=f"Claim {number}")
                return "updated"
            except ValueError as exc:
                self.assertIn("revision changed", str(exc))
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(change, range(2)))
        self.assertCountEqual(results, ["updated", "conflict"])
        self.assertEqual(self.work.show(item["id"])["revision"], 2)

    def test_cycles_paths_and_lifecycle_reasons_are_validated(self) -> None:
        agent = self.agent("validation")
        first = agent.add_task("First")["node"]
        second = agent.add_task("Second", depends_on=[first["id"]])["node"]
        with self.assertRaisesRegex(ValueError, "Cycle"):
            self.update(agent, first, depends_on=[second["id"]])
        for path in ("../secret", "C:/private", "/private"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "repository-relative"):
                agent.record("change", "unsafe evidence", files=[path])
        with self.assertRaisesRegex(ValueError, "rationale"):
            self.update(agent, first, status="blocked")
        with self.assertRaisesRegex(ValueError, "waiting"):
            agent.complete_task(second["id"], "Premature completion")
        with self.assertRaisesRegex(ValueError, "reason must not be empty"):
            agent.reject_approach("Cache every query", " ")
        with self.assertRaisesRegex(ValueError, "content must not be empty"):
            agent.reject_approach(" ", "Stale results")
        self.assertEqual(self.work.show(first["id"])["revision"], 1)

    def test_history_and_terminal_retention_preserve_active_and_referenced_evidence(self) -> None:
        agent = self.agent("retention")
        item = agent.record("observation", "Current finding")["node"]
        for number in range(12):
            item = self.update(agent, item, rationale=f"Evidence {number}")
        self.assertEqual(len(item["history"]), MAX_REVISIONS)
        old = agent.add_task("Old outcome")["node"]
        agent.complete_task(old["id"], "Tested")
        active = agent.add_task("Follow-up", depends_on=[old["id"]])["node"]
        # A smaller limit exercises the actual maintenance policy without 200 writes.
        from unittest.mock import patch
        with patch("memory.services.coordination.MAX_TERMINAL_RECORDS", 2):
            for number in range(5):
                agent.record("change", f"Completed {number}", status="done")
            self.assertEqual(self.work.prune(), 3)
        ids = {record["id"] for record in self.work.records()}
        self.assertTrue({item["id"], old["id"], active["id"]}.issubset(ids))

    def test_legacy_private_work_migrates_without_publishing_scratch(self) -> None:
        agent = self.agent("legacy")
        graph = MemoryGraph.open(agent.paths.agent_storage)
        try:
            graph.add_node(MemoryNode(id="legacy-task", type="task", label="Legacy outcome", text="Legacy outcome",
                                     status="active", properties={"content": "Legacy outcome", "session_id": "legacy"}))
            graph.add_node(MemoryNode(id="legacy-failure", type="rejected_approach", text="Rejected cache",
                                     properties={"content": "Rejected cache", "metadata": {"reason": "Stale snapshot"}}))
        finally:
            graph.close()
        agent.add_note("Private scratch")
        agent.init("Resume migration")
        self.assertEqual(self.work.show("legacy-failure")["rationale"], "Stale snapshot")
        self.assertEqual(self.work.show("legacy-task")["origin_agent_id"], agent.agent_id)
        graph = MemoryGraph.open(agent.paths.agent_storage, read_only=True)
        try:
            self.assertIsNone(graph.get_node("legacy-task"))
        finally:
            graph.close()
        agent.finish("Legacy work retained")
        self.assertNotIn("Private scratch", json.dumps(self.work.records()))
        self.assertEqual(self.work.show("legacy-task")["content"], "Legacy outcome")

    def test_compiled_code_context_and_mcp_share_scoped_engineering_evidence(self) -> None:
        agent = self.agent("integration")
        decision = agent.add_decision("Preserve payload field", rationale="Client contract", files=["src/codec.py"])["node"]
        graph = MemoryGraph.open(self.storage)
        try:
            graph.add_node(MemoryNode(id="function:encode", type="Function", label="encode_payload", text="encode payload serializer",
                                     properties={"relative_path": "src/codec.py", "line_start": 1, "line_end": 5}))
            payload = graph.query_context_payload("encode payload", scopes=("code",))
            work = payload["payload"]["engineering_context"]
            self.assertIn(decision["id"], {item["id"] for item in work["records"]})
            self.assertIn("Client contract", graph.query_context("encode payload", scopes=("code",)))
        finally:
            graph.close()
        mcp = mcp_context(storage_path=str(self.storage), query="encode payload", code=True)
        self.assertEqual(mcp["payload"]["engineering_context"], work)

    def test_cli_handoff_works_across_processes_without_canonical_graph(self) -> None:
        cli = [sys.executable, str(Path("cli.py").resolve())]

        def run(*args: str) -> dict:
            """Use independent processes to prove persistence, not in-memory reuse."""
            result = subprocess.run([*cli, *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8", check=True)
            return json.loads(result.stdout)
        run("agent", "--agent", "agent:cli-A", "--no-progress", "init", "--name", "CLI handoff", "--json")
        task = run("agent", "--agent", "agent:cli-A", "--no-progress", "record", "task", "Complete codec", "--file", "src/codec.py", "--next", "Verify consumer", "--json")["node"]
        run("agent", "--agent", "agent:cli-A", "--no-progress", "finish", "Handoff pending", "--json")
        recovered = run("project", "context", "--file", "src/codec.py", "--json")
        self.assertIn(task["id"], {item["id"] for item in recovered["records"]})
        self.assertEqual(run("agent", "show", task["id"], "--json")["node"]["next_action"], "Verify consumer")
        self.assertFalse(self.storage.exists())

    def test_mcp_work_tools_support_sessions_reconciliation_and_read_only_discovery(self) -> None:
        from mcp.tools import call_tool, list_tools, MCPToolError
        common = {"storage_path": str(self.storage)}
        created = call_tool("reql_work_record", {**common, "kind": "task", "content": "MCP continuation",
                                                "agent_id": "agent:first", "session_id": "session:first"})["node"]
        updated = call_tool("reql_work_record", {**common, "kind": "task", "content": created["content"],
                                                "agent_id": "agent:next", "session_id": "session:next",
                                                "fields": {"record_id": created["id"], "expected_revision": 1,
                                                           "status": "in_progress", "rationale": "Continue the handoff"}})["node"]
        context = call_tool("reql_work_context", {**common, "record_id": created["id"], "include_history": True})
        self.assertEqual(context["records"][0]["history"][0]["agent_id"], "agent:first")
        self.assertEqual(updated["session_id"], "session:next")
        overview = call_tool("reql_work_overview", common)
        self.assertEqual(overview["sections"]["active"][0]["id"], created["id"])
        readable = {schema["name"] for schema in list_tools(include_write=False)}
        self.assertIn("reql_work_context", readable)
        self.assertIn("reql_work_overview", readable)
        self.assertNotIn("reql_work_record", readable)
        with self.assertRaisesRegex(MCPToolError, "revision changed"):
            call_tool("reql_work_record", {**common, "kind": "task", "content": created["content"],
                                         "agent_id": "agent:stale", "session_id": "stale",
                                         "fields": {"record_id": created["id"], "expected_revision": 1, "status": "done"}})
        self.assertFalse(self.storage.exists())

    def test_compatibility_views_and_reset_preserve_shared_owner(self) -> None:
        agent = self.agent("views")
        task = agent.add_task("Retained outcome")["node"]
        failure = agent.reject_approach("Rejected idea", "Breaks callers")["node"]
        completed = agent.add_task("Try direct parsing")["node"]
        agent.complete_task(completed["id"], "Parsing works")
        self.assertEqual(failure["reason"], "Breaks callers")
        self.assertEqual(failure["session_title"], "views")
        dashboard = agent.dashboard()["private"]
        self.assertEqual(dashboard["rejected"][0]["id"], failure["id"])
        self.assertEqual(dashboard["done"][0]["id"], completed["id"])
        self.assertEqual(dashboard["done"][0]["completion_message"], "Parsing works")
        self.assertEqual(agent.operational_overview()["rejected_approaches"][0]["reason"], failure["reason"])
        self.assertIn(task["id"], {item["id"] for item in agent.list_items(node_type="task")["nodes"]})
        self.assertIn(task["id"], {item["node"]["id"] for item in agent.search("Retained outcome")["results"]})
        self.assertIn(failure["id"], {item["id"] for item in agent.export(include_metadata=True)["nodes"]})
        # Noncontiguous content/reason words must match the shared search view.
        self.assertTrue(any(item.get("id") == failure["id"]
                            for item in AgentWorkspace.search_dashboards(self.storage, "Rejected callers")["results"]))
        agent.reset()
        self.assertEqual(self.work.show(task["id"])["status"], "open")
        self.assertEqual(self.work.show(failure["id"])["rationale"], "Breaks callers")
        agent.init("After reset")
        agent.finish("Shared work retained")
        self.assertFalse(agent.exists())
        self.assertTrue(any(item.get("id") == failure["id"]
                            for item in AgentWorkspace.search_dashboards(self.storage, "Rejected callers")["results"]))
        self.assertTrue(AgentWorkspace.search_dashboards(self.storage, "Parsing works")["results"])
        agent.init("Resumed views")
        self.assertEqual(agent.dashboard()["private"]["rejected"][0]["id"], failure["id"])
        self.assertEqual(agent.dashboard()["private"]["done"][0]["id"], completed["id"])
        agent.finish("Resumed work ended")

    def test_mid_session_checkpoint_does_not_close_work_or_replace_other_checkpoints(self) -> None:
        agent = self.agent("checkpoint")
        task = agent.add_task("Continue integration")["node"]
        manual = agent.record("checkpoint", "Decision milestone", status="done", key="milestone")["node"]
        self.assertEqual(agent.public_dashboard()["active_tasks"][0]["id"], task["id"])
        agent.finish("Integration handoff")
        self.assertEqual(self.work.show(manual["id"])["content"], "Decision milestone")
        self.assertEqual(len([item for item in self.work.records() if item["kind"] == "checkpoint"]), 2)
