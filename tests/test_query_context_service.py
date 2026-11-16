from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from mcp.tools import call_tool, list_tools, query_context as mcp_query_context
from memory.domain.models import MemoryNode
from memory.services.coordination import CoordinationStore
from memory.domain.query_context import (
    ContextResult,
    QueryContextRequest,
    QueryMode,
    RetrievalBudget,
)
from tests.config_helpers import open_graph_with_documents


class QueryContextContractTests(unittest.TestCase):
    """Validate provider values and the advertised MCP request schema."""

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

    def test_mcp_query_context_schema_uses_the_shared_contract(self) -> None:
        schema = next(item for item in list_tools() if item["name"] == "query_context")
        properties = schema["inputSchema"]["properties"]

        self.assertEqual(properties["top_k"]["default"], 20)
        self.assertEqual(properties["max_depth"]["default"], 3)
        self.assertEqual(properties["max_items"]["default"], 20)
        self.assertEqual(properties["mode"]["enum"], ["informative", "cleanup"])
        self.assertEqual(properties["scopes"]["items"]["enum"], ["code", "docs", "test"])
        for name in ("code", "docs", "test", "include_archived"):
            self.assertIn(name, properties)
        self.assertNotIn("include_risky", properties)


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

    def test_scoped_search_propagates_adapter_type_errors_without_retry(self) -> None:
        """Propagate adapter TypeErrors directly without retrying the failed operation."""
        request = QueryContextRequest.from_raw(text="typed query context", scopes=["code"])
        with patch.object(self.graph.store, "lexical_search", side_effect=TypeError("adapter failure")) as search:
            with self.assertRaisesRegex(TypeError, "adapter failure"):
                self.graph.query_context_result(request)
        search.assert_called_once()
        self.assertFalse(search.call_args.kwargs["clone"])

    def test_empty_lexical_results_use_the_current_type_lookup_contract(self) -> None:
        """The fallback candidate path uses the required indexed store operation."""
        request = QueryContextRequest.from_raw(text="typed query context", scopes=["code"])
        with patch.object(self.graph.store, "lexical_search", return_value=[]):
            with patch.object(self.graph.store, "find_nodes_by_types", side_effect=TypeError("indexed lookup failure")) as lookup:
                with self.assertRaisesRegex(TypeError, "indexed lookup failure"):
                    self.graph.query_context_result(request)
        lookup.assert_called_once()
        self.assertFalse(lookup.call_args.kwargs["clone"])

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
        self.assertNotIn("context", result.payload)
        self.assertEqual(result.payload["kind"], "code")
        for field in ("followups", "working_set", "contracts", "read_plan", "change_chain", "targeted_reads"):
            self.assertIn(field, result.payload)
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

    def test_file_qualified_symbols_are_compact_without_losing_namespace_evidence(self) -> None:
        """The file row supplies the module qualifier while JSON retains exact symbol names."""
        project = Path(self.tmp.name) / "qualified-project"
        for component in ("billing", "shipping"):
            folder = project / "services" / component
            folder.mkdir(parents=True)
            (folder / "rules.py").write_text(
                "class Invoice:\n    def total(self, values):\n        return sum(values)\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(project).run.errors)
        result = self.graph.query_context_result(QueryContextRequest.from_raw(text="Invoice.total", scopes=["code"]))
        names = {row["name"] for row in result.payload["owner_candidates"]}
        self.assertIn("services.billing.rules.Invoice.total", names)
        self.assertIn("services.shipping.rules.Invoice.total", names)
        rendered = self.graph.query_context_service.render(result)
        files = rendered.split("## Files", 1)[1].split("##", 1)[0]
        for component in ("billing", "shipping"):
            self.assertIn(f"services/{component}/rules.py", files)
            self.assertNotIn(f"services.{component}.rules.Invoice.total", files)
        self.assertIn("Invoice.total", files)
        self.assertEqual(names, {row["name"] for row in result.payload["owner_candidates"]})

    def test_compiled_context_exposes_implementation_links_and_affected_callers(self) -> None:
        """Ordinary code context must reveal existing forwarding and override boundaries."""
        project = Path(self.tmp.name) / "implementation-project"
        project.mkdir()
        (project / "base.py").write_text(
            "class Base:\n    def execute(self, value):\n        return value\n\n"
            "def target(value):\n    return value\n", encoding="utf-8")
        (project / "derived.py").write_text(
            "from base import Base, target\n\nclass Derived(Base):\n"
            "    def execute(self, value):\n        return super().execute(value)\n\n"
            "def proxy(value):\n    return target(value)\n\n"
            "def local_request(value):\n    result = proxy(value)\n    return result\n", encoding="utf-8")
        (project / "api.py").write_text(
            "from derived import proxy\n\ndef request(value):\n    return proxy(value)\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(project).run.errors)
        for query, relation in (("proxy", "WRAPS"), ("Derived.execute", "OVERRIDES")):
            with self.subTest(query=query):
                result = self.graph.query_context_result(QueryContextRequest.from_raw(text=query, scopes=["code"], top_k=12))
                links = result.payload["impact"]["implementation_links"]
                self.assertTrue(any(row["edge_type"] == relation for row in links))
                rendered = self.graph.query_context_service.render(result)
                self.assertIn("## Change boundaries", rendered)
                self.assertIn(relation + ":", rendered)
                self.assertIn("base.py", rendered.split("## Change boundaries")[1])
                self.assertIn("derived.py", rendered.split("## Change boundaries")[1])
                if query == "proxy":
                    self.assertIn("CALLS:", rendered)
                    self.assertIn("api.py", rendered.split("## Change boundaries")[1])

    def test_automatic_work_context_is_bounded_without_losing_structured_records(self) -> None:
        """Source discovery carries short current guidance while full work remains inspectable."""
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        constraint = work.put("constraint", "Preserve empty fields", files=["src/typed_context.py"],
                              agent_id="agent:planner", session_id=None)["node"]
        for index in range(8):
            work.put("checkpoint", f"Typed query context result {index}", status="done",
                     rationale="Historical evidence. " * 100, files=["src/typed_context.py"],
                     agent_id="agent:planner", session_id=None)
        result = self.graph.query_context_result(QueryContextRequest.from_raw(text="typed query context", scopes=["code"]))
        engineering = result.payload["engineering_context"]
        self.assertEqual(len(engineering["records"]), 8)
        self.assertEqual(engineering["records"][0]["id"], constraint["id"])
        self.assertTrue(any(len(row["rationale"]) > 1500 for row in engineering["records"]))
        rendered = self.graph.query_context_service.render(result)
        work_text = rendered.split("## Engineering context", 1)[1]
        self.assertLessEqual(len("## Engineering context" + work_text), 2400)
        self.assertIn("Preserve empty fields", work_text)
        self.assertIn("omitted", work_text)
        self.assertIn("agent show", work_text)

    def test_work_evidence_follows_selected_files_instead_of_incidental_ranked_neighbors(self) -> None:
        """An unrelated ranked file must not displace the selected owner's constraint."""
        neighbor = MemoryNode(id="function:neighbor", type="Function", label="neighbor",
                              text="typed query context common service", canonical_key="function:neighbor",
                              properties={"relative_path": "src/neighbor.py", "context_scope": "code",
                                          "qualified_name": "neighbor", "line_start": 1, "line_end": 4})
        self.graph.add_node(neighbor)
        request = QueryContextRequest.from_raw(text="typed query context common service", scopes=["code"], max_items=1)
        selected = self.graph.query_context_result(request)
        self.assertGreater(selected.payload["ranked_nodes"], 1)
        self.assertEqual(len(selected.payload["working_set"]), 1)
        owner_path = selected.payload["working_set"][0]["path"]
        other_path = ({"src/typed_context.py", "src/neighbor.py"} - {owner_path}).pop()
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        owner = work.put("constraint", "Keep numeric ranges", files=[owner_path], importance=1,
                         agent_id="agent:planner", session_id=None)["node"]
        work.put("constraint", "Keep enum variants", files=[other_path], importance=3,
                 agent_id="agent:planner", session_id=None)
        result = self.graph.query_context_result(request)
        self.assertEqual(result.payload["engineering_context"]["records"][0]["id"], owner["id"])

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

        arguments = {
            "storage_path": str(self.db),
            "query": "typed query context common service",
            "scopes": ["code"],
            "top_k": 8,
            "max_depth": 3,
            "max_items": 8,
        }
        mcp_payload = mcp_query_context(**arguments)
        dispatched = call_tool("query_context", arguments)
        for field in ("schema_version", "graph_revision", "confidence", "freshness", "source_revision"):
            self.assertEqual(dispatched[field], mcp_payload[field])
        self.assertNotIn("context", dispatched["payload"])
        self.assertEqual(dispatched["payload"]["kind"], "code")
        for field in ("trace_id", "ranked_nodes", "seed_node_ids", "followups", "working_set",
                      "contracts", "read_plan", "change_chain", "targeted_reads"):
            self.assertIn(field, dispatched["payload"])
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
