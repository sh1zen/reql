from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
import memory.services.incremental_compilation as incremental_compilation
from memory.artifacts.cache import artifact_cache_path
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
        self.graph = MemoryGraph.open(self.db)

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

    def test_compile_backfills_context_scope_on_skipped_artifacts(self) -> None:
        first = self.graph.compile_project(self.root)
        self.assertFalse(first.run.errors)

        targets = [
            node
            for node in self.graph.store.all_nodes()
            if node.properties.get("relative_path") in {"a.py", "README.md"}
            and node.type in {"SourceArtifact", "File", "SourceFragment", "Module"}
        ]
        self.assertTrue(targets)
        for node in targets:
            properties = dict(node.properties)
            properties.pop("context_scope", None)
            self.graph.store.update_node_fields(node.id, properties=properties)

        second = self.graph.compile_project(self.root)

        self.assertFalse(second.run.errors)
        self.assertEqual(second.run.files_changed, 0)
        self.assertTrue(second.run.nodes_updated >= len(targets))
        for node in targets:
            refreshed = self.graph.get_node(node.id)
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            expected = "docs" if refreshed.properties.get("relative_path") == "README.md" else "code"
            self.assertEqual(refreshed.properties.get("context_scope"), expected)

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
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "package.py").write_text("value = 'ignored'\n", encoding="utf-8")

        result = self.graph.compile_project(self.root, exclude_patterns=["vendor/"])
        relative_paths = {artifact.relative_path for artifact in result.scan.artifacts}
        skipped_paths = {item.relative_path for item in result.scan.skipped_files}

        self.assertNotIn("vendor/package.py", relative_paths)
        self.assertIn("vendor", skipped_paths)

    def test_compile_applies_default_ignores(self) -> None:
        (self.root / ".git" / "objects" / "ab").mkdir(parents=True)
        (self.root / ".git" / "objects" / "ab" / "packed").write_bytes(b"git object")
        (self.root / ".reql").mkdir(exist_ok=True)
        (self.root / ".reql" / "artifact-cache.json").write_text("{}", encoding="utf-8")
        (self.root / "__pycache__").mkdir(exist_ok=True)
        (self.root / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"bytecode")

        result = self.graph.compile_project(self.root)
        relative_paths = {artifact.relative_path for artifact in result.scan.artifacts}
        skipped_paths = {item.relative_path for item in result.scan.skipped_files}

        self.assertNotIn(".git/objects/ab/packed", relative_paths)
        self.assertNotIn(".reql/artifact-cache.json", relative_paths)
        self.assertNotIn("__pycache__/a.cpython-314.pyc", relative_paths)
        self.assertIn(".git", skipped_paths)
        self.assertIn(".reql", skipped_paths)
        self.assertIn("__pycache__", skipped_paths)

    def test_cache_status_applies_default_ignores(self) -> None:
        (self.root / ".git" / "objects" / "ab").mkdir(parents=True)
        (self.root / ".git" / "objects" / "ab" / "packed").write_bytes(b"git object")
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
