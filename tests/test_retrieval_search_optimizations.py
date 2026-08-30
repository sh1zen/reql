from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.domain.models import MemoryNode, MemoryQuery
from memory.services.retrieval.common import (
    _canonical_token_overlap,
    _token_variants,
)
from memory.services.retrieval import RetrievalEngine
from memory.storage import BlockGraphStore


_REFERENCE_TOKEN_RE = re.compile(r"[a-z0-9_][a-z0-9_'-]{1,}")


def _reference_overlap(value: str, query_tokens: set[str]) -> set[str]:
    overlap: set[str] = set()
    for match in _REFERENCE_TOKEN_RE.finditer(value):
        token = match.group(0).strip("_-")
        if len(token) < 2:
            continue
        overlap.update(variant for variant in _token_variants(token) if variant in query_tokens)
    return overlap


class RetrievalSearchOptimizationTests(unittest.TestCase):
    def test_query_driven_overlap_preserves_token_and_plural_matching(self) -> None:
        values = (
            "query queries category categories class classes",
            "_node_ node-id cases uses processes",
            "alpha beta gamma delta",
            "--prefixed suffixed-- one x",
        )
        queries = (
            {"query", "category", "class"},
            {"node", "node-id", "cas", "us", "process"},
            {"alpha", "missing", "delta"},
            {"prefixed", "suffixed", "one"},
        )
        for value in values:
            for query_tokens in queries:
                with self.subTest(value=value, query_tokens=query_tokens):
                    self.assertEqual(
                        _canonical_token_overlap(value, query_tokens),
                        _reference_overlap(value, query_tokens),
                    )

    def test_multi_type_lookup_matches_the_established_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                nodes = [
                    MemoryNode(id="function:b", type="Function", label="b", created_at="2025-01-01T00:00:00+00:00"),
                    MemoryNode(id="class:a", type="Class", label="a", created_at="2025-01-01T00:00:00+00:00"),
                    MemoryNode(id="topic:ignored", type="Topic", label="ignored", created_at="2024-01-01T00:00:00+00:00"),
                    MemoryNode(id="function:a", type="Function", label="a", created_at="2024-01-01T00:00:00+00:00"),
                ]
                store.batch_upsert_nodes(nodes)

                results = store.find_nodes_by_types(["Function", "Class"], clone=False)

                self.assertEqual([node.id for node in results], ["function:a", "class:a", "function:b"])
            finally:
                store.close()

    def test_bounded_lexical_search_clones_only_selected_results(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(id=f"function:{index}", type="Function", label="shared search token")
                        for index in range(8)
                    ]
                )
                with patch.object(store, "_clone_node", wraps=store._clone_node) as clone_node:
                    results = store.lexical_search("shared search token", top_k=2, node_types={"Function"})

                self.assertEqual(len(results), 2)
                self.assertEqual(clone_node.call_count, 2)
                self.assertEqual(
                    len(store.lexical_search("shared search token", top_k=None, node_types=set())),
                    8,
                )
            finally:
                store.close()

    def test_bounded_lexical_search_materializes_only_a_candidate_pool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(id=f"function:{index}", type="Function", label="shared bounded candidate")
                        for index in range(200)
                    ]
                )
                with patch.object(store, "_load_node_from_location", wraps=store._load_node_from_location) as load_node:
                    results = store.lexical_search("shared bounded candidate", top_k=2, node_types={"Function"})

                self.assertEqual(len(results), 2)
                self.assertLess(load_node.call_count, 50)
            finally:
                store.close()

    def test_scoped_search_filters_indexed_candidates_without_type_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(
                            id="function:code",
                            type="Function",
                            label="compileProjectCache",
                            properties={"context_scope": "code", "relative_path": "src/cache.py"},
                        ),
                        MemoryNode(
                            id="function:test",
                            type="Function",
                            label="compileProjectCacheTest",
                            properties={"context_scope": "test", "relative_path": "tests/test_cache.py"},
                        ),
                    ]
                )
                engine = RetrievalEngine(store)
                query = MemoryQuery(text="compile project cache", context_scopes={"code"})
                with patch.object(engine, "_nodes_for_types", side_effect=AssertionError("unexpected full scan")):
                    results = engine._scoped_lexical_search(
                        query,
                        engine._query_profile(query.text),
                        lexical_node_types=("Function",),
                        scopes={"code"},
                        top_k=10,
                    )

                self.assertEqual([node.id for node, _score in results], ["function:code"])
            finally:
                store.close()

    def test_identifier_phrase_breaks_saturated_score_ties(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlockGraphStore(Path(td) / "memory.reql")
            try:
                store.batch_upsert_nodes(
                    [
                        MemoryNode(
                            id="function:identifier",
                            type="Function",
                            label="loadProjectCache",
                            text="small function",
                            salience=0.01,
                        ),
                        MemoryNode(
                            id="function:body",
                            type="Function",
                            label="generic helper",
                            text="load project cache appears in generic prose",
                            salience=0.99,
                        ),
                    ]
                )

                ranked = RetrievalEngine(store).retrieve(
                    MemoryQuery(text="load project cache", top_k=2, max_depth=0)
                ).ranked_nodes

                self.assertEqual(ranked[0].node.id, "function:identifier")
                self.assertGreater(ranked[0].reasons["match_score"], ranked[1].reasons["match_score"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
