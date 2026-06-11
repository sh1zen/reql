from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.domain.models import MemoryEdge, MemoryNode


class EndToEndFlowTests(unittest.TestCase):
    def test_code_project_organization_free_query_and_context_query(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "orders.py").write_text(
                "\n".join(
                    [
                        "from decimal import Decimal",
                        "",
                        "class OrderRepository:",
                        "    def load_order(self, order_id):",
                        "        return {'id': order_id, 'total': Decimal('12.50')}",
                        "",
                        "class PaymentService:",
                        "    def __init__(self, repository):",
                        "        self.repository = repository",
                        "",
                        "    def charge_order(self, order_id):",
                        "        order = self.repository.load_order(order_id)",
                        "        return capture_payment(order)",
                        "",
                        "def capture_payment(order):",
                        "    return {'captured': True, 'amount': order['total']}",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "\n".join(
                    [
                        "# Payment Workflow",
                        "",
                        "The PaymentService coordinates OrderRepository and capture_payment.",
                        "",
                        "## RefundPolicy",
                        "",
                        "RefundPolicy documentation explains how payment context is reviewed.",
                    ]
                ),
                encoding="utf-8",
            )

            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertEqual(result.run.status, "completed")

                node_types = {node.type for node in graph.store.all_nodes()}
                edge_types = {edge.type for edge in graph.store.all_edges()}
                self.assertIn("Class", node_types)
                self.assertIn("Method", node_types)
                self.assertIn("Function", node_types)
                self.assertIn("Dependency", node_types)
                self.assertIn("Concept", node_types)
                self.assertIn("METHOD", edge_types)
                self.assertIn("CALLS", edge_types)
                self.assertIn("IMPORTS_FROM", edge_types)
                self.assertIn("REFERENCES", edge_types)

                free_query = graph.query(
                    "MATCH (c:Class)-[:METHOD]->(m:Method) RETURN c.name,m.name ORDER BY c.name ASC",

                )
                class_methods = {(row["c.name"], row["m.name"]) for row in free_query.rows}
                self.assertIn(("OrderRepository", "load_order"), class_methods)
                self.assertIn(("PaymentService", "charge_order"), class_methods)

                call_query = graph.query(
                    "MATCH (m:Method)-[:CALLS]->(target) RETURN m.name,target.name",

                )
                self.assertTrue(any(row["m.name"] == "charge_order" and row["target.name"] == "capture_payment" for row in call_query.rows))

                query_graph = graph.query_graph(
                    "RefundPolicy payment context",

                    top_k=8,
                    max_depth=2,
                    max_nodes=30,
                    max_edges=60,
                )
                self.assertEqual(query_graph["query"], "RefundPolicy payment context")
                self.assertGreater(query_graph["counts"]["ranked_nodes"], 0)
                self.assertIn("REQL Query Graph", query_graph["context"])
                self.assertTrue(
                    any(
                        "RefundPolicy" in str(node)
                        for node in query_graph["ranked_nodes"] + query_graph["nodes"] + query_graph["sources"]
                    )
                )

                context = graph.query_context("PaymentService capture_payment OrderRepository", top_k=8, max_depth=2)
                self.assertIsInstance(context, str)
                self.assertTrue("PaymentService" in context or "capture_payment" in context)
                self.assertTrue("OrderRepository" in context or "orders.py" in context)
            finally:
                graph.close()

    def test_project_to_report_flow_is_incremental_and_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            reports = Path(td) / "reports"
            root.mkdir()
            self._write_project(root)
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                first = graph.compile_project(root, max_file_size_bytes=1024 * 1024)
                self.assertEqual(first.run.status, "completed")
                self.assertEqual(first.run.files_changed, len(first.scan.artifacts))
                self.assertEqual(first.scan.registration.artifacts_archived if first.scan.registration else 0, 0)

                counts_after_first = self._counts(graph)
                self.assertEqual(counts_after_first["Project"], 1)
                self.assertGreaterEqual(counts_after_first["SourceArtifact"], 4)
                self.assertGreaterEqual(counts_after_first["SourceFragment"], 4)
                self.assertGreaterEqual(counts_after_first["Function"], 1)
                self.assertGreaterEqual(counts_after_first["Class"], 1)
                self.assertGreaterEqual(counts_after_first["ArtifactCacheEntry"], len(first.scan.artifacts))
                self.assertEqual(len([item for item in first.scan.skipped_files if item.reason == "max_file_size_exceeded"]), 1)

                second = graph.compile_project(root, max_file_size_bytes=1024 * 1024)
                counts_after_second = self._counts(graph)
                self.assertEqual(second.run.files_changed, 0)
                self.assertEqual(second.run.files_skipped, len(second.scan.artifacts))
                self.assertEqual(second.run.nodes_created, 0)
                self.assertEqual(counts_after_second["SourceArtifact"], counts_after_first["SourceArtifact"])
                self.assertEqual(counts_after_second["SourceFragment"], counts_after_first["SourceFragment"])
                self.assertEqual(counts_after_second["Function"], counts_after_first["Function"])
                self.assertEqual(counts_after_second["Class"], counts_after_first["Class"])

                (root / "app.py").write_text(
                    "\n".join(
                        [
                            '"""Demo module."""',
                            "import json",
                            "",
                            "class Compiler:",
                            "    def run(self):",
                            "        return helper()",
                            "",
                            "def helper():",
                            "    return json.dumps({'status': 'changed'})",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                third = graph.compile_project(root, max_file_size_bytes=1024 * 1024)
                changed_paths = {artifact.relative_path for artifact in third.scan.artifacts if artifact.id in third.dirty_set.changed_artifact_ids}
                self.assertEqual(changed_paths, {"app.py"})
                self.assertEqual(third.run.files_changed, 1)
                self.assertTrue(third.delta.affected_node_ids)

                deltas = graph.list_deltas(limit=10)
                self.assertGreaterEqual(len(deltas), 3)
                self.assertIsNotNone(graph.show_delta(third.delta.id))

                match = graph.query(
                    "MATCH (a:SourceArtifact)-[:DEFINES]->(f:Function) "
                    "RETURN a.path,f.name,f.start_line ORDER BY a.path ASC",

                )
                self.assertIn("helper", {row["f.name"] for row in match.rows})

                retrieval = graph.query_memories_payload("compile helper json", limit=6)
                ranked_nodes = retrieval["ranked_nodes"]
                self.assertTrue(ranked_nodes)
                self.assertTrue(
                    any("helper" in ((node.get("label") or "") + " " + (node.get("text") or "")) for node in ranked_nodes)
                )

                self._add_cross_community_evidence(graph)
                communities = graph.detect_communities(limit=20)
                hubs = graph.analyze_hubs(limit=20)
                self.assertGreaterEqual(len(communities.community_nodes), 1)
                self.assertTrue(hubs.hubs)
                self.assertTrue(all(hub.reasons for hub in hubs.hubs[:3]))

                report_files = graph.project_report(root, output_dir=reports)
                graph_report = report_files.graph_report.read_text(encoding="utf-8")
                self.assertIn("## Project summary", graph_report)
                self.assertIn("## Compilation summary", graph_report)
                self.assertIn("## Artifact ingestion", graph_report)
                self.assertIn("## Code graph summary", graph_report)
                self.assertIn("## God nodes / hubs", graph_report)
                self.assertNotIn("## Surprising connections", graph_report)
            finally:
                graph.close()

    def _write_project(self, root: Path) -> None:
        (root / "README.md").write_text(
            "\n".join(
                [
                    "# Demo",
                    "",
                    "This project documents the incremental compiler.",
                    "",
                    "## Usage",
                    "",
                    "- Run compile.",
                    "- Inspect cache.",
                    "",
                    "[Docs](https://example.com)",
                    "",
                    "![Diagram](diagram.png)",
                    "",
                    "| key | value |",
                    "| --- | --- |",
                    "| cache | delta |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (root / "notes.txt").write_text("The graph cache avoids unchanged compilation.\n\nReports explain deltas.\n", encoding="utf-8")
        (root / "app.py").write_text(
            "\n".join(
                [
                    '"""Demo module."""',
                    "import json",
                    "",
                    "class Compiler:",
                    "    def run(self):",
                    "        return helper()",
                    "",
                    "def helper():",
                    "    return json.dumps({'status': 'ok'})",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (root / "ui.js").write_text("import x from 'pkg';\nexport function render() { return x(); }\nclass View {}\n", encoding="utf-8")
        (root / "data.json").write_text('{"name": "demo"}\n', encoding="utf-8")
        (root / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
        (root / "node_modules").mkdir()
        (root / "node_modules" / "ignored.js").write_text("console.log('ignored')\n", encoding="utf-8")

    def _counts(self, graph: MemoryGraph) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in graph.store.all_nodes():
            counts[node.type] = counts.get(node.type, 0) + 1
        return counts

    def _add_cross_community_evidence(self, graph: MemoryGraph) -> None:
        left = self._cluster(graph, "compiler", ["incremental compiler", "artifact fingerprint", "graph delta"])
        right = self._cluster(graph, "retrieval", ["activation engine", "query ranking", "context report"])
        graph.detect_communities(limit=20)
        bridge, _ = graph.add_node(
            MemoryNode(
                id="node:e2e:bridge",

                type="Concept",
                label="fingerprint activation feedback loop",
                canonical_key="fingerprint activation feedback loop",
                salience=0.8,
                confidence=1.0,
                utility=0.9,
            )
        )
        graph.add_edge(MemoryEdge(from_id=bridge.id, to_id=left[0].id, type="RELATED_TO", weight=0.95, confidence=1.0))
        graph.add_edge(MemoryEdge(from_id=bridge.id, to_id=right[0].id, type="RELATED_TO", weight=0.95, confidence=1.0))

    def _cluster(self, graph: MemoryGraph, prefix: str, labels: list[str]) -> list[MemoryNode]:
        nodes: list[MemoryNode] = []
        for index, label in enumerate(labels):
            node, _ = graph.add_node(
                MemoryNode(
                    id=f"node:e2e:{prefix}:{index}",

                    type="Concept",
                    label=label,
                    canonical_key=f"e2e:{prefix}:{index}",
                    salience=0.5,
                    confidence=1.0,
                )
            )
            nodes.append(node)
        for i, left in enumerate(nodes):
            for right in nodes[i + 1 :]:
                graph.add_edge(MemoryEdge(from_id=left.id, to_id=right.id, type="RELATED_TO", weight=0.9, confidence=1.0))
        return nodes

