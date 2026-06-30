from __future__ import annotations

import hashlib
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
from memory.domain.exceptions import StorageError
from memory.domain.models import MemoryEdge, MemoryNode
from memory.storage import BlockGraphStore
from memory.storage.adapters import block_store as block_store_module

_SUPERBLOCK_HEADER_SIZE = struct.calcsize("<8sIIII32s")


class BlockStorageTests(unittest.TestCase):
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

    def test_v2_indexes_persist_and_records_load_lazily(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(id="n1", type="SourceArtifact", label="app.py", canonical_key="app.py", properties={"artifact_type": "code", "relative_path": "app.py"}),
                        MemoryNode(id="n2", type="Function", label="compile project", canonical_key="compile_project", properties={"name": "compile_project", "relative_path": "app.py"}),
                    ]
                )
                store.batch_upsert_edges([MemoryEdge(id="e1", from_id="n1", to_id="n2", type="DEFINES", properties={"project_id": "p1"})])
                store.compact_storage()
            finally:
                store.close()

            reopened = BlockGraphStore(path)
            try:
                details = reopened.inspect_storage()
                self.assertEqual(details["index_stats"]["nodes"], 2)
                self.assertEqual(details["index_stats"]["edges"], 1)
                self.assertEqual(details["index_stats"]["loaded_nodes"], 0)
                self.assertEqual(details["index_stats"]["loaded_edges"], 0)
                self.assertEqual(reopened.count_nodes(node_types={"Function"}), 1)
                self.assertEqual(reopened.count_edges(edge_types={"DEFINES"}), 1)
                self.assertEqual(reopened.find_nodes_by_property("artifact_type", "code", type_="SourceArtifact")[0].id, "n1")
                self.assertEqual(reopened.get_node_by_key("Function", "compile_project").id, "n2")
                self.assertEqual(reopened.neighbors("n1", edge_types={"DEFINES"})[0][1].id, "n2")
                self.assertEqual(reopened.lexical_search("compile project", top_k=2)[0][0].id, "n2")
            finally:
                reopened.close()

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

    def test_internal_no_clone_flags_do_not_change_default_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                node_results = store.batch_upsert_nodes(
                    [
                        MemoryNode(id="n1", type="Topic", label="alpha", properties={"project_id": "p1", "items": ["a"]}),
                        MemoryNode(id="n2", type="Topic", label="beta", properties={"project_id": "p1"}),
                    ]
                )
                edge_results = store.batch_upsert_edges(
                    [MemoryEdge(id="e1", from_id="n1", to_id="n2", type="RELATED_TO", properties={"project_id": "p1", "items": ["b"]})]
                )

                self.assertIsNot(node_results[0][0], store._nodes["n1"])
                self.assertIsNot(edge_results[0][0], store._edges["e1"])
                self.assertIsNot(store.get_node("n1"), store._nodes["n1"])
                self.assertIsNot(store.get_edge("e1"), store._edges["e1"])
                self.assertIsNot(store.get_edges(from_id="n1")[0], store._edges["e1"])
                self.assertIsNot(store.incident_edges(["n1"])[0], store._edges["e1"])
                self.assertIsNot(store.find_nodes_by_property("project_id", "p1")[0], store._nodes["n1"])
                self.assertIsNot(store.find_edges_by_property("project_id", "p1")[0], store._edges["e1"])

                self.assertIs(store.get_node("n1", clone=False), store._nodes["n1"])
                self.assertIs(store.get_edge("e1", clone=False), store._edges["e1"])
                self.assertIs(store.get_edges(from_id="n1", clone=False)[0], store._edges["e1"])
                self.assertIs(store.incident_edges(["n1"], clone=False)[0], store._edges["e1"])
                raw_node = store.find_nodes_by_property("project_id", "p1", clone=False)[0]
                raw_edge = store.find_edges_by_property("project_id", "p1", clone=False)[0]
                self.assertIs(raw_node, store._nodes[raw_node.id])
                self.assertIs(raw_edge, store._edges[raw_edge.id])

                raw_node_results = store.batch_upsert_nodes([MemoryNode(id="n3", type="Topic", label="gamma", properties={"project_id": "p2"})], return_clones=False)
                raw_edge_results = store.batch_upsert_edges(
                    [MemoryEdge(id="e2", from_id="n2", to_id="n3", type="RELATED_TO", properties={"project_id": "p2"})],
                    return_clones=False,
                )
                self.assertIs(raw_node_results[0][0], store._nodes["n3"])
                self.assertIs(raw_edge_results[0][0], store._edges["e2"])
            finally:
                store.close()

    def test_mmap_backed_lazy_reads_survive_checkpoint_replace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(id="n1", type="Topic", label="alpha", canonical_key="topic:alpha"),
                        MemoryNode(id="n2", type="Topic", label="beta", canonical_key="topic:beta"),
                    ]
                )
                store.batch_upsert_edges([MemoryEdge(id="e1", from_id="n1", to_id="n2", type="RELATED_TO")])
                store.compact_storage()
            finally:
                store.close()

            reopened = BlockGraphStore(path)
            try:
                self.assertIsNotNone(reopened._data_mmap)
                self.assertEqual(reopened.get_node("n1").label, "alpha")
                reopened.upsert_node(MemoryNode(id="n3", type="Topic", label="gamma", canonical_key="topic:gamma"))
                result = reopened.checkpoint_if_needed(wal_bytes_threshold=1)
                self.assertTrue(result["checkpointed"])
                self.assertIsNotNone(reopened._data_mmap)
                self.assertEqual(reopened.get_node("n3").label, "gamma")
            finally:
                reopened.close()

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

    def test_edge_upsert_after_lazy_reopen_loads_endpoints_by_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(id="left", type="Topic", canonical_key="left"),
                        MemoryNode(id="right", type="Topic", canonical_key="right"),
                    ]
                )
            finally:
                store.close()

            reopened = BlockGraphStore(path)
            try:
                edge, created = reopened.upsert_edge(MemoryEdge(id="edge", from_id="left", to_id="right", type="RELATED_TO"))
                self.assertTrue(created)
                self.assertEqual(edge.id, "edge")
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

    def test_root_index_stays_compact_for_many_records(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path, block_size=4096)
            try:
                nodes = [
                    MemoryNode(
                        id=f"n{i}",

                        type="Topic",
                        canonical_key=f"topic:{i}:with:a:long:stable:key",
                    )
                    for i in range(500)
                ]
                store.batch_upsert_nodes(nodes)
                store.batch_upsert_edges(
                    [
                        MemoryEdge(id=f"e{i}", from_id=f"n{i}", to_id=f"n{i + 1}", type="RELATED_TO")
                        for i in range(len(nodes) - 1)
                    ]
                )
                store.compact_storage()
            finally:
                store.close()

            reopened = BlockGraphStore(path, block_size=4096)
            try:
                details = reopened.inspect_storage()
                self.assertEqual(details["root_index"]["nodes"], 500)
                self.assertEqual(details["root_index"]["edges"], 499)
                self.assertEqual(details["root_index"]["node_keys"], 500)
                self.assertEqual(details["root_index"]["edge_patterns"], 499)
                self.assertIsInstance(details["space_map"]["blocks"], int)
                self.assertGreater(details["space_map"]["blocks"], 0)
            finally:
                reopened.close()

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

    def test_non_lexical_node_updates_skip_term_reindex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                store.upsert_node(
                    MemoryNode(
                        id="n1",
                        type="Topic",
                        label="alpha marker",
                        text="retired-only-token",
                        canonical_key="topic:alpha",
                        properties={"project_id": "p1", "name": "alpha", "debug": "before"},
                    )
                )
                before_terms = {term: dict(postings) for term, postings in store._node_terms.items()}

                with patch.object(block_store_module, "keyword_scores", side_effect=AssertionError("unexpected lexical reindex")):
                    store.update_node_fields(
                        "n1",
                        usage_count=3,
                        properties={"project_id": "p1", "name": "alpha", "debug": "after"},
                    )
                    store.update_node_fields("n1", status="archived")
                    store.update_node_fields("n1", status="active")

                self.assertEqual({term: dict(postings) for term, postings in store._node_terms.items()}, before_terms)
                self.assertEqual(store.lexical_search("retired-only-token", top_k=1)[0][0].id, "n1")

                with patch.object(block_store_module, "keyword_scores", wraps=block_store_module.keyword_scores) as scorer:
                    store.update_node_fields("n1", text="changed-only-token")
                self.assertGreaterEqual(scorer.call_count, 1)
                self.assertEqual(store.lexical_search("changed-only-token", top_k=1)[0][0].id, "n1")
                self.assertFalse(store.lexical_search("retired-only-token", top_k=1))
            finally:
                store.close()

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

    def test_data_checksum_is_validated_by_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
            finally:
                store.close()

            payload = bytearray(path.read_bytes())
            payload[-1] ^= 1
            path.write_bytes(bytes(payload))

            reader = BlockGraphStore(path)
            try:
                with self.assertRaises(StorageError):
                    reader.inspect_storage()
            finally:
                reader.close()

    def test_unsupported_single_block_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            path.write_bytes(b"RQLBLK01" + b"\x00" * (64 * 1024 - 8))

            with self.assertRaises(StorageError):
                BlockGraphStore(path)

    def test_writer_lock_rejects_second_writer_and_readers_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            first = BlockGraphStore(path)
            try:
                lock_path = path.with_name(f"{path.name}.lock")
                self.assertTrue(lock_path.exists())
                with self.assertRaises(StorageError):
                    BlockGraphStore(path, lock_timeout_seconds=0.0)
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

    def test_read_only_usage_events_are_written_to_sidecar_journal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
                store.compact_storage()
            finally:
                store.close()

            reader = BlockGraphStore(path, read_only=True)
            try:
                reader.record_usage_event("topic one", [{"id": "n1", "score": 0.8, "activation": 0.3}])
                usage = reader.usage_for_node("n1")
                self.assertEqual(usage["usage_count"], 1)
                self.assertTrue(path.with_name(f"{path.name}.usage.jsonl").exists())
            finally:
                reader.close()

            reopened = BlockGraphStore(path, read_only=True)
            try:
                self.assertEqual(reopened.usage_for_node("n1")["usage_count"], 1)
            finally:
                reopened.close()

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

    def test_checkpoint_if_needed_repairs_wal_only_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
            store._release_lock()
            store._closed = True
            path.unlink()

            recovered = BlockGraphStore(path)
            try:
                self.assertIsNotNone(recovered.get_node("n1"))
                result = recovered.checkpoint_if_needed(wal_bytes_threshold=1024 * 1024 * 1024)
                self.assertTrue(result["checkpointed"])
                self.assertEqual(result["reason"], "base_missing")
                self.assertTrue(path.exists())
                self.assertFalse(path.with_name(f"{path.name}.wal").exists())
            finally:
                recovered.close()

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

    def test_transaction_rollback_on_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            store = BlockGraphStore(path)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    with store.transaction():
                        store.upsert_node(MemoryNode(id="n1", type="Topic", canonical_key="topic:one"))
                        raise KeyboardInterrupt
                self.assertIsNone(store.get_node("n1"))
            finally:
                store.close()

            reopened = BlockGraphStore(path)
            try:
                self.assertIsNone(reopened.get_node("n1"))
            finally:
                reopened.close()

    def test_nested_transaction_rollback_preserves_outer_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                with store.transaction():
                    store.upsert_node(MemoryNode(id="outer", type="Topic", canonical_key="outer"))
                    with self.assertRaises(RuntimeError):
                        with store.transaction():
                            store.upsert_node(MemoryNode(id="inner", type="Topic", canonical_key="inner"))
                            raise RuntimeError("inner fail")
                self.assertIsNotNone(store.get_node("outer"))
                self.assertIsNone(store.get_node("inner"))
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

