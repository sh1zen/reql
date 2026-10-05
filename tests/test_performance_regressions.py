"""Semantic and resource-isolation regressions for the optimized hot paths."""
from __future__ import annotations

from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from memory.domain.models import MemoryNode
from memory.explanation.service import _workflow_documentation_evidence
from memory.extraction.normalization import canonicalize, identifier_expanded_text, tokenize
from memory.services.coordination import CoordinationStore, STATUSES, TERMINAL
from memory.storage import BlockGraphStore


def reference_execution(item: dict, records: dict) -> dict:
    """Independent scan-based oracle for existing conflict/readiness semantics."""
    waiting, obsolete = [], []
    for target in item["depends_on"]:
        dependency = records.get(target)
        if dependency is None or dependency["status"] in {"superseded", "invalidated", "abandoned"}:
            obsolete.append(target)
        elif dependency["kind"] in {"task", "goal", "question"} and dependency["status"] not in {"done", "resolved"}:
            waiting.append(target)
        if dependency is not None and dependency["status"] not in TERMINAL:
            if any(other["status"] not in TERMINAL and
                   (other_id in dependency["contradicts"] or target in other["contradicts"])
                   for other_id, other in records.items()):
                obsolete.append(target)
    conflicts = [target for target in item["contradicts"]
                 if target in records and records[target]["status"] not in TERMINAL]
    conflicts.extend(other_id for other_id, other in records.items()
                     if item["id"] in other["contradicts"] and other["status"] not in TERMINAL)
    state = item["status"]
    if state not in TERMINAL:
        state = "needs_review" if obsolete or conflicts else (
            "blocked" if waiting or state == "blocked" else ("ready" if state == "open" else state))
    return {"execution_state": state, "waiting_on": waiting,
            "obsolete_dependencies": obsolete, "conflicts": sorted(set(conflicts))}


