"""Protect work bounds and result isolation in large-project hot paths without timing assertions."""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from memory.artifacts.scanner import ProjectScanner
from memory.domain.ids import stable_id
from memory.domain.models import MemoryNode
from memory.extraction import normalization
from memory.storage import BlockGraphStore


class LargeProjectPerformanceTests(unittest.TestCase):
    """Exercise actual scanner, index and retrieval paths with small deterministic fixtures."""

    def setUp(self) -> None:
        scratch = Path.cwd() / ".tmp" / "regression-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_token_analysis_is_reused_without_exposing_mutable_cached_values(self) -> None:
        """Repeated immutable text may reuse analysis, but public results remain independent."""
        self.assertEqual(normalization.canonicalize("  Città\n‘Invoice’ — PayMéNT_42  "), "citta invoice payment_42")
        self.assertEqual(normalization.identifier_expanded_text("src/HTTPServer.invoice_total"), "src HTTP Server invoice total")
        self.assertEqual(normalization.tokenize("Invoice Payment"), ["invoice", "payment"])
        normalization._cached_token_signals.cache_clear()
        text = "Città InvoicePayment_42 preserves billing contracts"
        expected = normalization.tokenize(text)
        first = normalization._cached_token_signals.cache_info()
        mutated = normalization.tokenize(text)
        mutated.clear()
        keywords = normalization.keyword_scores(text)
        keywords.clear()
        self.assertEqual(normalization.tokenize(text), expected)
        self.assertTrue(normalization.keyword_scores(text))
        self.assertGreater(normalization._cached_token_signals.cache_info().hits, first.hits)
        self.assertEqual(normalization.tokenize("x a invoice", keep_stopwords=True), ["invoice"])
        self.assertEqual(normalization.tokenize("invoice changed"), ["invoice", "changed"])
        _tokens, scores = normalization._tokenize_with_signal_scores(text)
        _tokens, unscored = normalization._tokenize_with_signal_scores(text, keep_stopwords=True)
        self.assertEqual(dict(unscored), {})
        with self.assertRaises(TypeError):
            scores["invoice"] = 99

    def test_long_source_text_does_not_fill_the_token_analysis_cache(self) -> None:
        """Bound retained source text while preserving analysis for arbitrarily long input."""
        normalization._cached_token_signals.cache_clear()
        value = "invoice payment " * (normalization.TOKEN_ANALYSIS_MAX_CHARS + 1)
        self.assertIn("invoice", normalization.tokenize(value))
        self.assertTrue(normalization.keyword_scores(value))
        self.assertEqual(normalization._cached_token_signals.cache_info().currsize, 0)
        for index in range(normalization.TOKEN_ANALYSIS_CACHE_SIZE + 3):
            normalization.tokenize(f"billing_identifier_{index}")
        self.assertEqual(normalization._cached_token_signals.cache_info().currsize, normalization.TOKEN_ANALYSIS_CACHE_SIZE)

    def test_large_scan_uses_bounded_parallelism_and_preserves_content_hashes(self) -> None:
        """Mid-sized projects should use available I/O concurrency without skipping hashing."""
        for index in range(96):
            content = f"value = {index}\n".encode("utf-8")
            if index == 1:
                content = b""
            elif index == 2:
                content = '\ufeffinvoice = "café"\n'.encode("utf-8")
            (self.root / f"service_{index:03}.py").write_bytes(content)
        from concurrent.futures import ThreadPoolExecutor
        with patch("memory.artifacts.scanner.os.cpu_count", return_value=8), patch(
            "memory.artifacts.scanner.ThreadPoolExecutor", wraps=ThreadPoolExecutor
        ) as executor:
            initial = ProjectScanner().scan(self.root)
        self.assertEqual(executor.call_args.kwargs["max_workers"], 8)
        self.assertFalse(initial.errors)
        self.assertEqual(len(initial.artifacts), 96)
        self.assertEqual([item.relative_path for item in initial.artifacts], sorted(item.relative_path for item in initial.artifacts))
        self.assertEqual(initial.project.id, stable_id("project", self.root.as_posix()))
        for artifact in initial.artifacts:
            path = self.root / artifact.relative_path
            content = path.read_bytes()
            self.assertEqual(artifact.id, stable_id("artifact", initial.project.id, artifact.relative_path))
            self.assertEqual(artifact.uri, path.as_uri())
            self.assertEqual(artifact.size_bytes, len(content))
            self.assertEqual(artifact.sha256, hashlib.sha256(content).hexdigest())
        changed = self.root / "service_000.py"
        stamp = changed.stat()
        changed.write_bytes(b"value = 9\n")
        import os
        os.utime(changed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        updated = ProjectScanner().scan(self.root)
        self.assertFalse(updated.errors)
        self.assertEqual(initial.artifacts[0].id, updated.artifacts[0].id)
        self.assertNotEqual(initial.artifacts[0].sha256, updated.artifacts[0].sha256)
        self.assertEqual(initial.artifacts[0].size_bytes, updated.artifacts[0].size_bytes)

    def test_exact_file_context_does_not_search_for_a_model_field_named_after_its_extension(self) -> None:
        """Whole-path intent must not trigger the separate dotted-field source search."""
        source = self.root / "invoice_rules.py"
        source.write_text("def invoice_total(values):\n    return sum(values)\n", encoding="utf-8")
        with closing(MemoryGraph.open(self.root / ".reql" / "memory.reql")) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            with patch.object(graph.retrieval, "_scoped_lexical_search", wraps=graph.retrieval._scoped_lexical_search) as search:
                payload = graph.query_context_payload("invoice_rules.py", scopes=["code"])["payload"]
            self.assertEqual(payload["working_set"][0]["path"], "invoice_rules.py")
            self.assertEqual(payload["data_trace"], [])
            self.assertFalse(any(call.kwargs.get("lexical_node_types") == ("SourceFragment",) for call in search.call_args_list))

    def test_loaded_postings_reconcile_deferred_edits_and_deletions(self) -> None:
        """Both index directions must agree after checkpoint, lazy loading and pending writes."""
        path = self.root / "memory.reql"
        with closing(BlockGraphStore(path)) as store:
            store.upsert_node(MemoryNode(id="invoice", type="Function", label="invoice", text="invoice payment"))
            store.upsert_node(MemoryNode(id="obsolete", type="Function", text="obsolete transport"))
            store.checkpoint_if_needed(wal_bytes_threshold=0)
        with closing(BlockGraphStore(path, defer_lexical_index=True)) as store:
            store.update_node_fields("invoice", text="invoice refund")
            store.remove_node("obsolete")
            self.assertEqual([node.id for node, _score in store.lexical_search("refund")], ["invoice"])
            self.assertFalse(store.lexical_search("obsolete"))
            expected = {node_id: {term for term, postings in store._node_terms.items() if node_id in postings}
                        for node_id in store._node_term_index}
            self.assertEqual(store._node_term_index, expected)
            store.checkpoint_if_needed(wal_bytes_threshold=0)
        with closing(BlockGraphStore(path, read_only=True)) as reopened:
            self.assertEqual([node.id for node, _score in reopened.lexical_search("refund")], ["invoice"])
            self.assertFalse(reopened.lexical_search("obsolete"))

    def test_posting_conversion_failure_does_not_publish_partial_indexes(self) -> None:
        """Failed lexical loading preserves both old directions and can retry normally."""
        path = self.root / "memory.reql"
        with closing(BlockGraphStore(path)) as store:
            store.upsert_node(MemoryNode(id="invoice", type="Function", text="invoice payment"))
            store.checkpoint_if_needed(wal_bytes_threshold=0)
        with closing(BlockGraphStore(path, read_only=True, defer_lexical_index=True)) as store:
            previous_terms, previous_reverse = store._node_terms, store._node_term_index
            payload = {"kind": "lexical_index", "value": {"node_terms": {"valid": {"invoice": 0.5}, "invalid": {"invoice": "bad-weight"}}}}
            with patch.object(store, "_read_record_at", return_value=payload), self.assertRaises(ValueError):
                store.lexical_search("invoice")
            self.assertIs(store._node_terms, previous_terms)
            self.assertIs(store._node_term_index, previous_reverse)
            self.assertFalse(store._lexical_index_loaded)
            self.assertEqual([node.id for node, _score in store.lexical_search("invoice")], ["invoice"])


if __name__ == "__main__":
    unittest.main()
