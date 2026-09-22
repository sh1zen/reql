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
from tests.config_helpers import open_graph_with_documents


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
        self.assertEqual(result.schema_version, 2)
        self.assertEqual(len(result.graph_revision), 64)
        self.assertNotIn("confidence", result.payload)
        self.assertIn("trace_id", result.payload)
        self.assertIn("ranked_nodes", result.payload)
        self.assertIn("seed_node_ids", result.payload)
        envelope = result.to_dict()
        self.assertIn("payload", envelope)
        self.assertEqual(envelope["schema_version"], 2)
        self.assertEqual(envelope["freshness"]["status"], "unknown")
        self.assertIn("source_revision", envelope)
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

    def test_committed_project_revision_is_current_without_a_watcher(self) -> None:
        project = Path(self.tmp.name) / "fresh-project"
        project.mkdir()
        (project / "fresh.py").write_text("def fresh_context():\n    return 'current'\n", encoding="utf-8")
        self.graph.compile_project(project)

        result = self.graph.query_context_result(
            QueryContextRequest.from_raw(text="fresh_context", scopes=["code"], top_k=8)
        )
        rendered = self.graph.query_context("fresh_context", scopes=["code"], top_k=8)

        self.assertIsNotNone(result.source_revision)
        self.assertEqual(result.freshness.status, "current")
        self.assertNotIn("snapshot", result.freshness.to_dict())
        self.assertIn("Graph freshness: current", rendered)
        self.assertNotIn("snapshot", rendered.casefold())

    def test_rendered_file_prefers_a_targeted_read_over_a_wide_owner_span(self) -> None:
        rendered = self.graph.retrieval.render_context_payload(
            {
                "kind": "code",
                "query": "session recovery",
                "query_mode": "informative",
                "confidence": {"status": "sufficient"},
                "working_set": [{"path": "src/workspace.py", "symbols": ["Workspace"], "score": 0.8}],
                "owner_candidates": [
                    {"path": "src/workspace.py", "line_start": 10, "line_end": 900, "name": "Workspace"}
                ],
                "targeted_reads": [
                    {"path": "src/workspace.py", "line_start": 240, "line_end": 268, "label": "Workspace.resume"}
                ],
                "test_targets": [],
            }
        )

        self.assertIn("src/workspace.py [240-268]", rendered)
        self.assertNotIn("src/workspace.py [10-900]", rendered)

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

    def test_exact_document_path_wins_across_query_surfaces(self) -> None:
        self.graph.close()
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        (project / "CONTRIBUTING.md").write_text(
            "# Contribution Guide\n\n"
            "Use the focused review workflow for patches.\n\n"
            + "\n".join(f"## Topic {index}\n\nGuidance term {index}." for index in range(80)),
            encoding="utf-8",
        )
        source = project / "pipeline.py"
        source.write_text(
            'class PipelineSymbol:\n    """One source symbol contributing to a pipeline component."""\n',
            encoding="utf-8",
        )
        self.graph = open_graph_with_documents(self.db)
        self.graph.compile_project(project)

        context = self.graph.query_context_payload("CONTRIBUTING.md", top_k=12)
        docs_context = self.graph.query_context_payload("CONTRIBUTING.md", scopes=["docs"], top_k=12)
        memories = self.graph.query_memories_payload("CONTRIBUTING.md", limit=8)
        query_graph = self.graph.query_graph("CONTRIBUTING.md", top_k=12, max_depth=2)
        document_content = self.graph.query_context_payload(
            "focused review workflow patches",
            scopes=["docs"],
            top_k=12,
        )
        code_context = self.graph.query_context_payload("pipeline.py", scopes=["code"], top_k=12)
        wrong_scope = self.graph.query_context_payload("CONTRIBUTING.md", scopes=["code"], top_k=12)
        raw_retrieval = self.graph.query(
            "RETRIEVE 'CONTRIBUTING.md' LIMIT 5 RETURN id,type,score,relative_path"
        )
        located = self.graph.locate("CONTRIBUTING.md")

        self.assertEqual(context["payload"]["kind"], "general")
        self.assertEqual(context["payload"]["results"][0]["location"], "CONTRIBUTING.md")
        self.assertEqual(docs_context["payload"]["kind"], "general")
        self.assertEqual(docs_context["confidence"]["status"], "sufficient")
        self.assertEqual(docs_context["payload"]["results"][0]["location"], "CONTRIBUTING.md")
        self.assertEqual(memories["memories"][0]["location"], "CONTRIBUTING.md")
        self.assertTrue(query_graph["seed_nodes"])
        self.assertTrue(
            all(node["properties"].get("relative_path") == "CONTRIBUTING.md" for node in query_graph["seed_nodes"])
        )
        self.assertEqual(document_content["confidence"]["status"], "sufficient")
        self.assertTrue(document_content["payload"]["results"][0]["location"].startswith("CONTRIBUTING.md"))
        self.assertEqual(code_context["payload"]["kind"], "code")
        self.assertEqual(code_context["payload"]["working_set"][0]["path"], "pipeline.py")
        self.assertEqual(wrong_scope["confidence"]["status"], "insufficient")
        self.assertEqual(wrong_scope["payload"]["working_set"], [])
        self.assertEqual(raw_retrieval.rows[0]["relative_path"], "CONTRIBUTING.md")
        self.assertEqual(located["matches"][0]["relative_path"], "CONTRIBUTING.md")
        self.assertNotIn("pipeline.py", self.graph.query_context("CONTRIBUTING.md", top_k=12))
