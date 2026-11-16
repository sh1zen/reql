"""Validate useful, bounded handoffs from persisted shared work."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.tools import reql_work_context
from memory.cli import main
from memory.services.coordination import CoordinationStore, render_work_context
from memory.services import coordination


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

    def test_known_obsolete_id_returns_its_current_replacement(self) -> None:
        old, middle, current, task = self.replacements()
        payload = self.work.context(record_id=old["id"])
        ids = {row["id"] for row in payload["records"]}
        self.assertIn(current["id"], ids)
        self.assertIn(task["id"], ids)
        self.assertNotIn(old["id"], ids)
        self.assertNotIn(middle["id"], ids)
        historical = self.work.context(record_id=old["id"], include_history=True)
        self.assertIn(old["id"], {row["id"] for row in historical["records"]})

    def test_explicit_task_keeps_causal_evidence_in_a_large_noisy_backlog(self) -> None:
        _, _, current, task = self.replacements()
        for index in range(85):
            self.put("change", f"Unrelated shared module outcome {index}", status="done", files=["src/shared.py"])
        payload = self.work.context("shared module", record_id=task["id"], files=["src/shared.py"], limit=4)
        ids = {row["id"] for row in payload["records"]}
        self.assertIn(task["id"], ids)
        self.assertIn(current["id"], ids)
        self.assertFalse(any("Unrelated" in row["content"] for row in payload["records"]))

    def test_task_scope_does_not_tokenize_unrelated_record_prose(self) -> None:
        """Empty and supplied queries skip unrelated prose for a known task."""
        task = self.put("task", "Verify transport", files=["src/transport.py"])
        for index in range(25):
            self.put("change", f"Other lane {index}", status="done", rationale="Long historical result. " * 70)
        for query in ("", "transport"):
            with self.subTest(query=query):
                with patch.object(coordination, "_tokens", wraps=coordination._tokens) as tokenize:
                    payload = self.work.context(query, record_id=task["id"])
                self.assertEqual([row["id"] for row in payload["records"]], [task["id"]])
                self.assertEqual(tokenize.call_count, 1)
                self.assertEqual(tokenize.call_args.args, (query,))

    def test_current_file_contract_precedes_verbose_historical_file_matches(self) -> None:
        """Completed summaries must not displace a current constraint for the same file."""
        constraint = self.put("constraint", "Preserve empty fields", files=["src/transport.py"])
        for index in range(12):
            self.put("checkpoint", f"Transport caching performance outcome {index}",
                     rationale="Transport caching performance verified", status="done", files=["src/transport.py"])
        payload = self.work.context("transport caching performance", files=["src/transport.py"], limit=1)
        self.assertEqual(payload["records"][0]["id"], constraint["id"])
        self.assertGreaterEqual(payload["omitted"], 12)

    def test_dependency_conflict_precedes_consumers_in_large_task_fanout(self) -> None:
        """The short handoff must explain reconciliation before listing affected consumers."""
        contract = self.put("decision", "Preserve wire fields", rationale="Compatibility")
        conflict = self.put("decision", "Remove wire fields", rationale="New assumption", contradicts=[contract["id"]])
        task = self.put("task", "Update transport", depends_on=[contract["id"]])
        for index in range(85):
            self.put("task", f"Consumer lane {index}", depends_on=[task["id"]])
        payload = self.work.context(record_id=task["id"], limit=3)
        rows = {row["id"]: row for row in payload["records"]}
        self.assertEqual(set(rows), {task["id"], contract["id"], conflict["id"]})
        self.assertEqual(rows[task["id"]]["execution_state"], "needs_review")
        self.assertTrue(payload["expansion_truncated"])
        text = render_work_context(payload, record_id=task["id"])
        self.assertIn("Causal expansion limited", text)

    def test_matching_backlog_does_not_disable_causal_expansion(self) -> None:
        """More than eighty lexical anchors must still recover a linked contract."""
        constraint = self.put("constraint", "Keep empty fields")
        self.put("task", "Transport caching implementation", files=["src/transport.py"], depends_on=[constraint["id"]])
        for index in range(85):
            self.put("change", f"Transport caching outcome {index}", status="done", files=["src/transport.py"])
        payload = self.work.context("transport caching", files=["src/transport.py"], limit=3)
        self.assertIn(constraint["id"], {row["id"] for row in payload["records"]})
        self.assertGreaterEqual(payload["total_matches"], 87)

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

    def test_compact_text_marks_field_truncation_and_matching_omissions(self) -> None:
        self.put("task", "Details " * 85, rationale="Evidence " * 70)
        self.put("task", "Another matching task")
        payload = self.work.context(limit=1)
        text = render_work_context(payload)
        self.assertIn("truncated", text)
        self.assertIn("agent show", text)
        self.assertIn("1 matching record", text)

    def test_huge_focus_cannot_hide_that_the_record_is_incomplete(self) -> None:
        task = self.put("task", "Transport " * 100, rationale="Evidence " * 200,
                        next_action="Verify consumer rollback")
        text = render_work_context(self.work.context(record_id=task["id"]), record_id=task["id"], max_chars=700)
        self.assertIn(task["id"], text)
        self.assertIn("truncated", text)
        self.assertIn("agent show " + task["id"] + " --json", text)
        self.assertLessEqual(len(text), 700)

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

    def test_cli_context_and_single_record_show_keep_current_evidence(self) -> None:
        rationale = "Detailed evidence. " * 30 + "LATE_RESULT: all transport checks passed"
        task = self.put("task", "Verify transport", rationale=rationale)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            for args in (["project", "context", "--task", task["id"]], ["agent", "show", task["id"]]):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = main(args)
                self.assertEqual(status, 0)
                self.assertIn("LATE_RESULT", output.getvalue())
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
