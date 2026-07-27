from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory.domain.models import MemoryNode
from memory.storage import BlockGraphStore
from memory.storage.adapters.block_store import BlockGraphStore as LegacyBlockGraphStore


class LexicalIndexingTests(unittest.TestCase):
    def test_long_text_tail_term_is_indexed_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            marker = "ultra-tail-retrieval-marker"
            text = ("ordinary introductory material " * 260) + marker

            store = BlockGraphStore(path)
            try:
                store.upsert_node(
                    MemoryNode(
                        id="fragment:tail",
                        type="DocumentFragment",
                        label="long document page",
                        canonical_key="document:long-page",
                        text=text,
                    )
                )
                matches = store.lexical_search(marker, top_k=5)
                self.assertEqual(matches[0][0].id, "fragment:tail")
                store.compact_storage()
            finally:
                store.close()

            reopened = BlockGraphStore(path, read_only=True)
            try:
                matches = reopened.lexical_search(marker, top_k=5)
                self.assertEqual(matches[0][0].id, "fragment:tail")
            finally:
                reopened.close()

    def test_legacy_head_only_index_is_rebuilt_for_long_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.reql"
            marker = "legacy-tail-retrieval-marker"
            text = ("legacy prefatory material " * 280) + marker

            legacy = LegacyBlockGraphStore(path)
            try:
                legacy.upsert_node(
                    MemoryNode(
                        id="fragment:legacy-tail",
                        type="DocumentFragment",
                        label="legacy long page",
                        canonical_key="document:legacy-long-page",
                        text=text,
                    )
                )
                self.assertFalse(legacy.lexical_search(marker, top_k=5))
                legacy.compact_storage()
            finally:
                legacy.close()

            upgraded = BlockGraphStore(path, read_only=True)
            try:
                matches = upgraded.lexical_search(marker, top_k=5)
                self.assertEqual(matches[0][0].id, "fragment:legacy-tail")
            finally:
                upgraded.close()


if __name__ == "__main__":
    unittest.main()
