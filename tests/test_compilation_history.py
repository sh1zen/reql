"""History selection must use immutable chronology rather than mutable metrics."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from api import MemoryGraph
from memory.artifacts.models import SourceArtifact
from memory.artifacts.revision import RevisionRepository
from memory.storage.retention import prune_project_data


class CompilationHistoryTests(unittest.TestCase):
    """Verify ordering after older graph records receive new usage metrics."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.graph = MemoryGraph.open(Path(self.temp.name) / "graph.reql")
        self.addCleanup(self.graph.close)

    def test_latest_revision_is_not_lost_after_older_nodes_are_used(self) -> None:
        """Candidate limits cannot discard the head before sorting by sequence."""
        revisions = RevisionRepository(self.graph.store)
        artifact = SourceArtifact(
            id="artifact", project_id="project", uri="file:///rules.py", path="rules.py",
            relative_path="rules.py", artifact_type="code", language="python", size_bytes=1,
            sha256="", mtime=0,
        )
        history = []
        with self.graph.store.transaction():
            for index in range(110):
                artifact.sha256 = str(index)
                history.append(revisions.record(project_id="project", run_id=str(index), artifacts=[artifact]))
            for revision in history[:-1]:
                self.graph.store.update_node_metrics(revision.id, usage_count=1)
        self.assertEqual(revisions.latest("project").id, history[-1].id)
        self.assertEqual([item.id for item in revisions.list(project_id="project", limit=3)],
                         [item.id for item in reversed(history[-3:])])
        artifact.sha256 = "next"
        following = revisions.record(project_id="project", run_id="next", artifacts=[artifact])
        self.assertEqual(following.parent_id, history[-1].id)
        self.assertEqual(following.sequence, 111)

    def test_retention_chooses_newest_sequences_before_limiting(self) -> None:
        """Usage timestamps must not change which revision defines the retention boundary."""
        revisions = RevisionRepository(self.graph.store)
        artifact = SourceArtifact(
            id="artifact", project_id="project", uri="file:///rules.py", path="rules.py",
            relative_path="rules.py", artifact_type="code", language="python", size_bytes=1,
            sha256="", mtime=0,
        )
        history = []
        for index in range(5):
            artifact.sha256 = str(index)
            revision = revisions.record(project_id="project", run_id=str(index), artifacts=[artifact])
            node = self.graph.store.get_node(revision.id)
            # Store chronology explicitly so assertions do not depend on clock resolution.
            stamp = f"2026-01-{index + 1:02d}T00:00:00+00:00"
            node.properties["created_at"] = stamp
            self.graph.store.update_node_fields(node.id, properties=node.properties)
            history.append(revision)
        for revision in history[:3]:
            self.graph.store.update_node_metrics(revision.id, usage_count=1)
        result = prune_project_data(self.graph.store, project_id="project", retention_commits=2)
        self.assertEqual(result.cutoff, "2026-01-04T00:00:00+00:00")
        self.assertIsNotNone(revisions.get(history[-1].id))
        self.assertIsNotNone(revisions.get(history[-2].id))
        self.assertEqual(len(revisions.list(project_id="project")), 2)


if __name__ == "__main__":
    unittest.main()
