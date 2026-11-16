"""Keep bounded work text informative while referring only to evidence already shown."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from memory.services import coordination
from memory.services.coordination import CoordinationStore, render_work_context


class CompactWorkRenderingTests(unittest.TestCase):
    """Exercise real work snapshots instead of replacing coordination logic in tests."""

    def setUp(self) -> None:
        scratch = Path.cwd() / ".tmp" / "regression-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.work = CoordinationStore(Path(self.temp.name) / "work.reql")

    def put(self, kind: str, content: str, **fields) -> dict:
        """Build authoritative records through the normal transactional owner."""
        return self.work.put(kind, content, agent_id="agent:planner", session_id=None, **fields)["node"]

    def test_repeated_full_fields_leave_room_for_late_evidence(self) -> None:
        """A repeated rationale must not consume the space needed for checks and next steps."""
        evidence = ("Verified wire semantics and consumer invariants. " * 70).rstrip()
        task = self.put("task", evidence, rationale=evidence, next_action="NEXT: verify rollback",
                        files=["src/wire.py", "tests/test_wire.py"])
        payload = self.work.context(record_id=task["id"])
        original = deepcopy(payload)
        text = render_work_context(payload, record_id=task["id"])
        self.assertEqual(text.count(evidence), 1)
        self.assertIn("Why: same as content above", text)
        self.assertIn("NEXT: verify rollback", text)
        self.assertIn("tests/test_wire.py", text)
        self.assertNotIn("truncated", text)
        self.assertEqual(payload, original)

    def test_identical_fields_across_records_retain_distinct_provenance_and_evidence(self) -> None:
        """Only repeated prose can be shared: revisions, status, files and owners stay visible."""
        evidence = ("Protocol evidence. " * 16).rstrip()
        task = self.put("task", "Implement transport", rationale=evidence, files=["src/wire.py"])
        checkpoint = self.put("checkpoint", evidence, status="done", summarizes=[task["id"]], files=["tests/test_wire.py"])
        payload = self.work.context(record_id=task["id"])
        text = render_work_context(payload, record_id=task["id"])
        self.assertEqual(text.count(evidence), 1)
        self.assertIn(checkpoint["id"], text)
        self.assertIn("same as item 1 rationale above", text)
        self.assertIn(task["id"] + " r1", text)
        self.assertIn("[task/ready]", text)
        self.assertIn("[checkpoint/done]", text)
        self.assertIn("src/wire.py", text)
        self.assertIn("tests/test_wire.py", text)
        self.assertEqual(render_work_context(payload, record_id=task["id"]), text)
        compact = render_work_context(payload)
        self.assertIn(evidence, compact)
        self.assertIn(checkpoint["id"], compact)

    def test_equal_prefixes_do_not_hide_distinct_contract_tails(self) -> None:
        """Similar text is not duplicate evidence, including differences after compact previews."""
        shared = "Compatibility detail. " * 24
        decision = self.put("decision", shared + "KEEP_NULL", rationale=shared + "KEEP_EMPTY")
        task = self.put("task", "Verify transport", depends_on=[decision["id"]])
        text = render_work_context(self.work.context(record_id=task["id"]), record_id=task["id"])
        self.assertIn(shared + "KEEP_NULL", text)
        self.assertIn(shared + "KEEP_EMPTY", text)

    def test_repeated_links_keep_every_role_without_repeating_targets(self) -> None:
        """Readiness and parent/dependency roles remain recoverable from compact text."""
        prerequisite = self.put("task", "Publish wire contract")
        task = self.put("task", "Implement consumer", depends_on=[prerequisite["id"]], parent=prerequisite["id"])
        text = render_work_context(self.work.context(record_id=task["id"], limit=1), record_id=task["id"])
        self.assertEqual(text.count(prerequisite["id"]), 1)
        self.assertIn("waiting+parent+depends:", text)
        self.assertIn("task/blocked", text)

    def test_overview_keeps_section_membership_without_repeating_record_details(self) -> None:
        """The active and ready sections point to the same displayed snapshot."""
        task = self.put("task", "Preserve invoice rounding", rationale="Consumer contract", files=["src/billing.py"])
        payload = self.work.overview()
        original = deepcopy(payload)
        text = coordination.render_work_overview(payload)
        self.assertEqual(text.count("Preserve invoice rounding"), 1)
        self.assertIn("### Active (1)", text)
        self.assertIn("### Next (1)", text)
        self.assertIn(task["id"] + " r1; see Active above", text)
        self.assertEqual(payload, original)
        self.assertEqual(self.work.overview(), original)

    def test_overview_does_not_reference_a_budget_cut_record(self) -> None:
        """A partially emitted row cannot stand in for details in another section."""
        self.put("task", "A long contract. " * 80, rationale="Late evidence. " * 50)
        text = coordination.render_work_overview(self.work.overview(), max_chars_per_section=350)
        self.assertNotIn("see Active above", text)
        self.assertGreaterEqual(text.count("truncated"), 2)

    def test_overview_keeps_changed_execution_evidence_at_the_same_revision(self) -> None:
        """Dependency state can change independently of a task's record revision."""
        self.put("task", "Verify contract", next_action="Check all consumers")
        payload = self.work.overview()
        changed = deepcopy(payload["sections"]["active"][0])
        changed.update(execution_state="needs_review", obsolete_dependencies=["work:changed-assumption"])
        payload["sections"]["blockers"] = [changed]
        payload["counts"]["blockers"] = 1
        text = coordination.render_work_overview(payload)
        blockers = text.split("### Blockers", 1)[1].split("###", 1)[0]
        self.assertIn("task/needs_review", blockers)
        self.assertIn("work:changed-assumption", blockers)
        self.assertNotIn("see Active above", blockers)

    def test_overview_counts_records_excluded_before_rendering(self) -> None:
        """The omission count includes both store selection and the text budget."""
        for index in range(7):
            self.put("task", f"Consumer validation {index}")
        text = coordination.render_work_overview(self.work.overview(limit=1))
        active = text.split("### Active (7)", 1)[1].split("###", 1)[0]
        self.assertIn("6 matching record(s) omitted", active)

    def test_truncated_earlier_fields_and_small_budgets_remain_explicit(self) -> None:
        """Sharing an equal preview must preserve its warning and a path to complete evidence."""
        evidence = ("Contract detail. " * 70).rstrip()
        task = self.put("task", evidence, rationale=evidence)
        payload = self.work.context(record_id=task["id"])
        compact = render_work_context(payload)
        self.assertIn("Why: same as content above [truncated]", compact)
        self.assertIn("agent show " + task["id"] + " --json", compact)
        for budget in (1, 32, 100, 350, 700, 2400, 6000):
            with self.subTest(budget=budget):
                self.assertLessEqual(len(render_work_context(payload, max_chars=budget)), budget)


if __name__ == "__main__":
    unittest.main()
