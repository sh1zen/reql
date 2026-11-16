"""Exercise the canonical block store's loading, format validation, and journals."""
from __future__ import annotations

import random
import string
import unittest
from typing import Any
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.storage import BlockGraphStore
from memory.storage.adapters.block_store import BlockGraphStore as AdapterStore
from memory.storage.adapters import BlockGraphStore as AdapterExport
from memory.storage.adapters.lexical_index import LEXICAL_INDEX_SCHEMA_VERSION
from memory.domain.exceptions import StorageError
from reql import BlockGraphStore as PublicStore, MemoryEdge, MemoryNode


class StorageTransactionTests(unittest.TestCase):
    """Protect persisted reads, rollback and checkpoint validation."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "graph.reql"

    def open_store(self, *, read_only: bool = False, defer_lexical_index: bool = False,
                   block_size: int = 64 * 1024) -> BlockGraphStore:
        """Open a test store and release its lock even when an assertion fails."""
        store = BlockGraphStore(self.path, read_only=read_only,
                                defer_lexical_index=defer_lexical_index, block_size=block_size)
        self.addCleanup(store.close)
        return store

    def test_supported_imports_share_the_canonical_store(self) -> None:
        for imported in (AdapterStore, AdapterExport, PublicStore):
            self.assertIs(imported, BlockGraphStore)

    def test_manifest_requires_explicit_algorithm_and_integer_metadata(self) -> None:
        """Current checkpoints reject omitted fields and coercible scalar metadata."""
        for field, convert in (("checksum_algorithm", None), ("data_offset", str),
                               ("generation_id", bool), ("data_block_count", float)):
            self.path = Path(self.directory.name) / f"invalid-{field}.reql"
            store = self.open_store()
            store.upsert_node(MemoryNode(id="owner", type="Function", text="persistedtoken"))
            build_manifest = store._build_manifest

            def invalid_manifest(**fields: Any) -> dict[str, Any]:
                """Write malformed metadata through the real checkpoint encoder."""
                manifest = build_manifest(**fields)
                if convert is None:
                    del manifest[field]
                else:
                    manifest[field] = convert(manifest[field])
                return manifest

            with patch.object(store, "_build_manifest", side_effect=invalid_manifest):
                store.checkpoint_if_needed(wal_bytes_threshold=0)
            store.close()
            checkpoint = self.path.read_bytes()
            for read_only in (False, True):
                with self.subTest(field=field, read_only=read_only):
                    with self.assertRaisesRegex(StorageError, f"manifest.*{field}"):
                        reopened = self.open_store(read_only=read_only)
                        reopened.close()
                    self.assertEqual(self.path.read_bytes(), checkpoint)
                    self.assertFalse(self.path.with_name(self.path.name + ".wal").exists())
                    self.assertFalse(self.path.with_name(self.path.name + ".lock").exists())

    def test_nested_journal_rollback_restores_records_indexes_and_deferred_terms(self) -> None:
        store = self.open_store()
        store.upsert_node(MemoryNode(id="owner", type="Function", text="originalalpha",
                                     properties={"relative_path": "original.py"}))
        store.upsert_node(MemoryNode(id="target", type="Function", text="targetbeta"))
        store.upsert_edge(MemoryEdge(id="call", from_id="owner", to_id="target", type="CALLS"))
        store.checkpoint_if_needed(wal_bytes_threshold=0)
        store.close()
        store = self.open_store(defer_lexical_index=True)
        original_node = store.get_node("owner")
        original_edge = store.get_edge("call")

        with self.assertRaisesRegex(RuntimeError, "outer abort"):
            with store.transaction():
                store.update_node_fields("owner", text="outergamma", properties={"relative_path": "outer.py"})
                with self.assertRaisesRegex(ValueError, "inner abort"):
                    with store.transaction():
                        store.remove_node("target")
                        store.remove_edge("call")
                        store.upsert_node(MemoryNode(id="temporary", type="Function", text="temporarydelta"))
                        raise ValueError("inner abort")
                self.assertEqual(store.get_node("owner").text, "outergamma")
                self.assertIsNotNone(store.get_node("target"))
                self.assertEqual(store.get_edge("call"), original_edge)
                self.assertIsNone(store.get_node("temporary"))
                # Loading deferred postings inside the transaction must also roll back.
                self.assertEqual([node.id for node, _ in store.lexical_search("outergamma")], ["owner"])
                with store.transaction():
                    store.upsert_node(MemoryNode(id="nested", type="Function", text="nestedcommit"))
                raise RuntimeError("outer abort")

        self.assertEqual(store.get_node("owner"), original_node)
        self.assertEqual(store.get_edge("call"), original_edge)
        self.assertIsNotNone(store.get_node("target"))
        self.assertIsNone(store.get_node("nested"))
        self.assertEqual([node.id for node in store.find_nodes_by_property("relative_path", "original.py")],
                         ["owner"])
        self.assertEqual(store.find_nodes_by_property("relative_path", "outer.py"), [])
        self.assertEqual([node.id for node, _ in store.lexical_search("originalalpha")], ["owner"])
        self.assertEqual(store.lexical_search("outergamma"), [])
        store.close()
        reopened = self.open_store(read_only=True)
        self.assertEqual(reopened.get_node("owner"), original_node)
        self.assertEqual(reopened.get_edge("call"), original_edge)
        self.assertIsNone(reopened.get_node("nested"))

    def test_lazy_checkpoint_loading_and_wal_replay_preserve_large_records(self) -> None:
        rng = random.Random(319)
        text = "".join(rng.choices(string.ascii_letters + string.digits, k=100_000))
        store = self.open_store(block_size=4096)
        store.upsert_node(MemoryNode(id="large", type="SourceFragment", text=text,
                                     properties={"relative_path": "large.txt"}))
        store.upsert_node(MemoryNode(id="tail", type="Function", text="persistedtail"))
        store.upsert_edge(MemoryEdge(id="evidence", from_id="tail", to_id="large", type="EVIDENCED_BY"))
        store.checkpoint_if_needed(wal_bytes_threshold=0)
        store.update_node_fields("tail", text="replayedtail")
        store.close()

        reader = self.open_store(read_only=True, defer_lexical_index=True)
        self.assertEqual(reader.get_node("large").text, text)
        self.assertEqual(reader.get_node("tail").text, "replayedtail")
        self.assertEqual(reader.get_edge("evidence").to_id, "large")
        self.assertEqual([node.id for node, _ in reader.lexical_search("replayedtail")], ["tail"])
        self.assertEqual(reader.lexical_search("persistedtail"), [])

    def test_unsupported_postings_are_rejected_without_rewriting_or_leaking_locks(self) -> None:
        """Every open mode rejects malformed and future lexical metadata."""
        for index, version in enumerate((LEXICAL_INDEX_SCHEMA_VERSION + 1, "2", True)):
            self.path = Path(self.directory.name) / f"unsupported-{index}.reql"
            store = self.open_store()
            store.upsert_node(MemoryNode(id="owner", type="Function", text="persistedtoken"))
            build_root = store._build_root_index

            def unsupported_root(*, locations: dict[str, dict[str, dict[str, int]]],
                                 space_map: dict[str, Any], generation_id: int) -> dict[str, Any]:
                """Encode unsupported metadata at the checkpoint persistence boundary."""
                root = build_root(locations=locations, space_map=space_map, generation_id=generation_id)
                root["lexical_schema_version"] = version
                return root

            with patch.object(store, "_build_root_index", side_effect=unsupported_root):
                store.checkpoint_if_needed(wal_bytes_threshold=0)
            store.close()
            checkpoint = self.path.read_bytes()
            for read_only in (False, True):
                for deferred in (False, True):
                    with self.subTest(version=version, read_only=read_only, deferred=deferred):
                        with self.assertRaisesRegex(StorageError, "Unsupported REQL lexical index.*Rebuild"):
                            self.open_store(read_only=read_only, defer_lexical_index=deferred)
                        self.assertEqual(self.path.read_bytes(), checkpoint)
                        self.assertFalse(self.path.with_name(self.path.name + ".wal").exists())
                        self.assertFalse(self.path.with_name(self.path.name + ".lock").exists())


if __name__ == "__main__":
    unittest.main()