class PerformanceRegressionTests(unittest.TestCase):
    """Verify results without brittle wall-clock assertions or live providers."""

    def test_conflict_index_matches_scan_for_all_states_and_directionality(self) -> None:
        rng = random.Random(76291)
        statuses = sorted(STATUSES)
        ids = [f"work:{index}" for index in range(180)]
        records = {item_id: {"id": item_id, "kind": rng.choice(["goal", "task", "question", "decision"]),
                            "status": statuses[index % len(statuses)],
                            "depends_on": rng.sample(ids + ["missing"], rng.randrange(5)),
                            "contradicts": rng.sample(ids + ["missing"], rng.randrange(5))}
                   for index, item_id in enumerate(ids)}
        index = CoordinationStore._conflict_index(records)
        for item in records.values():
            self.assertEqual(reference_execution(item, records), CoordinationStore._execution(item, records, index))

    def test_work_reads_are_isolated_and_show_uses_identity_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = CoordinationStore(Path(directory) / "work.reql")
            item = work.put("task", "Preserve billing contracts", agent_id="agent:one", session_id="session:one",
                            files=["src/billing.py"])["node"]
            work.put("task", item["content"], record_id=item["id"], expected_revision=1,
                     agent_id="agent:two", session_id="session:two", next_action="Verify callers")
            records = work.records()
            records[0]["files"].append("mutated.py")
            records[0]["history"][0]["files"].clear()
            with patch.object(BlockGraphStore, "find_nodes_by_types", side_effect=AssertionError("full history read")):
                shown = work.show(item["id"])
            self.assertEqual(shown["files"], ["src/billing.py"])
            self.assertEqual(shown["history"][0]["files"], ["src/billing.py"])
            self.assertEqual(shown["revision"], 2)
            with self.assertRaisesRegex(ValueError, "Work record not found"):
                work.show("missing")

    def test_ordinary_write_checks_reachable_dependencies_without_full_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = CoordinationStore(Path(directory) / "work.reql")
            goal = work.put("goal", "Preserve payment contracts", agent_id="agent:one", session_id=None)["node"]
            with patch.object(BlockGraphStore, "find_nodes_by_types", side_effect=AssertionError("full history read")):
                task = work.put("task", "Verify consumers", parent=goal["id"], depends_on=[goal["id"]],
                                agent_id="agent:one", session_id=None)["node"]
                with self.assertRaisesRegex(ValueError, "Cycle in depends_on"):
                    work.put("goal", goal["content"], record_id=goal["id"], expected_revision=1,
                             depends_on=[task["id"]], agent_id="agent:two", session_id=None)
            self.assertEqual(work.show(goal["id"])["revision"], 1)
            self.assertEqual(work.context(record_id=task["id"])["records"][0]["waiting_on"], [goal["id"]])

    def test_deferred_work_writes_rebuild_identical_lexical_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = CoordinationStore(Path(directory) / "work.reql")
            original = work.put("decision", "Preserve invoice normalization", rationale="Existing consumers",
                                agent_id="agent:one", session_id="session:one")["node"]
            work.put("decision", "Use invoice tokenization", rationale="Existing consumers",
                     agent_id="agent:two", session_id="session:two", supersedes=[original["id"]])
            records = work.records()
            eager = BlockGraphStore(Path(directory) / "eager.reql")
            deferred = BlockGraphStore(work.path)
            try:
                eager.batch_upsert_nodes(deferred.find_nodes_by_types(["work_record"]))
                for query in ("invoice", "normalization", "tokenization", "decision"):
                    self.assertEqual([(node.id, score) for node, score in eager.lexical_search(query)],
                                     [(node.id, score) for node, score in deferred.lexical_search(query)])
                self.assertEqual(next(item for item in records if item["id"] == original["id"])["status"], "superseded")
                self.assertEqual(len(records), 2)
                # Checkpoint finalization, reopen and another revision must retain
                # both posting directions and remove the replaced text's terms.
                deferred.checkpoint_if_needed(wal_bytes_threshold=0)
            finally:
                eager.close()
                deferred.close()
            item = work.show(original["id"])
            work.put("decision", "Preserve billing serialization", record_id=item["id"],
                     expected_revision=item["revision"], agent_id="agent:three", session_id="session:three")
            reopened = BlockGraphStore(work.path)
            try:
                self.assertNotIn(item["id"], [node.id for node, _ in reopened.lexical_search("normalization")])
                self.assertIn(item["id"], [node.id for node, _ in reopened.lexical_search("serialization")])
            finally:
                reopened.close()

    def test_document_tokens_are_request_local_and_evidence_order_is_preserved(self) -> None:
        docs = [MemoryNode(id="doc:two", type="SourceFragment", label="Invoice payment", text="Payment validation",
                           properties={"relative_path": "docs/b.md"}),
                MemoryNode(id="doc:one", type="SourceFragment", label="Invoice payment", text="Payment validation",
                           properties={"relative_path": "docs/a.md"})]
        cache: dict[str, set[str]] = {}
        evidence = _workflow_documentation_evidence(docs, {"payment", "validation"}, tokens_by_id=cache)
        self.assertEqual([item.node_id for item in evidence], ["doc:one", "doc:two"])
        self.assertEqual(_workflow_documentation_evidence(docs, {"payment", "validation"}, tokens_by_id=cache), evidence)
        docs[0].text = "Serialization only"
        docs[0].label = "Serialization"
        refreshed = _workflow_documentation_evidence(docs, {"payment", "validation"}, tokens_by_id={})
        self.assertEqual([item.node_id for item in refreshed], ["doc:one"])

    def test_normalization_preserves_unicode_and_returns_independent_tokens(self) -> None:
        self.assertEqual(canonicalize("  Città\n‘Invoice’ — PayMéNT_42  "), "citta invoice payment_42")
        self.assertEqual(identifier_expanded_text("src/HTTPServer.invoice_total"), "src HTTP Server invoice total")
        tokens = tokenize("Invoice Payment")
        tokens.clear()
        self.assertEqual(tokenize("Invoice Payment"), ["invoice", "payment"])


if __name__ == "__main__":
    unittest.main()
