"""Persist large administrative indexes with the existing compressed record codec."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reql import BlockGraphStore, MemoryEdge, MemoryNode


class CheckpointIndexTests(unittest.TestCase):
    """Protect lookup results, Unicode values and recoverability across checkpoints."""

    def test_compressed_indexes_preserve_wal_replay_and_both_open_modes(self) -> None:
        """Multi-page indexes retain exact records and lexical lookup after reopening."""
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.reql"
            with closing(BlockGraphStore(path, block_size=4096)) as store:
                with store.transaction():
                    for index in range(240):
                        store.upsert_node(MemoryNode(id=f"n:{index}", type="Function", text="invoice payment sharedword",
                            properties={"relative_path": f"src/invoice_{index}.py", "description": "caffè 東京", "index": index}))
                    store.upsert_edge(MemoryEdge(id="call", from_id="n:0", to_id="n:239", type="CALLS"))
                store.checkpoint_if_needed(wal_bytes_threshold=0)
                report = store.inspect_storage()
                self.assertGreater(report["compression"]["space_saved_ratio"], 0)
                store.update_node_fields("n:0", text="refund replacementword")
            for read_only in (True, False):
                with self.subTest(read_only=read_only), closing(BlockGraphStore(path, read_only=read_only)) as reopened:
                    self.assertEqual(reopened.count_nodes(), 240)
                    self.assertEqual(reopened.get_node("n:239").properties["description"], "caffè 東京")
                    self.assertEqual([n.id for n, _score in reopened.lexical_search("replacementword")], ["n:0"])
                    self.assertNotIn("n:0", {n.id for n, _score in reopened.lexical_search("sharedword", top_k=300)})
                    self.assertEqual(reopened.get_edge("call").to_id, "n:239")

    def test_failed_checkpoint_keeps_previous_file_and_durable_wal(self) -> None:
        """A streamed-write failure leaves the preceding checkpoint recoverable."""
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.reql"
            with closing(BlockGraphStore(path)) as store:
                store.upsert_node(MemoryNode(id="original", type="Function", text="originalword"))
                store.checkpoint_if_needed(wal_bytes_threshold=0)
                before = path.read_bytes()
                store.upsert_node(MemoryNode(id="pending", type="Function", text="pendingword"))
                with patch.object(store, "_write_data_pages", side_effect=OSError("disk full")):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        store.checkpoint_if_needed(wal_bytes_threshold=0)
                self.assertEqual(path.read_bytes(), before)
            with closing(BlockGraphStore(path, read_only=True)) as reopened:
                self.assertEqual({n.id for n in reopened.all_nodes()}, {"original", "pending"})
                self.assertEqual([n.id for n, _score in reopened.lexical_search("pendingword")], ["pending"])
