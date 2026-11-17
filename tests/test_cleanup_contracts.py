"""Core extraction, document processing, centrality and storage lock contracts."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.analysis.centrality import CentralityCalculator
from memory.artifacts.models import SourceArtifact
from memory.code_analysis import TreeSitterCodeParser
from memory.document_ingestion.metadata import make_fragment
from memory.domain.exceptions import StorageError
from memory.domain.models import MemoryEdge, MemoryNode
from memory.extraction.document_processor import DocumentProcessor
from memory.storage import BlockGraphStore, inspect_store_locks


class CleanupContractTests(unittest.TestCase):
    def test_ecmascript_extractors_retain_typed_signatures_and_call_owners(self) -> None:
        """Exercise the registered JavaScript, TypeScript and TSX parser paths."""
        for language, suffix in (("javascript", "js"), ("typescript", "ts"), ("tsx", "tsx")):
            typed = language != "javascript"
            argument = "value: string" if typed else "value"
            returns = ": Promise<string>" if typed else ""
            text = (
                'import { helper } from "./helper";\n'
                'export { helper } from "./helper";\n'
                '// Shared syntax with nested scope.\n'
                'class Service extends Base {\n'
                f'    async run({argument}){returns} {{ return helper(value); }}\n'
                '}\n'
                f'async function outer({argument}){returns} {{\n'
                '    function inner(input) { return helper(input); }\n'
                '    return inner(value);\n'
                '}\n'
                'const render = (value) => helper(value);\n'
                'helper("total");\n'
            )
            if language == "tsx":
                text += 'export function View() { return <section>{render("total")}</section>; }\n'
            artifact = SourceArtifact(
                id="artifact", project_id="project", uri="file:///sample",
                path=f"sample.{suffix}", relative_path=f"sample.{suffix}",
                artifact_type="code", language=language,
                size_bytes=len(text.encode("utf-8")), sha256="", mtime=0,
            )
            with self.subTest(language=language):
                result = TreeSitterCodeParser().parse_artifact(artifact, text)
                self.assertEqual(result.errors, [])
                self.assertNotIn("parser_status", result.module.metadata)
                self.assertEqual([item.module for item in result.imports], ["./helper", "./helper"])
                self.assertEqual([item.text for item in result.comments], ["// Shared syntax with nested scope."])
                symbols = {item.name: item for item in result.symbols}
                self.assertEqual(symbols["Service"].bases, ["Base"])
                self.assertEqual(symbols["run"].kind, "async_method")
                self.assertEqual(symbols["run"].args, ["value"])
                self.assertEqual(symbols["run"].returns, "Promise<string>" if typed else None)
                self.assertEqual(symbols["outer"].kind, "async_function")
                self.assertEqual(symbols["inner"].parent_qualified_name, "sample.outer")
                calls = [
                    ("sample.Service.run", "helper"),
                    ("sample.outer.inner", "helper"),
                    ("sample.outer", "inner"),
                    ("sample.render", "helper"),
                    (None, "helper"),
                ]
                if language == "tsx":
                    calls.append(("sample.View", "render"))
                self.assertEqual([(item.caller, item.target) for item in result.calls], calls)
                self.assertEqual([item.name for item in result.references], [target for _, target in calls])
                self.assertEqual(len({item.id for item in result.calls}), len(calls))

    def test_document_events_preserve_duplicate_counts_and_first_evidence(self) -> None:
        """Repeated fragment identities accumulate counts without duplicating membership."""
        first = make_fragment(artifact_id="doc", fragment_type="paragraph", index=0,
                              text="invoice invoice validation", start_line=1)
        second = make_fragment(artifact_id="doc", fragment_type="paragraph", index=1,
                               text="invoice validation receipt", start_line=2)
        result = DocumentProcessor().process([first, second, first])
        invoice = next(term for term in result.terms if term.key == "invoice")
        self.assertEqual(invoice.term_frequency, 5.0)
        self.assertEqual(invoice.fragment_count, 2)
        self.assertEqual(invoice.first_fragment_id, first.id)
        self.assertEqual(invoice.evidence, first.text)
        self.assertEqual([(event.fragment_id, event.occurrence_count) for event in invoice.raw_events],
                         [(first.id, 4.0), (second.id, 1.0)])
        self.assertTrue(all(any(event is item for item in result.raw_events) for event in invoice.raw_events))
        relation = next(item for item in result.relations
                        if {item.source_key, item.target_key} == {"invoice", "validation"})
        self.assertEqual(relation.cooccurrence_count, 3.0)
        self.assertEqual(relation.fragment_count, 2)
        self.assertEqual(relation.evidence_fragment_id, min(first.id, second.id))
        self.assertEqual(relation.evidence, first.text)

    def test_document_empty_and_zero_event_budgets_keep_result_shapes(self) -> None:
        """Keep empty signatures and ranked multilingual terms under zero event budgets."""
        empty = DocumentProcessor().process([])
        self.assertEqual(empty.signature, {"term_count": 0, "relation_count": 0})
        fragment = make_fragment(artifact_id="doc", fragment_type="paragraph", index=0,
                                 text="invoice validation café 付款 東京")
        result = DocumentProcessor(max_events_per_term=0).process([fragment])
        self.assertIn("付款", {term.key for term in result.terms})
        self.assertEqual(result.raw_events, [])
        self.assertTrue(all(not term.raw_events for term in result.terms))
        self.assertEqual(result.signature["raw_event_count"], 0)
        limited = DocumentProcessor(max_terms=0).process([fragment])
        self.assertEqual(limited.terms, [])
        self.assertEqual(limited.relations, [])
        self.assertEqual(limited.signature,
                         {"term_count": 0, "relation_count": 0, "raw_event_count": 0, "top_terms": []})

    def test_centrality_preserves_star_triangle_and_isolated_scores(self) -> None:
        """Compute bridge ratios through the graph store, including isolated nodes."""
        with tempfile.TemporaryDirectory() as temporary:
            graph = MemoryGraph.open(Path(temporary) / "graph.reql")
            try:
                for name in ("a", "b", "c", "d", "isolated"):
                    graph.store.upsert_node(MemoryNode(id=name, type="Function", label=name))
                for left, right in (("a", "b"), ("a", "c"), ("a", "d"), ("b", "c")):
                    graph.store.upsert_edge(MemoryEdge(from_id=left, to_id=right, type="CALLS"))
                metrics = CentralityCalculator(graph.store).calculate()
                self.assertEqual(metrics["a"].bridge_score, 2 / 3)
                for name in ("b", "c", "d", "isolated"):
                    self.assertEqual(metrics[name].bridge_score, 0.0)
                self.assertEqual(metrics["a"].out_degree, 3)
                self.assertEqual(metrics["isolated"].degree_centrality, 0.0)
            finally:
                graph.close()

    def test_store_locks_keep_multiple_readers_and_writer_exclusion(self) -> None:
        """Real store opens retain lock ownership and release failed acquisition slots."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "graph.reql"
            writer = BlockGraphStore(path, lock_timeout_seconds=0)
            try:
                self.assertIsNotNone(inspect_store_locks(path)["writer"])
                with self.assertRaises(StorageError):
                    BlockGraphStore(path, read_only=True, lock_timeout_seconds=0)
                self.assertEqual(inspect_store_locks(path)["readers"], [])
            finally:
                writer.close()
            first = BlockGraphStore(path, read_only=True, lock_timeout_seconds=0)
            try:
                second = BlockGraphStore(path, read_only=True, lock_timeout_seconds=0)
                try:
                    self.assertEqual(len(inspect_store_locks(path)["readers"]), 2)
                    with self.assertRaises(StorageError):
                        BlockGraphStore(path, lock_timeout_seconds=0)
                    self.assertIsNone(inspect_store_locks(path)["writer"])
                finally:
                    second.close()
            finally:
                first.close()
            self.assertFalse(inspect_store_locks(path)["locked"])
            reopened = BlockGraphStore(path, lock_timeout_seconds=0)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
