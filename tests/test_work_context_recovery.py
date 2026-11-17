"""Validate useful, bounded handoffs from persisted shared work."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from mcp.tools import reql_work_context
from memory.services.coordination import CoordinationStore, render_work_context


class WorkContextRecoveryTests(unittest.TestCase):
    """Recover current assumptions and complete task evidence without history dumps."""

    def setUp(self) -> None:
        scratch = Path.cwd() / ".tmp" / "regression-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = CoordinationStore(self.root / ".reql" / "agent-dashboard.reql")

    def put(self, kind: str, content: str, **fields) -> dict:
        """Write through the normal revision-checked store boundary."""
        return self.work.put(kind, content, agent_id="agent:planner", session_id="session:plan", **fields)["node"]

    def replacements(self) -> tuple[dict, dict, dict, dict]:
        """Build a changed contract whose replacements have no lexical/file anchor."""
        old = self.put("decision", "Store complete requests", rationale="Initial contract")
        task = self.put("task", "Implement transport", depends_on=[old["id"]])
        middle = self.put("decision", "Store request references", rationale="Changed retention policy", supersedes=[old["id"]])
        current = self.put("decision", "Store signed reference hashes", rationale="Revised privacy contract", supersedes=[middle["id"]])
        return old, middle, current, task

    def test_task_recovers_current_replacement_chain_without_rewriting_dependencies(self) -> None:
        old, middle, current, task = self.replacements()
        before = self.work.show(task["id"])
        payload = self.work.context(record_id=task["id"])
        records = {row["id"]: row for row in payload["records"]}
        self.assertIn(current["id"], records)
        self.assertNotIn(old["id"], records)
        self.assertNotIn(middle["id"], records)
        self.assertIn("replacement:" + old["id"], records[current["id"]]["reasons"])
        self.assertEqual(records[task["id"]]["execution_state"], "needs_review")
        self.assertEqual(records[task["id"]]["obsolete_dependencies"], [old["id"]])
        self.assertEqual(before, self.work.show(task["id"]))
        with self.assertRaisesRegex(ValueError, "obsolete"):
            self.work.put(record_id=task["id"], expected_revision=task["revision"], status="done",
                          agent_id="agent:worker", session_id="session:worker", rationale="Do not accept obsolete assumptions")

    def test_explicit_task_keeps_causal_evidence_in_a_large_noisy_backlog(self) -> None:
        _, _, current, task = self.replacements()
        for index in range(85):
            self.put("change", f"Unrelated shared module outcome {index}", status="done", files=["src/shared.py"])
        payload = self.work.context("shared module", record_id=task["id"], files=["src/shared.py"], limit=4)
        ids = {row["id"] for row in payload["records"]}
        self.assertIn(task["id"], ids)
        self.assertIn(current["id"], ids)
        self.assertFalse(any("Unrelated" in row["content"] for row in payload["records"]))

    def test_focused_text_keeps_late_contract_and_test_evidence(self) -> None:
        rationale = "Contract explanation. " * 24 + "VERIFIED: python -m unittest tests.test_transport; 23 passed."
        contract_rationale = "Compatibility details. " * 25 + "CONTRACT_TAIL: exclusion wins before limit"
        contract = self.put("decision", "Keep filter semantics", rationale=contract_rationale)
        task = self.put("task", "Implement signed request transport", rationale=rationale, depends_on=[contract["id"]],
                        next_action="Continue from accepted transport fixtures", files=[f"src/part_{n}.py" for n in range(7)])
        payload = self.work.context(record_id=task["id"])
        before = deepcopy(payload)
        text = render_work_context(payload, record_id=task["id"])
        self.assertIn(rationale, text)
        self.assertIn("src/part_6.py", text)
        self.assertIn(contract_rationale, text)
        mcp = reql_work_context(storage_path=str(self.root / ".reql" / "memory.reql"), record_id=task["id"])
        self.assertEqual(mcp, payload)
        self.assertEqual(payload, before)
        self.assertLessEqual(len(text), 6000)

    def test_small_budgets_never_present_partial_coordination_links_as_complete(self) -> None:
        dependencies = [self.put("task", f"Prerequisite {index}")["id"] for index in range(32)]
        task = self.put("task", "Coordinate all prerequisites", depends_on=dependencies)
        payload = self.work.context(record_id=task["id"], limit=1)
        text = render_work_context(payload, record_id=task["id"], max_chars=700)
        self.assertIn("task/blocked", text)
        self.assertIn("coordination links omitted", text)
        self.assertIn("agent show " + task["id"] + " --json", text)
        for budget in (1, 32, 100, 700, 1200, 6000):
            with self.subTest(budget=budget):
                self.assertLessEqual(len(render_work_context(payload, record_id=task["id"], max_chars=budget)), budget)
        for budget in (0, -1, True):
            with self.subTest(invalid=budget), self.assertRaises(ValueError):
                render_work_context(payload, max_chars=budget)

if __name__ == "__main__":
    unittest.main()
