from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from mcp.tools import query_context as mcp_query_context
from memory.domain.models import MemoryNode
from memory.domain.query_context import (
    ContextResult,
    QueryContextRequest,
    QueryMode,
    RetrievalBudget,
)


class QueryContextContractTests(unittest.TestCase):
    def test_request_factory_normalizes_provider_values(self) -> None:
        request = QueryContextRequest.from_raw(
            text="  shared context  ",
            mode="CLEANUP",
            scopes=[" Code ", "test", "code"],
            top_k=7,
            max_depth=2,
            max_items=9,
            include_archived=True,
        )

        self.assertEqual(request.text, "  shared context  ")
        self.assertIs(request.mode, QueryMode.CLEANUP)
        self.assertEqual(request.scopes, frozenset({"code", "test"}))
        self.assertEqual(request.budget, RetrievalBudget(top_k=7, max_depth=2, max_items=9))
        self.assertTrue(request.include_archived)
        with self.assertRaises(FrozenInstanceError):
            request.include_archived = False  # type: ignore[misc]

    def test_request_factory_enforces_shared_limits_and_labels(self) -> None:
        invalid_cases = (
            {"text": ""},
            {"text": "x", "mode": "edit"},
            {"text": "x", "scopes": ["unknown"]},
            {"text": "x", "top_k": 0},
            {"text": "x", "top_k": 51},
            {"text": "x", "max_depth": 6},
            {"text": "x", "max_items": 51},
        )
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                QueryContextRequest.from_raw(**values)


class QueryContextServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=tmp_root)
        self.db = Path(self.tmp.name) / "memory.reql"
        self.graph = MemoryGraph.open(self.db)
        self.node = MemoryNode(
            id="function:typed-query-context",
            type="Function",
            label="typed_query_context",
            text="typed query context common service provider",
            canonical_key="function:typed-query-context",
            properties={
                "relative_path": "src/typed_context.py",
                "context_scope": "code",
                "qualified_name": "typed_context.typed_query_context",
                "line_start": 3,
                "line_end": 8,
            },
            salience=0.9,
        )
        self.graph.add_node(self.node)

    def tearDown(self) -> None:
        self.graph.close()
        self.tmp.cleanup()

    def test_service_retrieves_once_and_returns_versioned_result(self) -> None:
        request = QueryContextRequest.from_raw(
            text="typed query context common service",
            scopes=["code"],
            top_k=8,
        )
        with patch.object(self.graph.retrieval, "retrieve", wraps=self.graph.retrieval.retrieve) as retrieve:
            result = self.graph.query_context_result(request)

        self.assertIsInstance(result, ContextResult)
        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(result.schema_version, 1)
        self.assertEqual(len(result.graph_revision), 64)
        self.assertNotIn("confidence", result.payload)
        self.assertIn("trace_id", result.payload)
        self.assertIn("ranked_nodes", result.payload)
        self.assertIn("seed_node_ids", result.payload)
        envelope = result.to_dict()
        self.assertIn("payload", envelope)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["graph_revision"], result.graph_revision)
        self.assertEqual(envelope["confidence"]["status"], result.confidence.status)

    def test_graph_revision_is_stable_and_changes_with_relevant_state(self) -> None:
        request = QueryContextRequest.from_raw(
            text="typed query context common service",
            scopes=["code"],
            top_k=8,
        )

        first = self.graph.query_context_result(request)
        second = self.graph.query_context_result(request)
        self.assertEqual(first.graph_revision, second.graph_revision)

        self.graph.store.update_node_fields(
            self.node.id,
            text="typed query context common service provider changed",
        )
        changed = self.graph.query_context_result(request)
        self.assertNotEqual(first.graph_revision, changed.graph_revision)

    def test_python_and_mcp_adapters_serialize_the_same_result(self) -> None:
        self.graph.close()
        api_graph = MemoryGraph.open(self.db, read_only=True)
        try:
            api_payload = api_graph.query_context_payload(
                "typed query context common service",
                scopes=["code"],
                top_k=8,
                max_depth=3,
                max_items=8,
            )
        finally:
            api_graph.close()

        mcp_payload = mcp_query_context(
            storage_path=str(self.db),
            query="typed query context common service",
            scopes=["code"],
            top_k=8,
            max_depth=3,
            max_items=8,
        )
        api_payload["payload"].pop("trace_id", None)
        mcp_payload["payload"].pop("trace_id", None)
        self.assertEqual(api_payload, mcp_payload)

        self.graph = MemoryGraph.open(self.db)
