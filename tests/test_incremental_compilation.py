from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.config_helpers import open_graph_with_documents as _open_graph_with_documents
import memory.services.incremental_compilation as incremental_compilation
from memory.artifacts.cache import DiskArtifactCache, artifact_cache_path
from memory.artifacts.compiler import ArtifactCompiler
from memory.artifacts.models import SourceArtifact
from memory.domain.timeutils import utcnow_iso
from memory.document_ingestion.models import DocumentFragment, DocumentParseResult
from memory.services.incremental_compilation import IncrementalCompilationService


class FailingCompiler(ArtifactCompiler):
    def build_fragments(self, artifact: SourceArtifact):
        if artifact.relative_path == "bad.py":
            raise ValueError("simulated parser failure")
        return super().build_fragments(artifact)


class DuplicateCanonicalFragmentCompiler(ArtifactCompiler):
    def build_fragments(self, artifact: SourceArtifact):
        if artifact.relative_path != "README.md":
            return super().build_fragments(artifact)
        first = DocumentFragment(
            id="candidate-fragment-one",
            artifact_id=artifact.id,
            fragment_type="paragraph",
            text="First fragment.",
            start_line=1,
            end_line=1,
            start_offset=None,
            end_offset=None,
            page_number=None,
            section_path=None,
            hash="h1",
            metadata={"structural_hash": "same-structure", "fragment_index": 0},
        )
        second = DocumentFragment(
            id="candidate-fragment-two",
            artifact_id=artifact.id,
            fragment_type="paragraph",
            text="Second fragment.",
            start_line=2,
            end_line=2,
            start_offset=None,
            end_offset=None,
            page_number=None,
            section_path=None,
            hash="h2",
            metadata={"structural_hash": "same-structure", "fragment_index": 1},
        )
        return DocumentParseResult(
            title="Duplicate",
            metadata={},
            fragments=[first, second],
            links=[{"source_fragment_id": second.id, "uri": "https://example.test/ref", "text": "ref"}],
            tables=[],
            errors=[],
            parser_name="duplicate-test",
            parser_version="1",
        )


class IncrementalCompilationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "memory.reql"
        (self.root / "a.py").write_text("print('a')\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Title\n\nBody\n", encoding="utf-8")
        self.graph = _open_graph_with_documents(self.db)

    def tearDown(self) -> None:
        self.graph.close()
        self.tmp.cleanup()

    def test_first_compile_compiles_all_artifacts(self) -> None:
        result = self.graph.compile_project(self.root)
        cache_path = artifact_cache_path(self.root)
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        project_cache = payload["projects"][result.scan.project.id]

        self.assertEqual(result.run.files_seen, 2)
        self.assertEqual(result.run.files_changed, 2)
        self.assertEqual(result.run.files_skipped, 0)
        self.assertEqual(result.run.status, "completed")
        self.assertGreaterEqual(result.run.nodes_created, 2)
        self.assertEqual(len(self._nodes("ArtifactCacheEntry")), 2)
        self.assertGreaterEqual(len(self._nodes("SourceFragment")), 2)
        self.assertEqual(payload["format"], "reql-artifact-cache-v1")
        self.assertEqual(set(project_cache["entries"]), {artifact.id for artifact in result.scan.artifacts})

    def test_compile_records_content_addressed_revision_chain_and_file_changes(self) -> None:
        first = self.graph.compile_project(self.root)

        self.assertIsNotNone(first.revision)
        assert first.revision is not None
        self.assertEqual(first.revision.sequence, 1)
        self.assertIsNone(first.revision.parent_id)
        self.assertEqual({change.path: change.status for change in first.revision.changes}, {"README.md": "added", "a.py": "added"})

        no_op = self.graph.compile_project(self.root)
        self.assertIsNone(no_op.revision)
        self.assertEqual(len(self.graph.project_history(self.root)), 1)

        (self.root / "a.py").write_text("print('changed')\n", encoding="utf-8")
        (self.root / "README.md").unlink()
        (self.root / "b.py").write_text("VALUE = 2\n", encoding="utf-8")
        second = self.graph.compile_project(self.root)

        self.assertIsNotNone(second.revision)
        assert second.revision is not None
        self.assertEqual(second.revision.sequence, 2)
        self.assertEqual(second.revision.parent_id, first.revision.id)
        self.assertEqual(
            {change.path: change.status for change in second.revision.changes},
            {"README.md": "deleted", "a.py": "modified", "b.py": "added"},
        )
        self.assertNotEqual(second.revision.tree_hash, first.revision.tree_hash)
        self.assertEqual([item.id for item in self.graph.project_history(self.root)], [second.revision.id, first.revision.id])

    def test_compile_summary_keeps_unchanged_associated_test_for_modified_source(self) -> None:
        source = self.root / "calculator.py"
        test_dir = self.root / "tests"
        test_dir.mkdir()
        test_path = test_dir / "test_calculator.py"
        source.write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
        test_path.write_text(
            "from calculator import add\n\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        (test_dir / "test_unrelated.py").write_text("def test_unrelated():\n    assert True\n", encoding="utf-8")
        self.graph.compile_project(self.root)

        source.write_text("def add(left, right):\n    return int(left) + int(right)\n", encoding="utf-8")
        result = self.graph.compile_project(self.root)

        self.assertEqual([item["path"] for item in result.summary.changed_files], ["calculator.py"])
        self.assertIn("add", {item.name.rsplit(".", 1)[-1] for item in result.summary.updated_symbols})
        self.assertEqual([item.path for item in result.summary.associated_tests], ["tests/test_calculator.py"])
        self.assertIn("summary", result.to_dict())

    def test_code_context_omits_automatic_revision_section(self) -> None:
        self.graph.compile_project(self.root)

        context = self.graph.query_context("print a", scopes=["code"])
        payload = self.graph.query_context_payload("print a", scopes=["code"])

        self.assertNotIn("## Recent changes", context)
        self.assertNotIn("recent_changes", payload)

    def test_compile_flushes_disk_cache_once_per_batch(self) -> None:
        original_write = DiskArtifactCache._write
        with patch.object(DiskArtifactCache, "_write", autospec=True, side_effect=original_write) as write:
            result = self.graph.compile_project(self.root)

        self.assertEqual(result.run.files_changed, 2)
        self.assertEqual(write.call_count, 1)

    def test_disk_cache_is_used_when_graph_cache_nodes_are_missing(self) -> None:
        self.graph.compile_project(self.root)
        for node in self._nodes("ArtifactCacheEntry"):
            properties = dict(node.properties)
            properties["status"] = "archived"
            properties["updated_at"] = utcnow_iso()
            self.graph.store.update_node_fields(node.id, status="archived", properties=properties)

        result = self.graph.compile_project(self.root)

        self.assertEqual(result.run.files_seen, 2)
        self.assertEqual(result.run.files_changed, 0)
        self.assertEqual(result.run.files_skipped, 2)

        self.graph.clear_cache(self.root)
        recovered = self.graph.compile_project(self.root)
        self.assertEqual(recovered.run.files_seen, 2)
        self.assertEqual(recovered.run.files_changed, 0)
        self.assertEqual(recovered.run.files_skipped, 2)
        self.assertEqual(recovered.run.files_deleted, 0)
        self.assertEqual(len(self._nodes("ArtifactCacheEntry")), 2)

    def test_clear_cache_archives_disk_cache_entries(self) -> None:
        result = self.graph.compile_project(self.root)

        clear = self.graph.clear_cache(self.root)
        payload = json.loads(artifact_cache_path(self.root).read_text(encoding="utf-8"))
        statuses = {
            entry["status"]
            for entry in payload["projects"][result.scan.project.id]["entries"].values()
        }

        self.assertEqual(clear["cleared_entries"], 2)
        self.assertEqual(statuses, {"archived"})

    def test_cold_compile_creates_expected_technical_node_and_edge_types(self) -> None:
        (self.root / "a.py").write_text(
            "\n".join(
                [
                    "class Service:",
                    "    def run(self):",
                    "        return helper()",
                    "",
                    "def helper():",
                    "    return 'ok'",
                ]
            ),
            encoding="utf-8",
        )

        self.graph.compile_project(self.root)
        node_types = {node.type for node in self.graph.store.all_nodes()}
        edge_types = {edge.type for edge in self.graph.store.all_edges()}

        self.assertTrue({"Project", "SourceArtifact", "SourceFragment", "Module", "Class", "Method", "Function"}.issubset(node_types))
        self.assertTrue({"CONTAINS", "DEFINES", "CONTAINS_FRAGMENT", "DERIVED_FROM", "CALLS"}.issubset(edge_types))

    def test_fragment_edges_use_persisted_id_returned_for_canonical_key(self) -> None:
        service = IncrementalCompilationService(self.graph.store, compiler=DuplicateCanonicalFragmentCompiler())

        service.compile_path(self.root)
        edges = self.graph.store.all_edges()

        self.assertIsNotNone(self.graph.get_node("candidate-fragment-one"))
        self.assertIsNone(self.graph.get_node("candidate-fragment-two"))
        self.assertFalse(any(edge.from_id == "candidate-fragment-two" or edge.to_id == "candidate-fragment-two" for edge in edges))
        self.assertTrue(any(edge.from_id == "candidate-fragment-one" and edge.type == "LINKS_TO" for edge in edges))

    def test_second_compile_on_unchanged_project_skips_all_files(self) -> None:
        self.graph.compile_project(self.root)
        result = self.graph.compile_project(self.root)

        self.assertEqual(result.run.files_seen, 2)
        self.assertEqual(result.run.files_changed, 0)
        self.assertEqual(result.run.files_skipped, 2)
        self.assertEqual(result.run.nodes_created, 0)
        self.assertEqual(result.run.edges_created, 0)

    def test_large_project_detects_same_size_change_with_restored_mtime(self) -> None:
        large_payload = "VALUE = 1\n" + ("x" * 16384)
        paths = [self.root / f"large_{index}.py" for index in range(12)]
        for path in paths:
            path.write_text(large_payload, encoding="utf-8")
        self.graph.compile_project(self.root)

        unchanged = self.graph.compile_project(self.root)
        self.assertEqual(unchanged.run.files_changed, 0)

        original_stat = paths[5].stat()
        changed_payload = "VALUE = 2\n" + ("x" * 16384)
        self.assertEqual(len(changed_payload), len(large_payload))
        paths[5].write_text(changed_payload, encoding="utf-8")
        os.utime(paths[5], ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        changed = self.graph.compile_project(self.root)

        self.assertEqual(changed.run.files_changed, 1)

    def test_compile_detects_deleted_artifact_when_cache_is_missing(self) -> None:
        first = self.graph.compile_project(self.root)
        artifact_id = self._artifact_id(first, "a.py")
        self.graph.clear_cache(self.root)
        (self.root / "a.py").unlink()

        result = self.graph.compile_project(self.root)
        artifact = self.graph.get_node(artifact_id)

        self.assertEqual(result.run.files_changed, 0)
        self.assertEqual(result.run.files_deleted, 1)
        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertEqual(artifact.status, "archived")

    def test_modifying_one_file_recompiles_only_that_file(self) -> None:
        first = self.graph.compile_project(self.root)
        initial_fragment_count = len(self._nodes("SourceFragment"))
        (self.root / "a.py").write_text("print('changed')\n", encoding="utf-8")

        second = self.graph.compile_project(self.root)

        self.assertEqual(second.run.files_changed, 1)
        self.assertEqual(second.run.files_skipped, 1)
        self.assertIn(self._artifact_id(first, "a.py"), second.dirty_set.changed_artifact_ids)
        self.assertEqual(len(self._nodes("SourceFragment")), initial_fragment_count)
        self.assertGreaterEqual(second.run.nodes_updated, 1)

    def test_document_and_code_changes_use_expected_document_code_relinking_scope(self) -> None:
        (self.root / "a.py").write_text("def AlphaService():\n    return 'ok'\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Title\n\nAlphaService is documented.\n", encoding="utf-8")
        first = self.graph.compile_project(self.root)
        readme_id = self._artifact_id(first, "README.md")
        (self.root / "README.md").write_text("# Title\n\nAlphaService is documented again.\n", encoding="utf-8")

        with patch("memory.services.incremental_compilation.link_document_fragments_to_code", wraps=incremental_compilation.link_document_fragments_to_code) as linker:
            self.graph.compile_project(self.root)

        self.assertTrue(linker.called)
        self.assertEqual(linker.call_args.kwargs["artifact_ids"], {readme_id})

        (self.root / "a.py").write_text("def BetaService():\n    return 'ok'\n", encoding="utf-8")

        with patch("memory.services.incremental_compilation.link_document_fragments_to_code", wraps=incremental_compilation.link_document_fragments_to_code) as linker:
            self.graph.compile_project(self.root)

        self.assertTrue(linker.called)
        self.assertIsNone(linker.call_args.kwargs["artifact_ids"])

    def test_cache_planning_distinguishes_clean_changed_deleted_and_recoverable(self) -> None:
        first = self.graph.compile_project(self.root)
        readme_id = self._artifact_id(first, "README.md")
        code_id = self._artifact_id(first, "a.py")

        clean = self.graph.compile_project(self.root)
        self.assertEqual(clean.dirty_set.changed_artifact_ids, set())

        (self.root / "README.md").write_text("# Title\n\nChanged body\n", encoding="utf-8")
        changed = self.graph.compile_project(self.root)
        self.assertEqual(changed.dirty_set.changed_artifact_ids, {readme_id})

        self.graph.clear_cache(self.root)
        recovered = self.graph.compile_project(self.root)
        self.assertEqual(recovered.run.files_changed, 0)

        (self.root / "a.py").unlink()
        deleted = self.graph.compile_project(self.root)
        self.assertIn(code_id, deleted.dirty_set.deleted_artifact_ids)

    def test_deleting_one_file_archives_artifact_and_fragments(self) -> None:
        (self.root / "a.py").write_text(
            "\n".join(
                [
                    "import sys",
                    "",
                    "def removed():",
                    "    local_unused = 1",
                    "    return 'gone'",
                ]
            ),
            encoding="utf-8",
        )
        first = self.graph.compile_project(self.root)
        artifact_id = self._artifact_id(first, "a.py")
        code_nodes_before = [
            node
            for node in self.graph.store.all_nodes()
            if node.properties.get("artifact_id") == artifact_id
            and node.type in {"Module", "Function", "Import", "Variable", "StaticAnalysisFinding"}
        ]
        code_edges_before = [
            edge
            for edge in self.graph.store.all_edges()
            if edge.properties.get("artifact_id") == artifact_id
        ]
        (self.root / "a.py").unlink()

        second = self.graph.compile_project(self.root)
        artifact = self.graph.get_node(artifact_id)
        fragments = [
            node
            for node in self._nodes("SourceFragment")
            if node.properties.get("artifact_id") == artifact_id
        ]

        self.assertEqual(second.run.files_deleted, 1)
        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertEqual(artifact.status, "archived")
        self.assertTrue(fragments)
        self.assertTrue(all(node.status == "archived" for node in fragments))
        self.assertTrue(code_nodes_before)
        self.assertTrue(code_edges_before)
        self.assertTrue(
            all(
                self.graph.get_node(node.id) is not None and self.graph.get_node(node.id).status == "archived"
                for node in code_nodes_before
            )
        )
        active_code_nodes = [
            node
            for node in self.graph.store.all_nodes()
            if node.properties.get("artifact_id") == artifact_id
            and node.type in {"Module", "Function", "Import", "Variable", "StaticAnalysisFinding"}
            and node.status == "active"
        ]
        active_code_edges = [
            edge
            for edge in self.graph.store.all_edges()
            if edge.properties.get("artifact_id") == artifact_id
            and edge.properties.get("status") != "archived"
        ]
        self.assertFalse(active_code_nodes)
        self.assertFalse(active_code_edges)

    def test_compile_uses_config_exclude_rules(self) -> None:
        (self.root / "generated").mkdir()
        (self.root / "generated" / "package.py").write_text("value = 'ignored'\n", encoding="utf-8")

        result = self.graph.compile_project(self.root, exclude_patterns=["generated/"])
        relative_paths = {artifact.relative_path for artifact in result.scan.artifacts}
        skipped_paths = {item.relative_path for item in result.scan.skipped_files}

        self.assertNotIn("generated/package.py", relative_paths)
        self.assertIn("generated", skipped_paths)

    def test_compile_applies_default_ignores(self) -> None:
        (self.root / ".git" / "objects" / "ab").mkdir(parents=True)
        (self.root / ".git" / "objects" / "ab" / "packed").write_bytes(b"git object")
        (self.root / ".cache").mkdir(exist_ok=True)
        (self.root / ".cache" / "artifact.json").write_text("{}", encoding="utf-8")
        (self.root / ".reql").mkdir(exist_ok=True)
        (self.root / ".reql" / "artifact-cache.json").write_text("{}", encoding="utf-8")
        (self.root / "__pycache__").mkdir(exist_ok=True)
        (self.root / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"bytecode")

        result = self.graph.compile_project(self.root)
        relative_paths = {artifact.relative_path for artifact in result.scan.artifacts}
        skipped_paths = {item.relative_path for item in result.scan.skipped_files}

        self.assertNotIn(".git/objects/ab/packed", relative_paths)
        self.assertNotIn(".cache/artifact.json", relative_paths)
        self.assertNotIn(".reql/artifact-cache.json", relative_paths)
        self.assertNotIn("__pycache__/a.cpython-314.pyc", relative_paths)
        self.assertIn(".git", skipped_paths)
        self.assertIn(".cache", skipped_paths)
        self.assertIn(".reql", skipped_paths)
        self.assertIn("__pycache__", skipped_paths)

    def test_compile_and_cache_status_can_apply_root_gitignore(self) -> None:
        (self.root / ".gitignore").write_text("ignored.py\n!keep.py\n!.cache/\n", encoding="utf-8")
        (self.root / "ignored.py").write_text("ignored = True\n", encoding="utf-8")
        (self.root / "keep.py").write_text("kept = True\n", encoding="utf-8")
        (self.root / ".cache").mkdir()
        (self.root / ".cache" / "protected.py").write_text("protected = True\n", encoding="utf-8")
        parsing_options = {"scan": {"use_gitignore": True}}

        result = self.graph.compile_project(self.root, parsing_options=parsing_options)
        relative_paths = {artifact.relative_path for artifact in result.scan.artifacts}
        skipped_paths = {item.relative_path for item in result.scan.skipped_files}
        status = self.graph.cache_status(self.root, parsing_options=parsing_options)

        self.assertNotIn("ignored.py", relative_paths)
        self.assertIn("ignored.py", skipped_paths)
        self.assertIn("keep.py", relative_paths)
        self.assertNotIn(".cache/protected.py", relative_paths)
        self.assertIn(".cache", skipped_paths)
        self.assertEqual(status["total_artifacts"], len(result.scan.artifacts))

    def test_scan_ignore_defaults_disables_internal_ignore_matcher(self) -> None:
        (self.root / ".cache").mkdir()
        (self.root / ".cache" / "included.py").write_text("included = True\n", encoding="utf-8")

        result = self.graph.compile_project(
            self.root,
            parsing_options={"scan": {"ignore_defaults": True}},
        )
        relative_paths = {artifact.relative_path for artifact in result.scan.artifacts}

        self.assertIn(".cache/included.py", relative_paths)

    def test_cache_status_applies_default_ignores(self) -> None:
        (self.root / ".git" / "objects" / "ab").mkdir(parents=True)
        (self.root / ".git" / "objects" / "ab" / "packed").write_bytes(b"git object")
        (self.root / ".cache").mkdir(exist_ok=True)
        (self.root / ".cache" / "artifact.json").write_text("{}", encoding="utf-8")
        (self.root / "__pycache__").mkdir(exist_ok=True)
        (self.root / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"bytecode")

        status = self.graph.cache_status(self.root)

        self.assertEqual(status["total_artifacts"], 2)

    def test_graph_delta_is_persisted_and_contains_affected_ids(self) -> None:
        result = self.graph.compile_project(self.root)
        deltas = self.graph.list_deltas()
        delta_node = self.graph.get_node(result.delta.id)

        self.assertTrue(deltas)
        self.assertIsNotNone(delta_node)
        self.assertEqual(deltas[0].id, result.delta.id)
        self.assertTrue(result.delta.affected_node_ids)
        assert delta_node is not None
        self.assertIn(result.delta.affected_node_ids[0], delta_node.properties["affected_node_ids"])

    def test_simulated_parser_failure_does_not_corrupt_prior_compiled_graph(self) -> None:
        (self.root / "bad.py").write_text("print('ok')\n", encoding="utf-8")
        self.graph.compile_project(self.root)
        bad_artifact = self._artifact_node("bad.py")
        self.assertIsNotNone(bad_artifact)
        assert bad_artifact is not None
        old_cache = [
            node
            for node in self._nodes("ArtifactCacheEntry")
            if node.properties.get("artifact_id") == bad_artifact.id
        ][0]
        old_fragments = [
            node
            for node in self._nodes("SourceFragment")
            if node.properties.get("artifact_id") == bad_artifact.id
        ]
        (self.root / "bad.py").write_text("raise RuntimeError('changed')\n", encoding="utf-8")
        service = IncrementalCompilationService(self.graph.store, compiler=FailingCompiler())

        result = service.compile_path(self.root)
        new_cache = self.graph.get_node(old_cache.id)
        fragments_after = [
            node
            for node in self._nodes("SourceFragment")
            if node.properties.get("artifact_id") == bad_artifact.id
        ]

        self.assertEqual(result.run.status, "failed")
        self.assertTrue(result.run.errors)
        self.assertIsNone(result.revision)
        self.assertEqual(len(self.graph.project_history(self.root)), 1)
        self.assertIsNotNone(new_cache)
        assert new_cache is not None
        self.assertEqual(new_cache.properties["sha256"], old_cache.properties["sha256"])
        self.assertEqual({node.id for node in fragments_after}, {node.id for node in old_fragments})
        self.assertTrue(all(node.status == "active" for node in fragments_after))

    def _nodes(self, type_: str):
        return [node for node in self.graph.store.all_nodes() if node.type == type_]

    def _artifact_node(self, relative_path: str):
        for node in self._nodes("SourceArtifact"):
            if node.properties.get("relative_path") == relative_path:
                return node
        return None

    def _artifact_id(self, result, relative_path: str) -> str:
        for artifact in result.scan.artifacts:
            if artifact.relative_path == relative_path:
                return artifact.id
        raise AssertionError(f"artifact not found: {relative_path}")


if __name__ == "__main__":
    unittest.main()
