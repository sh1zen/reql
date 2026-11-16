"""Semantic and resource-isolation regressions for the optimized hot paths."""
from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
import io
import json
import os
import random
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from mcp.tools import reql_project_status
from memory.cli import main as cli_main
from memory.domain.models import MemoryEdge, MemoryNode
from memory.explanation.service import _capability_key, _is_test_path, _workflow_documentation_evidence
from memory.services.coordination import CoordinationStore, STATUSES, TERMINAL
from memory.storage import BlockGraphStore
from memory.services.query_context import _canonical_json_value
from memory.services.retrieval.common import _canonical_token_overlap
from memory.extraction.normalization import singular_source_variants


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

    def test_degree_ranking_copies_selected_nodes_and_preserves_isolation(self) -> None:
        scratch = Path(".tmp")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            store = BlockGraphStore(Path(directory) / "degree.reql")
            try:
                for index in range(12):
                    store.upsert_node(MemoryNode(id=f"node:{index}", type="Function", salience=index / 12,
                                                 properties={"nested": {"names": [str(index)]}}))
                for index in range(1, 12):
                    store.upsert_edge(MemoryEdge(id=f"edge:{index}", from_id="node:0", to_id=f"node:{index}",
                                                 type="CALLS", weight=index / 12, confidence=0.5))
                store.upsert_edge(MemoryEdge(id="self", from_id="node:0", to_id="node:0", type="OTHER"))
                reference = store.top_nodes_by_degree(limit=None, ignored_edge_types={"OTHER"})
                expected = [(node.id, degree, weight) for node, degree, weight in reference[:2]]
                with patch.object(store, "_clone_node", wraps=store._clone_node) as copy:
                    selected = store.top_nodes_by_degree(limit=2, ignored_edge_types={"OTHER"})
                    self.assertEqual(copy.call_count, 2)
                self.assertEqual([(node.id, degree, weight) for node, degree, weight in selected], expected)
                self.assertEqual(selected[0][1], 11)
                self.assertEqual(store.top_nodes_by_degree(limit=0), [])
                selected[0][0].properties["nested"]["names"].append("changed")
                self.assertEqual(store.get_node("node:0").properties["nested"]["names"], ["0"])
                with patch.object(store, "_clone_node", side_effect=AssertionError("borrowed ranking copied nodes")):
                    borrowed = store.top_nodes_by_degree(limit=None, clone=False)
                self.assertIs(borrowed[0][0], store.get_node("node:0", clone=False))
                self.assertEqual(borrowed[0][1], 12)
            finally:
                store.close()

    def test_metadata_cli_and_mcp_skip_unused_lexical_loading_but_search_loads_it(self) -> None:
        scratch = Path(".tmp")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory).resolve()
            (root / "invoice.py").write_text("def invoice_total(amount):\n    return amount\n", encoding="utf-8")
            storage = root / ".reql" / "memory.reql"
            graph = MemoryGraph.open(storage)
            try:
                graph.compile_project(root)
                expected = graph.project_status(root)
                graph.store.checkpoint_if_needed(wal_bytes_threshold=0)
            finally:
                graph.close()
            original_bytes = storage.read_bytes()
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(BlockGraphStore, "_ensure_lexical_index_loaded",
                                  side_effect=AssertionError("metadata loaded the lexical index")):
                    for command in (["stats", "--json"], ["project", "status", "--json"], ["locate", "invoice.py", "--json"]):
                        with redirect_stdout(io.StringIO()) as out:
                            self.assertEqual(cli_main(command), 0)
                        result = json.loads(out.getvalue())
                        if command[:2] == ["project", "status"]:
                            self.assertEqual(result, expected)
                    self.assertEqual(reql_project_status(storage_path=str(storage), path=str(root)), {**expected, "found": True})
                loads = []
                ensure_loaded = BlockGraphStore._ensure_lexical_index_loaded

                def load(store):
                    """Count actual lazy loads while running the normal search implementation."""
                    loads.append(store.path)
                    return ensure_loaded(store)

                with patch.object(BlockGraphStore, "_ensure_lexical_index_loaded", new=load):
                    with redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(cli_main(["query_context", "--query", "invoice_total", "--code", "--json"]), 0)
                self.assertTrue(loads)
                self.assertEqual(json.loads(out.getvalue())["confidence"]["status"], "sufficient")
                self.assertEqual(storage.read_bytes(), original_bytes)
            finally:
                os.chdir(old_cwd)

    def test_overlap_keeps_plural_and_boundary_semantics_with_repeated_tokens(self) -> None:
        rng = random.Random(92512)
        words = ["policy", "policies", "class", "classes", "__edge__", "_tail-", "x", "retrieval", "stores", "store", "--", "42"]
        for _ in range(1000):
            value = " \t\n".join(rng.choices(words, k=rng.randrange(150)))
            query = set(rng.choices(words, k=5))
            source = {token for raw in value.split() if len(token := raw.strip("_-")) >= 2}
            expected = {token for token in query if token in source or any(variant in source for variant in singular_source_variants(token))}
            self.assertEqual(_canonical_token_overlap(value, query), expected)

    def test_revision_canonicalization_preserves_encoded_bytes_and_stringified_key_collisions(self) -> None:
        def reference(value):
            """Retain the original canonical-encoding algorithm as an independent oracle."""
            if isinstance(value, dict):
                return {str(key): reference(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
            if isinstance(value, (list, tuple)):
                return [reference(item) for item in value]
            if isinstance(value, (set, frozenset)):
                return sorted([reference(item) for item in value], key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True, default=str))
            return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)

        values = [{3: "number", "3": "string", None: [True, 2.5]},
                  {"z": (1, {"é", "beta"}), "a": {"path": Path("src/a.py"), "frozen": frozenset({2, 1})}},
                  {"1": "first", 1: "last", "bool": {True: False}}]
        for value in values:
            encode = lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            self.assertEqual(encode(_canonical_json_value(value)), encode(reference(value)))

    def test_cached_path_classification_includes_dominant_root_and_returns_immutable_values(self) -> None:
        self.assertEqual(_capability_key("src/platform/billing/service.py", "platform"), "billing")
        self.assertEqual(_capability_key("src/platform/billing/service.py", None), "platform")
        self.assertTrue(_is_test_path("src/billing/tests/test_invoice.py"))
        self.assertTrue(_is_test_path("web/invoice.spec.ts"))
        self.assertFalse(_is_test_path("src/billing/invoice.py"))


if __name__ == "__main__":
    unittest.main()
