"""Checkpointed lexical postings must reflect deferred edits and deletions."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from memory.domain.models import MemoryNode
from memory.storage import BlockGraphStore


class LargeProjectPerformanceTests(unittest.TestCase):
    """Exercise the index lifecycle with a small deterministic fixture."""

    def setUp(self) -> None:
        scratch = Path.cwd() / ".tmp" / "regression-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

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

if __name__ == "__main__":
    unittest.main()
