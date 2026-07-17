from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.domain.exceptions import StorageError
from memory.domain.models import MemoryEdge, MemoryNode
from memory.storage import BlockGraphStore
from memory.storage.adapters import block_store as block_store_module

_SUPERBLOCK_HEADER_SIZE = struct.calcsize("<8sIIII32s")


class BlockStorageTests(unittest.TestCase):
    def test_graph_queries_accept_none_for_unbounded_results(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                nodes = [
                    MemoryNode(
                        id=f"node:{index}",
                        type="Function",
                        label=f"shared term {index}",
                        text="shared lexical term",
                        properties={"project_id": "project"},
                    )
                    for index in range(4)
                ]
                store.batch_upsert_nodes(nodes)
                store.batch_upsert_edges(
                    [
                        MemoryEdge(
                            id=f"edge:{index}",
                            from_id="node:0",
                            to_id=f"node:{index}",
                            type="CALLS",
                        )
                        for index in range(1, 4)
                    ]
                )

                self.assertEqual(
                    len(store.find_nodes_by_property("project_id", "project", limit=None)),
                    4,
                )
                self.assertEqual(len(store.get_edges(type_="CALLS", limit=None)), 3)
                self.assertEqual(
                    len(store.incident_edges(["node:0"], edge_types={"CALLS"}, limit=None)),
                    3,
                )
                self.assertEqual(
                    len(
                        store.top_nodes_by_degree(
                            limit=None,
                            node_types={"Function"},
                            project_id="project",
                            include_global_project=False,
                        )
                    ),
                    4,
                )
                self.assertEqual(
                    len(store.lexical_search("shared lexical term", top_k=None)),
                    4,
                )
            finally:
                store.close()

    def test_block_store_persists_with_reql_block_header(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            graph = MemoryGraph.open(path)
            try:
                graph.add_node(MemoryNode(id="function:block", type="Function", label="block_storage", text="def block_storage(): ..."))
                node_count = len(graph.export_json()["nodes"])
            finally:
                graph.close()

            self.assertGreater(node_count, 0)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes()[:8], b"RQLSPB01")

            reopened = MemoryGraph.open(path)
            try:
                self.assertEqual(len(reopened.export_json()["nodes"]), node_count)
                self.assertEqual(reopened.store.schema_version(), 2)
                manifest = reopened.store.storage_manifest()
                self.assertEqual(manifest["schema_version"], 2)
                self.assertEqual(manifest["block_size"], reopened.store.block_size)
                self.assertEqual(manifest["data_offset"], reopened.store.block_size)
                self.assertGreaterEqual(manifest["root_index_offset"], reopened.store.block_size)
                self.assertEqual(manifest["record_codec"], "binary-v2")
                self.assertGreaterEqual(reopened.store.root_index_offset(), reopened.store.block_size)
                self.assertGreaterEqual(reopened.store.generation_id(), 1)
            finally:
                reopened.close()

            payload = path.read_bytes()
            self.assertIn(b"RQLREC02", payload)

            inspector = BlockGraphStore(path, read_only=True)
            try:
                details = inspector.inspect_storage()
                self.assertGreater(details["index_stats"]["nodes"], 0)
                self.assertGreater(details["wal"]["frames"], 0)
                self.assertIn("blocks", details["space_map"])
            finally:
                inspector.close()

    def test_lexical_search_prefers_free_phrase_order_over_scattered_terms(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                phrase = MemoryNode(
                    id="function:phrase",
                    type="Function",
                    label="apply_compile_transaction_speed",
                    text="apply compile transaction speed",
                    canonical_key="src.compiler.apply_compile_transaction_speed",
                    salience=0.01,
                )
                scattered = MemoryNode(
                    id="function:scattered",
                    type="Function",
                    label="transaction helper",
                    text="compile helper applies unrelated cache then transaction and later speed",
                    canonical_key="src.compiler.transaction_helper",
                    salience=0.99,
                )
                store.batch_upsert_nodes([phrase, scattered])

                results = store.lexical_search("apply compile transaction speed", top_k=2, node_types={"Function"})

                self.assertEqual(results[0][0].id, phrase.id)
            finally:
                store.close()

    def test_lexical_terms_update_after_lazy_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.upsert_node(
                    MemoryNode(
                        id="n1",
                        type="Topic",
                        label="original marker",
                        text="original-only-token",
                        canonical_key="topic:n1",
                    )
                )
                store.compact_storage()
            finally:
                store.close()

            reopened = BlockGraphStore(path)
            try:
                self.assertEqual(reopened.lexical_search("original-only-token", top_k=1)[0][0].id, "n1")
                reopened.update_node_fields("n1", label="replacement marker", text="replacement-only-token")
                self.assertFalse(reopened.lexical_search("original-only-token", top_k=1))
                self.assertEqual(reopened.lexical_search("replacement-only-token", top_k=1)[0][0].id, "n1")
            finally:
                reopened.close()

    def test_append_only_wal_reopens_without_manual_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(id="artifact", type="SourceArtifact", label="app.py", canonical_key="app.py", properties={"artifact_type": "code", "relative_path": "app.py"}),
                        MemoryNode(id="function", type="Function", label="compile project", canonical_key="compile_project", properties={"name": "compile_project", "relative_path": "app.py"}),
                    ]
                )
                store.batch_upsert_edges([MemoryEdge(id="defines", from_id="artifact", to_id="function", type="DEFINES", properties={"project_id": "p1"})])
            finally:
                store.close()

            self.assertTrue(path.exists())
            self.assertTrue(path.with_name(f"{path.name}.wal").exists())

            reopened = BlockGraphStore(path)
            try:
                self.assertEqual(reopened.get_node_by_key("Function", "compile_project").id, "function")
                self.assertEqual(reopened.find_nodes_by_property("artifact_type", "code", type_="SourceArtifact")[0].id, "artifact")
                self.assertEqual(reopened.neighbors("artifact", edge_types={"DEFINES"})[0][1].id, "function")
            finally:
                reopened.close()

    def test_unsupported_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
            finally:
                store.close()

            payload = bytearray(path.read_bytes())
            payload[12:16] = struct.pack("<I", 1)
            path.write_bytes(bytes(payload))

            with self.assertRaises(StorageError) as ctx:
                BlockGraphStore(path)

        self.assertIn("Unsupported REQL schema version 1", str(ctx.exception))

    def test_large_single_record_spans_blocks_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            large_text = "".join(hashlib.sha256(f"line-{i}".encode("ascii")).hexdigest() for i in range(4000))
            store = BlockGraphStore(path, block_size=4096)
            try:
                store.upsert_node(
                    MemoryNode(
                        id="large",

                        type="SourceFragment",
                        label="large",
                        text=large_text,
                        canonical_key="large",
                        properties={"payload_hash": hashlib.sha256(large_text.encode("utf-8")).hexdigest()},
                    )
                )
                store.compact_storage()
            finally:
                store.close()

            reopened = BlockGraphStore(path, block_size=4096)
            try:
                node = reopened.get_node("large")
                self.assertIsNotNone(node)
                assert node is not None
                self.assertEqual(node.text, large_text)
                details = reopened.inspect_storage()
                self.assertEqual(details["records"]["by_kind"]["node"], 1)
            finally:
                reopened.close()

    def test_manifest_checksum_is_validated_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
            finally:
                store.close()

            payload = bytearray(path.read_bytes())
            payload[_SUPERBLOCK_HEADER_SIZE] ^= 1
            path.write_bytes(bytes(payload))

            with self.assertRaises(StorageError):
                BlockGraphStore(path)

    def test_writer_lock_rejects_second_writer_and_readers_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            first = BlockGraphStore(path)
            try:
                lock_path = path.with_name(f"{path.name}.lock")
                self.assertTrue(lock_path.exists())
                with self.assertRaises(StorageError) as locked:
                    BlockGraphStore(path, lock_timeout_seconds=0.0)
                message = str(locked.exception)
                self.assertIn("command=", message)
                self.assertIn("duration=", message)
                self.assertIn("process_alive=true", message)
                self.assertIn("watcher=false", message)
                self.assertIn("--snapshot", message)
                with self.assertRaises(StorageError):
                    BlockGraphStore(path, read_only=True, lock_timeout_seconds=0.0)
            finally:
                first.close()

            self.assertFalse(path.with_name(f"{path.name}.lock").exists())
            second = BlockGraphStore(path)
            try:
                self.assertEqual(second.schema_version(), 2)
            finally:
                second.close()

    def test_stale_lock_recovery_is_safe_for_dead_and_incomplete_owners(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            lock_path = path.with_name(f"{path.name}.lock")
            lock_path.write_text(
                json.dumps(
                    {
                        "format": "reql-store-lock-v2",
                        "path": str(path),
                        "pid": 2147483647,
                        "host": block_store_module.socket.gethostname(),
                        "token": "dead-owner",
                        "created_at": block_store_module.utcnow_iso(),
                        "command": "reql project compile .",
                        "watcher": False,
                    }
                ),
                encoding="utf-8",
            )

            recovered = block_store_module.inspect_store_locks(path, recover_stale=True)
            self.assertFalse(lock_path.exists())
            self.assertEqual(len(recovered["recovered"]), 1)
            self.assertFalse(recovered["recovered"][0]["process_alive"])

            lock_path.write_text("", encoding="utf-8")
            recent = block_store_module.inspect_store_locks(path, recover_stale=True)
            self.assertTrue(lock_path.exists())
            self.assertFalse(recent["writer"]["stale"])

            old = time.time() - block_store_module.DEFAULT_INCOMPLETE_LOCK_STALE_SECONDS - 1
            os.utime(lock_path, (old, old))
            expired = block_store_module.inspect_store_locks(path, recover_stale=True)
            self.assertFalse(lock_path.exists())
            self.assertEqual(len(expired["recovered"]), 1)

    def test_readers_share_lock_and_writer_waits_for_readers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            store.close()

            first_reader = BlockGraphStore(path, read_only=True)
            second_reader = BlockGraphStore(path, read_only=True)
            readers_path = path.with_name(f"{path.name}.readers")
            self.assertTrue(readers_path.exists())
            self.assertGreaterEqual(len(list(readers_path.glob("*.lock"))), 2)

            def release_readers() -> None:
                time.sleep(0.2)
                first_reader.close()
                second_reader.close()

            releaser = threading.Thread(target=release_readers)
            releaser.start()
            started = time.monotonic()
            writer = BlockGraphStore(path, lock_timeout_seconds=2.0)
            elapsed = time.monotonic() - started
            try:
                self.assertGreaterEqual(elapsed, 0.15)
                self.assertEqual(writer.schema_version(), 2)
            finally:
                writer.close()
                releaser.join(timeout=2.0)
            self.assertFalse(readers_path.exists())

    def test_memory_graph_read_only_open_requires_existing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"

            with self.assertRaises(StorageError):
                MemoryGraph.open(path, read_only=True)

            path.write_bytes(b"")
            with self.assertRaises(StorageError):
                MemoryGraph.open(path, read_only=True)

    def test_read_only_store_rejects_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            store.close()

            reader = BlockGraphStore(path, read_only=True)
            try:
                with self.assertRaises(StorageError):
                    reader.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
            finally:
                reader.close()

    def test_wal_replays_updates_without_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
            wal_path = path.with_name(f"{path.name}.wal")
            self.assertTrue(wal_path.exists())
            store._release_lock()
            store._closed = True

            recovered = BlockGraphStore(path)
            try:
                self.assertIsNotNone(recovered.get_node("n1"))
            finally:
                recovered.close()

            self.assertTrue(path.exists())
            self.assertTrue(wal_path.exists())

    def test_checkpoint_if_needed_compacts_large_wal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
                wal_path = path.with_name(f"{path.name}.wal")
                self.assertTrue(wal_path.exists())

                result = store.checkpoint_if_needed(wal_bytes_threshold=1)

                self.assertTrue(result["checkpointed"])
                self.assertEqual(result["reason"], "wal_threshold")
                self.assertTrue(path.exists())
                self.assertFalse(wal_path.exists())
                self.assertGreater(result["generation_id_after"], result["generation_id_before"])
            finally:
                store.close()

            reopened = BlockGraphStore(path)
            try:
                self.assertEqual(reopened.get_node_by_key("Topic", "topic:one").id, "n1")
            finally:
                reopened.close()

    def test_transaction_rollback_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
                        raise RuntimeError("fail")
                self.assertIsNone(store.get_node("n1"))
            finally:
                store.close()

    def test_transaction_rollback_restores_existing_node_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                store.upsert_node(MemoryNode(id="n1", type="Topic", label="before", canonical_key="n1"))
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.update_node_fields("n1", label="after", properties={"marker": "changed"})
                        raise RuntimeError("fail")
                node = store.get_node("n1")
                self.assertIsNotNone(node)
                assert node is not None
                self.assertEqual(node.label, "before")
                self.assertNotIn("marker", node.properties)
            finally:
                store.close()

    def test_transaction_rollback_restores_indexes_adjacency_and_lexical_search(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(id="a", type="Topic", label="alpha", text="original searchable text", canonical_key="topic:a", properties={"project_id": "p1", "name": "alpha"}),
                        MemoryNode(id="b", type="Topic", label="beta", canonical_key="topic:b", properties={"project_id": "p1", "name": "beta"}),
                    ]
                )
                store.upsert_edge(MemoryEdge(id="e1", from_id="a", to_id="b", type="RELATED_TO", properties={"project_id": "p1"}))

                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.update_node_fields("a", label="changed", text="mutated-only-token", properties={"project_id": "p2", "name": "changed"})
                        store.update_edge_fields("e1", properties={"project_id": "p2"})
                        store.upsert_node(MemoryNode(id="c", type="Topic", label="created", text="created-only-token", canonical_key="topic:c", properties={"project_id": "p2", "name": "created"}))
                        store.upsert_edge(MemoryEdge(id="e2", from_id="a", to_id="c", type="RELATED_TO", properties={"project_id": "p2"}))
                        raise RuntimeError("rollback")

                node = store.get_node("a")
                edge = store.get_edge("e1")
                self.assertIsNotNone(node)
                self.assertIsNotNone(edge)
                assert node is not None
                assert edge is not None
                self.assertEqual(node.label, "alpha")
                self.assertEqual(node.properties["project_id"], "p1")
                self.assertEqual(edge.properties["project_id"], "p1")
                self.assertIsNone(store.get_node("c"))
                self.assertIsNone(store.get_edge("e2"))
                self.assertEqual({item.id for item in store.find_nodes_by_property("project_id", "p1")}, {"a", "b"})
                self.assertEqual(store.find_nodes_by_property("project_id", "p2"), [])
                self.assertEqual({neighbor.id for _, neighbor in store.neighbors("a", direction="out")}, {"b"})
                self.assertTrue(store.lexical_search("original searchable", top_k=1))
                self.assertFalse(store.lexical_search("mutated-only-token", top_k=1))
                self.assertFalse(store.lexical_search("created-only-token", top_k=1))
            finally:
                store.close()

    def test_batch_upsert_is_idempotent_inside_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                nodes = [
                    MemoryNode(id="n1", type="Topic", canonical_key="topic:one"),
                    MemoryNode(id="n2", type="Topic", canonical_key="topic:two"),
                ]
                with store.transaction():
                    first = store.batch_upsert_nodes(nodes)
                    second = store.batch_upsert_nodes(nodes)
                self.assertEqual([created for _, created in first], [True, True])
                self.assertEqual([created for _, created in second], [False, False])
                self.assertEqual(store.count_nodes(node_types={"Topic"}), 2)
            finally:
                store.close()

    def test_indexed_counts_and_bounded_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(id="active-low", type="Topic", status="active", salience=0.1),
                        MemoryNode(id="active-high", type="Topic", status="active", salience=0.9),
                        MemoryNode(id="archived", type="Topic", status="archived", salience=1.0),
                        MemoryNode(id="other", type="Entity", status="active", salience=0.8),
                    ]
                )
                store.batch_upsert_edges(
                    [MemoryEdge(id="related", from_id="active-low", to_id="active-high", type="RELATED_TO")]
                )

                self.assertEqual(store.count_nodes(node_types={"Topic"}, statuses={"active"}), 2)
                self.assertEqual(store.count_edges(edge_types={"RELATED_TO"}), 1)
                top = store.find_nodes(type_="Topic", status="active", limit=1, order_by="salience")
                self.assertEqual([node.id for node in top], ["active-high"])
                internal = store.find_nodes(type_="Topic", status="active", limit=1, order_by="salience", clone=False)
                self.assertIs(internal[0], store.get_node("active-high", clone=False))
                self.assertIsNot(top[0], internal[0])
            finally:
                store.close()

    def test_dense_node_edges_are_kept_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql", dense_node_threshold=2)
            try:
                center = MemoryNode(id="center", type="Topic", canonical_key="center")
                leaves = [MemoryNode(id=f"leaf{i}", type="Entity", canonical_key=f"leaf{i}") for i in range(3)]
                store.batch_upsert_nodes([center, *leaves])
                store.batch_upsert_edges(
                    [
                        MemoryEdge(id=f"edge{i}", from_id="center", to_id=leaf.id, type="RELATED_TO")
                        for i, leaf in enumerate(leaves)
                    ]
                )
                self.assertEqual(store.degree("center"), 3)
                self.assertEqual(len(store.neighbors("center")), 3)
            finally:
                store.close()

            reopened = BlockGraphStore(Path(td) / "memory.reql", dense_node_threshold=2)
            try:
                self.assertEqual(reopened.degree("center"), 3)
                self.assertEqual(len(reopened.neighbors("center")), 3)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()

