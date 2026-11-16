"""Regression coverage for complete scans and trustworthy compilation caches."""
from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from memory.artifacts.cache import artifact_cache_path
from memory.artifacts.compiler import ArtifactCompiler
from memory.artifacts.options import CompilationOptions
from memory.artifacts.fingerprint import DEFAULT_PARSER_VERSION
from memory.domain.exceptions import StorageError


class IncrementalIntegrityTests(unittest.TestCase):
    """Exercise cache and scanner failure transitions through the public API."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.path = self.root / "rules.py"
        self.path.write_text("def invoice_total():\n    return 1\n", encoding="utf-8")
        self.graph = MemoryGraph.open(Path(self.temp.name) / "graph.reql")
        self.addCleanup(self.graph.close)

    def test_uncached_compile_archives_deleted_artifacts_and_symbols(self) -> None:
        """Disabling memoization must preserve deletion semantics and idempotence."""
        first = self.graph.compile_project(self.root, cache_enabled=False)
        self.assertFalse(first.run.errors)
        artifact = first.scan.artifacts[0]
        symbols = self.graph.store.find_nodes_by_property("artifact_id", artifact.id, type_="Function")
        self.assertTrue(symbols)
        self.path.unlink()
        deleted = self.graph.update_project(self.root, cache_enabled=False)
        self.assertFalse(deleted.run.errors)
        self.assertEqual(deleted.run.files_deleted, 1)
        for node_id in [artifact.id, *(node.id for node in symbols)]:
            self.assertEqual(self.graph.store.get_node(node_id).status, "archived")
        self.assertEqual(self.graph.compile_project(self.root, cache_enabled=False).run.files_deleted, 0)
        self.assertFalse(artifact_cache_path(self.root).exists())

    def test_scan_errors_preserve_graph_revision_and_disk_cache(self) -> None:
        """Unreadable files and directories are scan failures, not confirmed deletions."""
        self.graph.compile_project(self.root)
        before_nodes = {node.id: node.to_dict() for node in self.graph.store.all_nodes()}
        before_edges = {edge.id: edge.to_dict() for edge in self.graph.store.all_edges()}
        cache_path = artifact_cache_path(self.root)
        before_cache = cache_path.read_bytes()
        original_open, original_scandir = Path.open, os.scandir

        def denied_open(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError("fixture read denied")
            return original_open(path, *args, **kwargs)

        def denied_scandir(path):
            if Path(path) == self.root:
                raise PermissionError("fixture listing denied")
            return original_scandir(path)

        for target, denied in (("pathlib.Path.open", denied_open),
                               ("memory.artifacts.scanner.os.scandir", denied_scandir)):
            for cache_enabled in (True, False):
                with self.subTest(target=target, cache_enabled=cache_enabled):
                    with patch(target, denied), self.assertRaisesRegex(StorageError, "scan.*failed|Scan.*failed"):
                        self.graph.compile_project(self.root, cache_enabled=cache_enabled)
                    self.assertEqual({node.id: node.to_dict() for node in self.graph.store.all_nodes()}, before_nodes)
                    self.assertEqual({edge.id: edge.to_dict() for edge in self.graph.store.all_edges()}, before_edges)
                    self.assertEqual(cache_path.read_bytes(), before_cache)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)

    def test_uncached_deletion_invalidates_existing_cache_entries(self) -> None:
        """Re-enabling the cache must not rediscover an already processed deletion."""
        self.graph.compile_project(self.root)
        self.path.unlink()
        self.assertEqual(self.graph.compile_project(self.root, cache_enabled=False).run.files_deleted, 1)
        self.assertEqual(self.graph.compile_project(self.root).run.files_deleted, 0)

    def test_deleted_artifact_is_archived_even_when_its_first_compile_failed(self) -> None:
        """Registration can survive a parser failure without any completed source snapshot."""
        with patch.object(ArtifactCompiler, "_read_verified_content", side_effect=PermissionError("read denied")):
            failed = self.graph.compile_project(self.root)
        self.assertTrue(failed.run.errors)
        artifact = failed.scan.artifacts[0]
        self.path.unlink()
        deleted = self.graph.compile_project(self.root)
        self.assertEqual(deleted.run.files_deleted, 1)
        self.assertEqual(self.graph.store.get_node(artifact.id).status, "archived")

    def test_missing_cache_cannot_recover_under_different_compile_options(self) -> None:
        """Matching bytes do not establish that the requested document policy ran."""
        document = self.root / "guide.md"
        document.write_text("# Invoice processing\n\nInvoice validation rules.\n", encoding="utf-8")
        disabled = CompilationOptions(ingest_documents=False)
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=disabled).run.errors)
        self.graph.clear_cache(self.root)
        enabled = self.graph.compile_project(self.root, parsing_options=CompilationOptions(ingest_documents=True))
        self.assertFalse(enabled.run.errors)
        self.assertEqual(enabled.run.files_changed, 2)
        doc_artifact = next(item for item in enabled.scan.artifacts if item.relative_path == "guide.md")
        self.assertTrue(self.graph.store.find_nodes_by_property("artifact_id", doc_artifact.id, type_="SourceFragment"))

    def test_matching_missing_cache_recovers_but_new_parser_version_recompiles(self) -> None:
        """Recovery requires the same compiler version as the completed source snapshot."""
        self.graph.compile_project(self.root)
        self.graph.clear_cache(self.root)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)
        self.graph.clear_cache(self.root)
        with patch("memory.services.incremental_compilation.DEFAULT_PARSER_VERSION", DEFAULT_PARSER_VERSION + "-next"):
            self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 1)

    def test_uncached_option_changes_cannot_reuse_an_older_cache_entry(self) -> None:
        """An existing cache entry must also describe the graph's last actual compile."""
        self.graph.compile_project(self.root)
        alternate = CompilationOptions(ingest_documents=False)
        self.graph.compile_project(self.root, cache_enabled=False, parsing_options=alternate)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 1)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)

    def test_failed_compile_cannot_recover_from_scan_time_content_metadata(self) -> None:
        """Only the digest of successfully compiled bytes establishes cache provenance."""
        self.graph.compile_project(self.root)
        self.graph.clear_cache(self.root)
        self.path.write_text("def invoice_total():\n    return 2\n", encoding="utf-8")
        with patch.object(ArtifactCompiler, "_read_verified_content", side_effect=PermissionError("read denied")):
            self.assertTrue(self.graph.compile_project(self.root).run.errors)
        retried = self.graph.compile_project(self.root)
        self.assertFalse(retried.run.errors)
        self.assertEqual(retried.run.files_changed, 1)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)

if __name__ == "__main__":
    unittest.main()
