from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.config import default_config, merge_config
from memory.domain.models import MemoryNode


class AutomaticRetentionTests(unittest.TestCase):
    def test_compile_prunes_expired_project_data_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            source = project / "old.py"
            source.write_text("value = 1\n", encoding="utf-8")
            config = merge_config(default_config(), {"retention.commits": 1})
            storage = root / "memory.reql"
            graph = MemoryGraph.open(storage, config=config)
            try:
                first = graph.compile_project(project)
                self.assertIsNotNone(first.retention)
                assert first.retention is not None
                self.assertFalse(first.retention.compacted)
                self.assertEqual(first.retention.records_removed, 0)
                artifact_id = first.scan.artifacts[0].id
                unrelated = MemoryNode(
                    id="other:archived",
                    type="SourceArtifact",
                    properties={"project_id": "project:other"},
                    status="archived",
                    created_at="2020-01-01T00:00:00+00:00",
                    updated_at="2020-01-01T00:00:00+00:00",
                )
                graph.add_node(unrelated)
                graph.store.record_usage_event(
                    "mixed project query",
                    [
                        {"id": artifact_id, "score": 1.0, "activation": 0.5},
                        {"id": unrelated.id, "score": 0.5, "activation": 0.2},
                    ],
                )
                source.unlink()

                result = graph.compile_project(project)

                self.assertIsNotNone(result.retention)
                assert result.retention is not None
                self.assertGreater(result.retention.records_removed, 0)
                self.assertGreater(result.retention.usage_entries_removed, 0)
                self.assertTrue(result.retention.compacted)
                self.assertIsNone(graph.get_node(artifact_id))
                self.assertIsNotNone(graph.get_node(unrelated.id))
                self.assertEqual(
                    graph.store.count_nodes(node_types={"CompilationRun"}),
                    1,
                )
                self.assertEqual(graph.store.count_nodes(node_types={"GraphDelta"}), 1)
                self.assertEqual(
                    graph.store.count_nodes(node_types={"ProjectRevision"}),
                    1,
                )

                usage_path = storage.with_name(f"{storage.name}.usage.jsonl")
                events = [
                    json.loads(line)
                    for line in usage_path.read_text(encoding="utf-8").splitlines()
                ]
                retained_ids = {
                    str(item.get("id"))
                    for event in events
                    for item in event.get("payload", {}).get("nodes", [])
                }
                self.assertNotIn(artifact_id, retained_ids)
                self.assertIn(unrelated.id, retained_ids)
            finally:
                graph.close()

            reopened = MemoryGraph.open(storage, config=config)
            try:
                self.assertIsNone(reopened.get_node(artifact_id))
                self.assertIsNotNone(reopened.get_node("other:archived"))
            finally:
                reopened.close()

    def test_retention_counts_changed_project_revisions_not_compile_invocations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            project.mkdir()
            source = project / "version.py"
            config = merge_config(default_config(), {"retention.commits": 20})
            graph = MemoryGraph.open(root / "memory.reql", config=config)
            try:
                for version in range(21):
                    source.write_text(f"value = {version}\n", encoding="utf-8")
                    result = graph.compile_project(project)
                    self.assertIsNotNone(result.revision)

                revisions = graph.project_history(project, limit=30)
                self.assertEqual(len(revisions), 20)
                self.assertEqual([revision.sequence for revision in revisions], list(range(21, 1, -1)))

                no_op = graph.compile_project(project)

                self.assertIsNone(no_op.revision)
                self.assertIsNone(no_op.retention)
                self.assertEqual(len(graph.project_history(project, limit=30)), 20)
                self.assertEqual(graph.store.count_nodes(node_types={"CompilationRun"}), 21)
                self.assertEqual(graph.store.count_nodes(node_types={"GraphDelta"}), 21)
            finally:
                graph.close()


if __name__ == "__main__":
    unittest.main()
