from __future__ import annotations

from dataclasses import asdict
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
from memory.domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from memory.extraction import normalization


class NormalizationTests(unittest.TestCase):

    def test_query_tokenization_is_language_agnostic(self) -> None:
        self.assertFalse(hasattr(normalization, "STOPWORDS"))
        self.assertFalse(hasattr(normalization, "TECH_TERMS"))
        tokens = normalization.tokenize("where this dove questo pagamento Überweisung APIError42")

        self.assertIn("where", tokens)
        self.assertIn("this", tokens)
        self.assertIn("dove", tokens)
        self.assertIn("questo", tokens)
        self.assertIn("pagamento", tokens)
        self.assertIn("uberweisung", tokens)
        self.assertIn("apierror42", tokens)

class DomainModelTests(unittest.TestCase):
    def test_node_and_edge_to_dict_match_dataclass_payload_and_isolate_properties(self) -> None:
        node = MemoryNode(
            id="n1",
            type="Topic",
            label="node",
            properties={"nested": {"items": ["a"]}},
        )
        edge = MemoryEdge(
            id="e1",
            from_id="n1",
            to_id="n2",
            type="RELATED_TO",
            properties={"nested": {"items": ["b"]}},
        )

        node_payload = node.to_dict()
        edge_payload = edge.to_dict()

        self.assertEqual(node_payload, asdict(node))
        self.assertEqual(edge_payload, asdict(edge))
        node_payload["properties"]["nested"]["items"].append("mutated")
        edge_payload["properties"]["nested"]["items"].append("mutated")
        self.assertEqual(node.properties["nested"]["items"], ["a"])
        self.assertEqual(edge.properties["nested"]["items"], ["b"])


class MemoryGraphIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "memory.reql"
        self.graph = MemoryGraph.open(self.db)

    def tearDown(self) -> None:
        self.graph.close()
        self.tmp.cleanup()

    def test_public_retrieve_returns_raw_subgraph(self) -> None:
        self.graph.add_node(
            MemoryNode(
                id="function:retrieve-api",
                type="Function",
                label="retrieve_api",
                text="retrieve public api raw subgraph",
                canonical_key="function:retrieve-api",
                salience=0.8,
            )
        )

        subgraph = self.graph.retrieve("retrieve public api", top_k=3, max_depth=1)

        self.assertTrue(any(item.node.id == "function:retrieve-api" for item in subgraph.ranked_nodes))
        self.assertIn("function:retrieve-api", {node.id for node in subgraph.nodes})
        self.assertIsNotNone(subgraph.trace_id)

    def test_locate_uses_the_normalized_path_index(self) -> None:
        indexed = MemoryNode(
            id="artifact:indexed",
            type="SourceArtifact",
            label="src/Indexed.py",
            properties={
                "relative_path": "src/Indexed.py",
                "relative_path_key": "src/indexed.py",
            },
        )
        self.graph.add_node(indexed)

        self.assertEqual(
            [item["id"] for item in self.graph.locate("SRC/INDEXED.py")["matches"]],
            [indexed.id],
        )

    def test_query_context_prioritizes_code_working_set_for_coding_agent_queries(self) -> None:
        query = "query_context coding agent minimal files context retrieval noise guide edits"
        project_root = Path(self.tmp.name) / "project"
        source_path = project_root / "src" / "memory" / "services" / "retrieval.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            "\n".join(
                [
                    "class RetrievalEngine:",
                    "    def query_context(self):",
                    "        return 'focused context'",
                    "        # guide edits with snippets",
                ]
            ),
            encoding="utf-8",
        )
        file_node = MemoryNode(
            id="file:retrieval",

            type="File",
            label="src/memory/services/retrieval.py",
            text="Retrieval service file.",
            canonical_key="file:retrieval",
            properties={"relative_path": "src/memory/services/retrieval.py", "context_scope": "code", "path": str(source_path), "project_id": "project:test"},
            salience=0.7,
        )
        function_node = MemoryNode(
            id="function:query-context",

            type="Function",
            label="query_context",
            text=query,
            canonical_key="src.memory.services.retrieval.RetrievalEngine.query_context",
            properties={
                "relative_path": "src/memory/services/retrieval.py",
                "context_scope": "code",
                "name": "query_context",
                "qualified_name": "src.memory.services.retrieval.RetrievalEngine.query_context",
                "line_start": 2,
                "line_end": 4,
            },
            salience=0.8,
        )
        finding_node = MemoryNode(
            id="finding:retrieval-noise",

            type="StaticAnalysisFinding",
            label="unused_variable: noise",
            text="Context retrieval noise should guide a focused coding agent edit.",
            canonical_key="finding:retrieval-noise",
            properties={
                "relative_path": "src/memory/services/retrieval.py",
                "context_scope": "code",
                "finding_type": "unused_variable",
                "symbol_name": "noise",
                "cleanup_priority": "high",
                "cleanup_rank": 3,
                "confidence": 0.8,
                "removal_safety": "safe",
                "removal_reason": "unused_variable is local to this artifact with high confidence and no public-surface signal.",
                "validation_reason": "",
                "blocking_signals": [],
            },
            salience=0.75,
        )
        broad_source_fragment = MemoryNode(
            id="fragment:retrieval-class",

            type="SourceFragment",
            label="src/memory/services/retrieval.py#class",
            text=query,
            canonical_key="fragment:retrieval-class",
            properties={"relative_path": "src/memory/services/retrieval.py", "context_scope": "code", "line_start": 1, "line_end": 1000},
            salience=0.95,
        )
        generated_fragment = MemoryNode(
            id="fragment:generated-pkg-info",

            type="SourceFragment",
            label="src/reql.egg-info/PKG-INFO#1",
            text=query,
            canonical_key="fragment:generated-pkg-info",
            properties={"relative_path": "src/reql.egg-info/PKG-INFO", "context_scope": "docs", "line_start": 1, "line_end": 3},
            salience=0.95,
        )
        test_noise = MemoryNode(
            id="function:test-query-context",

            type="Function",
            label="test_query_context_noise",
            text="query_context coding agent minimal files context retrieval noise guide edits",
            canonical_key="tests.test_retrieval.test_query_context_noise",
            properties={
                "relative_path": "tests/test_retrieval.py",
                "context_scope": "test",
                "name": "test_query_context_noise",
                "qualified_name": "tests.test_retrieval.test_query_context_noise",
                "line_start": 10,
                "line_end": 18,
            },
            salience=0.9,
        )
        docs_noise = MemoryNode(
            id="fragment:docs-query-context",

            type="SourceFragment",
            label="docs/query_context.md#1",
            text="query_context coding agent minimal files context retrieval noise guide edits",
            canonical_key="fragment:docs-query-context",
            properties={"relative_path": "docs/query_context.md", "context_scope": "docs", "line_start": 1, "line_end": 4},
            salience=0.9,
        )
        for node in (file_node, function_node, finding_node, broad_source_fragment, generated_fragment, test_noise, docs_noise):
            self.graph.add_node(node)
        self.graph.add_edge(MemoryEdge(id="edge:file-function", from_id=file_node.id, to_id=function_node.id, type="CONTAINS", weight=1.0, properties={"relative_path": "src/memory/services/retrieval.py"}))
        self.graph.add_edge(MemoryEdge(id="edge:function-finding", from_id=function_node.id, to_id=finding_node.id, type="HAS_FINDING", weight=1.0, properties={"relative_path": "src/memory/services/retrieval.py"}))

        context = self.graph.query_context(query, top_k=8, scopes=["code"])

        self.assertIn("# REQL Context", context)
        self.assertIn("Mode: informative", context)
        self.assertIn("Scope: code", context)
        self.assertIn("## Files", context)
        self.assertIn("## Associated tests", context)
        self.assertNotIn("## Read plan", context)
        self.assertNotIn("## Recent changes", context)
        self.assertNotIn("## Action plan", context)
        self.assertNotIn("## Change chain", context)
        self.assertNotIn("## Research queries", context)
        self.assertNotIn("## Summary", context)
        self.assertNotIn("## Graph links", context)
        self.assertNotIn("## Snippets", context)
        self.assertNotIn("## Best matches", context)
        self.assertNotIn("## Source evidence", context)
        self.assertIn("src/memory/services/retrieval.py [2-4]", context)
        self.assertIn("owners=src.memory.services.retrieval.RetrievalEngine.query_context", context)
        self.assertNotIn("signals:", context)
        self.assertIn("src/memory/services/retrieval.py", context)
        self.assertIn("query_context", context)
        self.assertNotIn("src/reql.egg-info/PKG-INFO", context)
        self.assertNotIn("docs/query_context.md", context)
        self.assertIn("tests/test_retrieval.py [10-18]", context)

        envelope = self.graph.query_context_payload(query, top_k=8, scopes=["code"])
        payload = envelope["payload"]
        self.assertEqual(payload["kind"], "code")
        self.assertEqual(payload["query_mode"], "informative")
        self.assertEqual(payload["scopes"], ["code"])
        self.assertNotIn("context", payload)
        self.assertNotIn("usage_guidance", payload)
        self.assertTrue(any(item["id"] == "function:query-context" for item in payload["owner_candidates"]))
        self.assertNotIn("primary_targets", payload)
        self.assertNotIn("intervention_targets", payload)
        self.assertTrue(payload["read_plan"])
        self.assertTrue(payload["change_chain"])
        self.assertFalse(any(step.get("phase") == "verify" for step in payload["change_chain"]))
        self.assertFalse(any("instruction" in step for step in payload["change_chain"]))
        self.assertTrue(any(row["path"] == "src/memory/services/retrieval.py" for row in payload["working_set"]))
        retrieval_rows = [row for row in payload["working_set"] if row["path"] == "src/memory/services/retrieval.py"]
        self.assertTrue(retrieval_rows)
        self.assertEqual(retrieval_rows[0]["role"], "read")
        self.assertEqual(retrieval_rows[0]["line_start"], 2)
        self.assertEqual(retrieval_rows[0]["line_end"], 4)
        self.assertFalse(any(row["path"] == "tests/test_retrieval.py" for row in payload["working_set"]))
        self.assertFalse(any(row["path"] == "docs/query_context.md" for row in payload["working_set"]))
        self.assertTrue(payload["contracts"])
        self.assertTrue(payload["impact"])
        self.assertTrue(payload["targeted_reads"])
        self.assertFalse(payload["snippets"])
        self.assertTrue(any(item["path"] == "tests/test_retrieval.py" for item in payload["test_targets"]))
        self.assertEqual(envelope["confidence"]["status"], "sufficient")
        self.assertFalse(envelope["confidence"]["targeted_rg_fallback_allowed"])
        self.assertTrue(any(item["label"] == "Retrieve ranked rows" for item in payload["followups"]))
        self.assertNotIn("symbols", payload)
        self.assertNotIn("code_links", payload)

        informative_payload = self.graph.query_context_payload(
            "query_context project structure context retrieval",
            top_k=8,
        )["payload"]
        self.assertEqual(informative_payload["kind"], "code")
        self.assertEqual(informative_payload["query_mode"], "informative")
        self.assertNotIn("context", informative_payload)
        self.assertNotIn("intervention_targets", informative_payload)
        self.assertNotIn("usage_guidance", informative_payload)
        self.assertIn("read_plan", informative_payload)
        self.assertIn("change_chain", informative_payload)
        self.assertFalse(informative_payload["snippets"])
        self.assertFalse(informative_payload["edit_plan"])
        self.assertTrue(all(row["role"] == "read" for row in informative_payload["working_set"]))

        cleanup_payload = self.graph.query_context_payload(
            "unused variable cleanup query_context noise",
            top_k=8,
            mode="cleanup",
        )["payload"]
        self.assertEqual(cleanup_payload["kind"], "code")
        self.assertEqual(cleanup_payload["query_mode"], "cleanup")
        self.assertNotIn("context", cleanup_payload)
        self.assertTrue(cleanup_payload["cleanup_candidates"])
        self.assertNotIn("primary_targets", cleanup_payload)
        self.assertNotIn("intervention_targets", cleanup_payload)
        self.assertTrue(any(row["role"] == "cleanup" for row in cleanup_payload["working_set"]))
        self.assertTrue(cleanup_payload["cleanup_plan"])
        cleanup_candidate = cleanup_payload["cleanup_candidates"][0]
        self.assertEqual(cleanup_candidate["removal_safety"], "safe")
        self.assertIn("removal_reason", cleanup_candidate)
        self.assertIn("validation_reason", cleanup_candidate)

        cleanup_context = self.graph.query_context("unused variable cleanup query_context noise", top_k=8, mode="cleanup")
        self.assertIn("## Cleanup candidates", cleanup_context)
        self.assertIn("## Change chain", cleanup_context)
        self.assertIn("## Research queries", cleanup_context)
        self.assertIn("## Summary", cleanup_context)

        default_payload = self.graph.query_context_payload(
            "modifica unused cleanup query_context noise",
            top_k=8,
        )["payload"]
        self.assertEqual(default_payload["query_mode"], "informative")
        self.assertNotIn("intervention_targets", default_payload)
        self.assertFalse(default_payload["cleanup_candidates"])

    def test_scoped_query_context_does_not_materialize_the_full_graph(self) -> None:
        query = "query_context indexed scoped retrieval"
        self.graph.add_node(
            MemoryNode(
                id="function:scoped-query-context",
                type="Function",
                label="query_context",
                text=query,
                canonical_key="function:scoped-query-context",
                properties={
                    "relative_path": "src/memory/services/retrieval.py",
                    "context_scope": "code",
                    "name": "query_context",
                    "qualified_name": "memory.services.retrieval.RetrievalEngine.query_context",
                    "line_start": 246,
                    "line_end": 249,
                },
                salience=0.9,
            )
        )
        self.graph.add_node(
            MemoryNode(
                id="fragment:scoped-query-context-docs",
                type="SourceFragment",
                label="docs/query_context.md#1",
                text=query,
                canonical_key="fragment:scoped-query-context-docs",
                properties={
                    "relative_path": "docs/query_context.md",
                    "context_scope": "docs",
                    "line_start": 1,
                    "line_end": 3,
                },
                salience=1.0,
            )
        )

        with patch.object(self.graph.store, "all_nodes", side_effect=AssertionError("full graph scan")):
            payload = self.graph.query_context_payload(query, top_k=8, scopes=["code"])["payload"]

        self.assertTrue(any(item["id"] == "function:scoped-query-context" for item in payload["owner_candidates"]))
        self.assertFalse(any(item["path"] == "docs/query_context.md" for item in payload["working_set"]))

    def test_docs_query_uses_specific_evidence_and_visible_confidence(self) -> None:
        query = "configure diagnostics performance logging path"
        concept = MemoryNode(
            id="concept:diagnostics-enabled",
            type="Concept",
            label="diagnostics enabled",
            text="project defaults cache graph settings",
            canonical_key="concept:diagnostics-enabled",
            properties={
                "relative_path": "docs/CONFIGURATION.md",
                "context_scope": "docs",
                "extractor": "document_processor",
            },
            salience=0.7,
        )
        evidence = MemoryNode(
            id="raw:diagnostics-enabled",
            type="RawEvent",
            label="document_term_observation:diagnostics enabled",
            text="diagnostics.enabled controls structured performance logging path",
            canonical_key="raw:diagnostics-enabled",
            properties={
                "relative_path": "docs/CONFIGURATION.md",
                "context_scope": "docs",
                "extractor": "document_processor",
                "line_start": 84,
                "line_end": 84,
            },
            salience=0.5,
        )
        generic = MemoryNode(
            id="concept:generic-community",
            type="Concept",
            label="community",
            text="community records mention performance logging",
            canonical_key="concept:generic-community",
            properties={
                "relative_path": "docs/GRAPH_ANALYSIS.md",
                "context_scope": "docs",
                "extractor": "document_processor",
            },
            salience=0.9,
        )
        for node in (concept, evidence, generic):
            self.graph.add_node(node)
        self.graph.add_edge(
            MemoryEdge(
                id="edge:diagnostics-evidence",
                from_id=concept.id,
                to_id=evidence.id,
                type="EVIDENCED_BY",
                weight=1.0,
            )
        )

        envelope = self.graph.query_context_payload(query, top_k=12, scopes=["docs"])
        payload = envelope["payload"]

        self.assertTrue(payload["results"])
        top = payload["results"][0]
        self.assertEqual(top["id"], concept.id)
        self.assertEqual(top["text"], evidence.text)
        self.assertEqual(top["location"], "docs/CONFIGURATION.md:84")
        self.assertEqual(envelope["confidence"]["max_score"], round(float(top["score"]), 4))
        self.assertEqual(envelope["confidence"]["status"], "sufficient")
        self.assertFalse(any(item["type"] == "RawEvent" for item in payload["results"]))
        self.assertFalse(any(item["id"] == generic.id for item in payload["results"]))

    def test_cleanup_query_context_includes_stronger_targeted_read_payload(self) -> None:
        source_path = Path(self.tmp.name) / "app.py"
        source_path.write_text(
            "\n".join(
                [
                    "import os",
                    "import sys",
                    "",
                    "def caller():",
                    "    return os.getcwd()",
                    "",
                    "def used():",
                    "    return caller()",
                    "",
                    "used()",
                    "",
                    "VALUE = 1",
                ]
            ),
            encoding="utf-8",
        )
        import_node = MemoryNode(
            id="import:sys",
            type="Import",
            label="sys",
            text="import sys",
            canonical_key="app.py:import:sys",
            properties={"relative_path": "app.py", "path": str(source_path), "name": "sys", "module": "sys", "line_start": 2, "line_end": 2},
            salience=0.8,
        )
        module_node = MemoryNode(
            id="module:app",
            type="Module",
            label="app",
            text="module app imports sys",
            canonical_key="module:app",
            properties={"relative_path": "app.py", "path": str(source_path), "name": "app", "line_start": 1, "line_end": 12},
            salience=0.7,
        )
        caller_node = MemoryNode(
            id="function:caller",
            type="Function",
            label="caller",
            text="caller references sys in static graph",
            canonical_key="app.caller",
            properties={"relative_path": "app.py", "path": str(source_path), "name": "caller", "qualified_name": "app.caller", "line_start": 4, "line_end": 5},
            salience=0.7,
        )
        docs_node = MemoryNode(
            id="fragment:docs-sys",
            type="SourceFragment",
            label="docs/usage.md#sys",
            text="Documentation mentions sys cleanup.",
            canonical_key="docs:sys",
            properties={"relative_path": "docs/usage.md", "line_start": 3, "line_end": 4},
            salience=0.4,
        )
        test_node = MemoryNode(
            id="function:test-sys",
            type="Function",
            label="test_sys_cleanup",
            text="test references sys cleanup",
            canonical_key="tests.test_app.test_sys_cleanup",
            properties={"relative_path": "tests/test_app.py", "name": "test_sys_cleanup", "qualified_name": "tests.test_app.test_sys_cleanup", "line_start": 7, "line_end": 9},
            salience=0.4,
        )
        importer_node = MemoryNode(
            id="module:consumer",
            type="Module",
            label="consumer",
            text="consumer imports sys from another file",
            canonical_key="module:consumer",
            properties={"relative_path": "pkg/consumer.py", "name": "consumer", "line_start": 1, "line_end": 3},
            salience=0.4,
        )
        finding = MemoryNode(
            id="finding:unused-sys",
            type="StaticAnalysisFinding",
            label="unused_import: sys",
            text="Import sys has no detected reference in this artifact.",
            canonical_key="app.py:finding:unused_import:sys",
            properties={
                "relative_path": "app.py",
                "path": str(source_path),
                "finding_type": "unused_import",
                "symbol_id": import_node.id,
                "symbol_type": "Import",
                "symbol_name": "sys",
                "qualified_name": "sys",
                "line_start": 2,
                "line_end": 2,
                "cleanup_priority": "high",
                "cleanup_rank": 3,
                "confidence": 0.8,
                "removal_safety": "safe",
                "removal_reason": "unused_import is local to this artifact with high confidence and no public-surface signal.",
                "validation_reason": "",
                "blocking_signals": [],
                "evidence_scope": "local_artifact",
            },
            salience=0.9,
        )
        for node in (import_node, module_node, caller_node, docs_node, test_node, importer_node, finding):
            self.graph.add_node(node)
        self.graph.add_edge(MemoryEdge(id="edge:module-import", from_id=module_node.id, to_id=import_node.id, type="IMPORTS", properties={"relative_path": "app.py", "line_start": 2, "line_end": 2}))
        self.graph.add_edge(MemoryEdge(id="edge:consumer-import", from_id=importer_node.id, to_id=import_node.id, type="IMPORTS_FROM", properties={"relative_path": "pkg/consumer.py", "line_start": 1, "line_end": 3}))
        self.graph.add_edge(MemoryEdge(id="edge:caller-import", from_id=caller_node.id, to_id=import_node.id, type="REFERENCES", properties={"relative_path": "app.py", "line_start": 5, "line_end": 5}))
        self.graph.add_edge(MemoryEdge(id="edge:docs-import", from_id=docs_node.id, to_id=import_node.id, type="REFERENCES", properties={"relative_path": "docs/usage.md", "line_start": 3, "line_end": 4}))
        self.graph.add_edge(MemoryEdge(id="edge:test-import", from_id=test_node.id, to_id=import_node.id, type="TESTS", properties={"relative_path": "tests/test_app.py", "line_start": 7, "line_end": 9}))
        self.graph.add_edge(MemoryEdge(id="edge:import-finding", from_id=import_node.id, to_id=finding.id, type="HAS_FINDING", properties={"relative_path": "app.py", "line_start": 2, "line_end": 2}))

        payload = self.graph.query_context_payload(
            "unused import sys cleanup",
            top_k=8,
            max_depth=1,
            mode="cleanup",
        )["payload"]
        reads = payload["targeted_reads"]
        kinds = {item.get("read_kind") for item in reads}

        self.assertIn("import_block", kinds)
        self.assertIn("finding_context", kinds)
        self.assertIn("caller_ref", kinds)
        self.assertIn("importer_ref", kinds)
        self.assertIn("doc_ref", kinds)
        self.assertIn("test_ref", kinds)
        context_read = next(item for item in reads if item.get("read_kind") == "finding_context")
        self.assertEqual(context_read["line_start"], 1)
        self.assertEqual(context_read["line_end"], 7)
        self.assertEqual(context_read["sufficiency"]["status"], "insufficient")
        self.assertIn("Reference checks found", context_read["sufficiency"]["reason"])
        self.assertTrue(any(item["path"] == "app.py" and "import sys" in item["text"] for item in payload["snippets"]))

        rendered = self.graph.query_context("unused import sys cleanup", top_k=8, max_depth=1, mode="cleanup")
        self.assertIn("## Targeted reads", rendered)
        self.assertIn("import_block `app.py [2]`", rendered)
        self.assertIn("## Snippets", rendered)
        self.assertIn("import sys", rendered)

    def test_cleanup_query_context_filters_risky_findings_by_default(self) -> None:
        safe = MemoryNode(
            id="finding:safe-unused",
            type="StaticAnalysisFinding",
            label="unused_variable: safe_local",
            text="safe_local cleanup candidate",
            properties={
                "relative_path": "app.py",
                "finding_type": "unused_variable",
                "symbol_name": "safe_local",
                "line_start": 3,
                "line_end": 3,
                "cleanup_priority": "high",
                "cleanup_rank": 3,
                "confidence": 0.8,
                "removal_safety": "safe",
                "removal_reason": "unused_variable is local to this artifact with high confidence and no public-surface signal.",
                "validation_reason": "",
                "blocking_signals": [],
            },
            salience=0.9,
        )
        risky = MemoryNode(
            id="finding:risky-public-api",
            type="StaticAnalysisFinding",
            label="possibly_unused_function: public_api",
            text="public_api cleanup candidate",
            properties={
                "relative_path": "app.py",
                "finding_type": "possibly_unused_function",
                "symbol_name": "public_api",
                "line_start": 8,
                "line_end": 9,
                "cleanup_priority": "low",
                "cleanup_rank": 1,
                "confidence": 0.4,
                "removal_safety": "risky",
                "removal_reason": "possibly_unused_function has no detected local usage, but removal needs validation before editing.",
                "validation_reason": "Validate public API, callbacks, configuration, and documentation before removing this symbol.",
                "blocking_signals": ["public_api", "dynamic_reference_unknown"],
            },
            salience=0.9,
        )
        self.graph.add_node(safe)
        self.graph.add_node(risky)

        default_payload = self.graph.query_context_payload(
            "cleanup candidate",
            top_k=8,
            mode="cleanup",
        )["payload"]
        default_ids = {item["id"] for item in default_payload["cleanup_candidates"]}
        self.assertIn(safe.id, default_ids)
        self.assertNotIn(risky.id, default_ids)
        self.assertEqual(default_payload["cleanup_filter"]["mode"], "safe_remove")
        self.assertEqual(default_payload["cleanup_filter"]["excluded_risky_candidates"], 1)
        self.assertFalse(any(item.get("finding_id") == risky.id for item in default_payload["targeted_reads"]))
        self.assertFalse(any(item.get("node_id") == risky.id for item in default_payload["snippets"]))

    def test_query_context_scopes_retrieve_inside_requested_section_before_top_k_cutoff(self) -> None:
        query = "shared scoped query_context target"
        code_node = MemoryNode(
            id="function:scoped-code",
            type="Function",
            label="shared_scoped_code",
            text=query,
            canonical_key="src.scoped.shared_scoped_code",
            properties={"relative_path": "src/scoped.py", "context_scope": "code", "qualified_name": "src.scoped.shared_scoped_code", "line_start": 3, "line_end": 7},
            salience=0.95,
        )
        test_node = MemoryNode(
            id="function:scoped-test",
            type="Function",
            label="test_shared_scoped_code",
            text=query,
            canonical_key="tests.test_scoped.test_shared_scoped_code",
            properties={"relative_path": "tests/test_scoped.py", "context_scope": "test", "qualified_name": "tests.test_scoped.test_shared_scoped_code", "line_start": 10, "line_end": 16},
            salience=0.2,
        )
        docs_node = MemoryNode(
            id="fragment:scoped-docs",
            type="SourceFragment",
            label="docs/scoped.md#1",
            text=query,
            canonical_key="fragment:scoped-docs",
            properties={"relative_path": "docs/scoped.md", "context_scope": "docs", "line_start": 2, "line_end": 4},
            salience=0.1,
        )
        self.graph.add_node(code_node)
        self.graph.add_node(test_node)
        self.graph.add_node(docs_node)

        code_payload = self.graph.query_context_payload(query, top_k=1, scopes=["code"])["payload"]
        test_payload = self.graph.query_context_payload(query, top_k=1, scopes=["test"])["payload"]
        docs_payload = self.graph.query_context_payload(query, top_k=1, scopes=["docs"])["payload"]

        self.assertTrue(any(row["path"] == "src/scoped.py" for row in code_payload["working_set"]))
        self.assertFalse(any(row["path"] == "tests/test_scoped.py" for row in code_payload["working_set"]))
        self.assertTrue(any(row["path"] == "tests/test_scoped.py" for row in test_payload["working_set"]))
        self.assertFalse(any(row["path"] == "src/scoped.py" for row in test_payload["working_set"]))
        self.assertTrue(any(item["location"] == "docs/scoped.md:2-4" for item in docs_payload["results"]))

    def test_query_context_keeps_structured_identifier_matches_actionable(self) -> None:
        target = MemoryNode(
            id="function:code-targeted-reads",
            type="Method",
            label="src.memory.services.retrieval.RetrievalEngine._code_targeted_reads",
            text="",
            canonical_key="src.memory.services.retrieval.RetrievalEngine._code_targeted_reads",
            properties={
                "relative_path": "src/memory/services/retrieval.py",
                "context_scope": "code",
                "name": "_code_targeted_reads",
                "qualified_name": "src.memory.services.retrieval.RetrievalEngine._code_targeted_reads",
                "line_start": 2024,
                "line_end": 2075,
            },
            salience=0.4,
        )
        broad_fragment = MemoryNode(
            id="fragment:compiler-sourcefragment-noise",
            type="SourceFragment",
            label="src/memory/artifacts/compiler.py#noise",
            text="SourceFragment owner symbol targeted reads generic compiler context",
            canonical_key="fragment:compiler-sourcefragment-noise",
            properties={"relative_path": "src/memory/artifacts/compiler.py", "context_scope": "code", "line_start": 540, "line_end": 856},
            salience=0.95,
        )
        self.graph.add_node(target)
        self.graph.add_node(broad_fragment)

        payload = self.graph.query_context_payload(
            "RetrievalEngine _code_targeted_reads SourceFragment owner symbol targeted reads",
            top_k=8,
            scopes=["code"],
        )["payload"]

        self.assertTrue(any(row["path"] == "src/memory/services/retrieval.py" for row in payload["working_set"]))
        self.assertFalse(any(row["path"] == "src/memory/artifacts/compiler.py" for row in payload["working_set"]))
        self.assertTrue(any(item["id"] == "function:code-targeted-reads" for item in payload["owner_candidates"]))
        reads = [item for item in payload["targeted_reads"] if item["node_id"] == "function:code-targeted-reads"]
        self.assertTrue(reads)
        self.assertEqual(reads[0]["line_start"], 2024)
        self.assertEqual(reads[0]["line_end"], 2075)

    def test_query_context_embeds_exact_source_fragment_snippet_for_small_code_sets(self) -> None:
        project_root = Path(self.tmp.name) / "project"
        source_path = project_root / "profile" / "show.php"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            "\n".join(
                [
                    "<?php",
                    "<section>",
                    "  <h1>Profilo utente</h1>",
                    "  <h2>Statistiche di visualizzazione</h2>",
                    "</section>",
                ]
            ),
            encoding="utf-8",
        )
        fragment = MemoryNode(
            id="fragment:profile-show-stats",
            type="SourceFragment",
            label="profile/show.php#stats",
            text="Statistiche di visualizzazione",
            canonical_key="fragment:profile-show-stats",
            properties={
                "relative_path": "profile/show.php",
                "source_path": str(source_path),
                "context_scope": "code",
                "line_start": 4,
                "line_end": 4,
            },
            salience=0.9,
        )
        file_node = MemoryNode(
            id="file:profile-show",
            type="File",
            label="profile/show.php",
            text="profile show php source file",
            canonical_key="file:profile-show",
            properties={
                "relative_path": "profile/show.php",
                "path": str(source_path),
                "context_scope": "code",
            },
            salience=0.5,
        )
        self.graph.add_node(file_node)
        self.graph.add_node(fragment)
        self.graph.add_edge(MemoryEdge(id="edge:file-profile-stats", from_id=file_node.id, to_id=fragment.id, type="CONTAINS"))

        payload = self.graph.query_context_payload(
            "profile show php Statistiche di visualizzazione",
            top_k=6,
            scopes=["code"],
        )["payload"]
        rendered = self.graph.query_context(
            "profile show php Statistiche di visualizzazione",
            top_k=6,
            scopes=["code"],
        )

        self.assertTrue(payload["snippets"])
        self.assertEqual(payload["snippets"][0]["path"], "profile/show.php")
        self.assertEqual(payload["snippets"][0]["line_start"], 4)
        self.assertIn("Statistiche di visualizzazione", payload["snippets"][0]["text"])
        self.assertNotIn("## Snippets", rendered)
        self.assertIn("profile/show.php [4]", rendered)

    def test_query_context_renders_at_most_eight_files_with_owners_ranges_and_tests(self) -> None:
        query = "bounded formatter owner range associated tests"
        for index in range(10):
            self.graph.add_node(
                MemoryNode(
                    id=f"method:bounded-source-{index}",
                    type="Method",
                    label=f"App.Service{index}.render_context",
                    text=query,
                    canonical_key=f"App.Service{index}.render_context",
                    properties={
                        "relative_path": f"src/service_{index}.py",
                        "context_scope": "code",
                        "name": "render_context",
                        "qualified_name": f"App.Service{index}.render_context",
                        "line_start": 10 + index,
                        "line_end": 20 + index,
                    },
                )
            )
        for index in range(3):
            self.graph.add_node(
                MemoryNode(
                    id=f"function:bounded-test-{index}",
                    type="Function",
                    label=f"test_bounded_context_{index}",
                    text=query,
                    canonical_key=f"tests.test_service_{index}.test_bounded_context_{index}",
                    properties={
                        "relative_path": f"tests/test_service_{index}.py",
                        "context_scope": "test",
                        "name": f"test_bounded_context_{index}",
                        "qualified_name": f"tests.test_service_{index}.test_bounded_context_{index}",
                        "line_start": 30 + index,
                        "line_end": 35 + index,
                    },
                )
            )

        rendered = self.graph.query_context(query, top_k=20, max_items=20, scopes=["code"])
        rendered_file_lines = [line for line in rendered.splitlines() if line.startswith("- `")]

        self.assertLessEqual(len(rendered_file_lines), 8)
        self.assertGreaterEqual(len(rendered_file_lines), 5)
        self.assertIn("## Associated tests", rendered)
        self.assertIn("owners=App.Service", rendered)
        self.assertRegex(rendered, r"src/service_\d+\.py \[\d+-\d+\]")
        self.assertIn("tests/test_service_", rendered)

    def test_query_context_short_circuits_to_targeted_rg_when_confidence_is_low(self) -> None:
        weak_match = MemoryNode(
            id="method:weak-context-match",
            type="Method",
            label="App.WeakContext.maybe_related",
            text="one weak partial match",
            canonical_key="App.WeakContext.maybe_related",
            properties={
                "relative_path": "src/weak_context.py",
                "context_scope": "code",
                "qualified_name": "App.WeakContext.maybe_related",
                "line_start": 12,
                "line_end": 18,
            },
        )
        subgraph = MemorySubgraph(
            query=MemoryQuery(text="missing exact implementation marker", context_scopes={"code"}),
            ranked_nodes=[
                RankedNode(
                    node=weak_match,
                    score=0.12,
                    reasons={"match_score": 0.08, "coverage": 0.2},
                )
            ],
            nodes=[weak_match],
            edges=[],
            seed_node_ids=[weak_match.id],
            trace_id="retrieval:weak-context",
        )

        payload = self.graph.retrieval.query_context_payload(subgraph, max_items=8, query_scopes=["code"])
        rendered = self.graph.retrieval.compose_context(subgraph, max_items=8, query_scopes=["code"])

        self.assertEqual(payload["confidence"]["status"], "insufficient")
        self.assertEqual(payload["confidence"]["max_score"], 0.12)
        self.assertEqual(payload["confidence"]["threshold"], 0.25)
        self.assertTrue(payload["confidence"]["targeted_rg_fallback_allowed"])
        self.assertIn("Confidence: insufficient", rendered)
        self.assertIn("targeted rg fallback allowed", rendered)
        self.assertNotIn("## Files", rendered)
        self.assertNotIn("src/weak_context.py", rendered)

    def test_query_outputs_include_directional_edge_context(self) -> None:
        upstream = MemoryNode(
            id="fact:upstream",

            type="Fact",
            label="Office plant schedule",
            text="Office plant schedule supports watering context.",
            canonical_key="office_plant_schedule",
            salience=0.8,
        )
        plant = MemoryNode(
            id="fact:plant",

            type="Fact",
            label="Office plant watering",
            text="Office plant watering should happen every Monday.",
            canonical_key="office_plant_watering",
            salience=0.9,
        )
        source = MemoryNode(
            id="fragment:plant-note",

            type="SourceFragment",
            label="Facilities source note",
            text="Facilities source note from the maintenance log.",
            canonical_key="office_plant_source_note",
            salience=0.7,
        )
        for node in (upstream, plant, source):
            self.graph.add_node(node)
        self.graph.add_edge(MemoryEdge(id="edge:incoming", from_id=upstream.id, to_id=plant.id, type="SUPPORTS", weight=1.0))
        self.graph.add_edge(MemoryEdge(id="edge:outgoing", from_id=plant.id, to_id=source.id, type="EVIDENCED_BY", weight=1.0))

        payload = self.graph.query_graph("office plant watering", top_k=3, max_depth=1, max_nodes=10, max_edges=10, filter_generic=False)
        edges_by_id = {edge["id"]: edge for edge in payload["edges"]}

        self.assertTrue(edges_by_id["edge:incoming"]["directed"])
        self.assertEqual(edges_by_id["edge:incoming"]["source_id"], upstream.id)
        self.assertEqual(edges_by_id["edge:incoming"]["target_id"], plant.id)
        self.assertEqual(edges_by_id["edge:incoming"]["direction"], "outgoing")
        self.assertIn("edge:outgoing", edges_by_id)
        self.assertIn("edge_directions", payload)
        plant_directions = payload["edge_directions"][plant.id]
        self.assertEqual({edge["edge_id"] for edge in plant_directions["incoming"]}, {"edge:incoming"})
        self.assertEqual({edge["edge_id"] for edge in plant_directions["outgoing"]}, {"edge:outgoing"})
        self.assertIn(source.id, {item["id"] for item in payload["sources"]})
        self.assertIn("Office plant watering --EVIDENCED_BY--> Facilities source note", payload["context"])
        self.assertIn("Office plant watering: 1 outgoing, 1 incoming", payload["context"])

        memories = self.graph.query_memories("office plant watering", top_k=3, max_depth=1, limit=5)
        source_memory = next(item for item in memories if item["id"] == source.id)
        self.assertEqual(source_memory["source_for"], plant.id)
        self.assertEqual(source_memory["source_for_label"], "Office plant watering")
        self.assertEqual(source_memory["relation"], "EVIDENCED_BY")
        self.assertEqual(source_memory["direction"], "outgoing")
        self.assertEqual(source_memory["edge_id"], "edge:outgoing")

    def test_free_search_prefers_multiterm_matches_over_generic_noise_signals(self) -> None:
        target = MemoryNode(
            id="function:capture-payment-order",
            type="Function",
            label="capture_payment_order_repository",
            text="Capture payment with order repository persistence.",
            canonical_key="src.payments.capture_payment_order_repository",
            salience=0.1,
            volatility=1.0,
            utility=0.0,
            properties={"relative_path": "src/payments.py", "line_start": 10, "line_end": 18},
        )
        generic = MemoryNode(
            id="function:capture",
            type="Function",
            label="capture",
            text="Generic capture helper.",
            canonical_key="src.generic.capture",
            salience=0.99,
            volatility=0.0,
            utility=1.0,
            properties={"relative_path": "src/generic.py", "line_start": 1, "line_end": 4},
        )
        stale = MemoryNode(
            id="fact:stale-contradiction",
            type="Fact",
            label="stale contradiction marker",
            text="Stale contradiction marker should not affect free-search ranking.",
        )
        self.graph.add_node(target)
        self.graph.add_node(generic)
        self.graph.add_node(stale)
        self.graph.add_edge(MemoryEdge(id="edge:stale-contradicts", from_id=stale.id, to_id=target.id, type="CONTRADICTS", weight=1.0))

        payload = self.graph.query_memories_payload("capture payment order repository", top_k=3, max_depth=1)
        ranked_nodes = payload["ranked_nodes"]

        self.assertEqual(ranked_nodes[0]["id"], target.id)
        self.assertNotEqual(ranked_nodes[0]["id"], generic.id)
        self.assertEqual(
            set(ranked_nodes[0]["reasons"]),
            {"match_score", "coverage", "path_score", "type_bonus", "seed_score", "depth_penalty"},
        )

    def test_reql_where_supports_sql_like_text_and_range_operators(self) -> None:
        self.graph.add_node(
            MemoryNode(
                id="fact:office-plant",

                type="Fact",
                label="Office Plant Watering",
                text="Office plant watering should happen every Monday.",
                salience=0.74,
                properties={"relative_path": "notes.md", "line_start": 7, "owner": None},
            )
        )
        self.graph.add_node(
            MemoryNode(
                id="fact:weekly-report",

                type="Fact",
                label="Weekly Report Review",
                text="Weekly report review should finish before Friday.",
                salience=0.25,
                properties={"relative_path": "reports.md", "line_start": 3, "owner": "ops"},
            )
        )

        like = self.graph.query('FIND nodes TYPE Fact WHERE label ILIKE "%plant%" RETURN id,label')
        self.assertEqual([row["id"] for row in like.rows], ["fact:office-plant"])

        regex = self.graph.query('FIND nodes TYPE Fact WHERE text REGEX "Friday\\.$" RETURN id')
        self.assertEqual([row["id"] for row in regex.rows], ["fact:weekly-report"])

        between = self.graph.query("FIND nodes TYPE Fact WHERE salience BETWEEN 0.7 AND 0.8 RETURN id,salience")
        self.assertEqual([row["id"] for row in between.rows], ["fact:office-plant"])

        nulls = self.graph.query("FIND nodes TYPE Fact WHERE owner IS NULL RETURN id")
        self.assertEqual([row["id"] for row in nulls.rows], ["fact:office-plant"])

    def test_reql_retrieve_returns_memory_rows_with_source_location(self) -> None:
        fact = MemoryNode(
            id="fact:plant",

            type="Fact",
            label="Office plant watering",
            text="The office plant should be watered every Monday.",
            salience=0.8,
        )
        source = MemoryNode(
            id="fragment:plant",

            type="SourceFragment",
            label="Office plant note",
            text="The office plant should be watered every Monday.",
            salience=0.9,
            properties={"metadata": {"source_path": "notes.md", "start_line": 4, "end_line": 4}},
        )
        self.graph.add_node(fact)
        self.graph.add_node(source)
        self.graph.add_edge(MemoryEdge(id="edge:plant-source", from_id=fact.id, to_id=source.id, type="EVIDENCED_BY", weight=1.0))

        result = self.graph.query(
            'RETRIEVE "office plant" LIMIT 3 RETURN id,type,text,score,path,line_start,line_end',

        )

        self.assertEqual(result.command, "RETRIEVE")
        self.assertGreater(result.rows[0]["score"], 0)
        self.assertEqual(result.rows[0]["id"], "fact:plant")
        self.assertEqual(result.rows[1]["id"], "fragment:plant")
        self.assertEqual(result.rows[1]["path"], "notes.md")
        self.assertEqual(result.rows[1]["line_start"], 4)
        self.assertEqual(result.rows[1]["line_end"], 4)

    def test_reql_verify_finding_returns_deterministic_bundle(self) -> None:
        symbol = MemoryNode(
            id="function:unused-helper",
            type="Function",
            label="app.unused_helper",
            text="def unused_helper(): return 1",
            canonical_key="app.unused_helper",
            properties={"relative_path": "app.py", "name": "unused_helper", "qualified_name": "app.unused_helper", "line_start": 4, "line_end": 5},
        )
        caller = MemoryNode(
            id="function:caller",
            type="Function",
            label="app.caller",
            text="def caller(): return unused_helper()",
            canonical_key="app.caller",
            properties={"relative_path": "app.py", "name": "caller", "qualified_name": "app.caller", "line_start": 8, "line_end": 9},
        )
        source = MemoryNode(
            id="fragment:unused-helper",
            type="SourceFragment",
            label="app.py#unused_helper",
            text="def unused_helper():\n    return 1",
            canonical_key="fragment:unused-helper",
            properties={"artifact_id": "artifact:app", "relative_path": "app.py", "line_start": 4, "line_end": 5},
        )
        finding = MemoryNode(
            id="static-analysis-finding:unused-helper",
            type="StaticAnalysisFinding",
            label="possibly_unused_function: app.unused_helper",
            text="Function unused_helper has no detected internal caller.",
            canonical_key="artifact:app:finding:possibly_unused_function:app.unused_helper",
            properties={
                "artifact_id": "artifact:app",
                "relative_path": "app.py",
                "context_scope": "code",
                "finding_type": "possibly_unused_function",
                "severity": "info",
                "reason": "Function unused_helper has no detected internal caller.",
                "evidence_scope": "public_api_local_artifact",
                "confidence": 0.4,
                "cleanup_priority": "low",
                "cleanup_rank": 1,
                "removal_safety": "risky",
                "removal_reason": "Public API candidate.",
                "validation_reason": "Validate public API, callbacks, configuration, and documentation before removing this symbol.",
                "blocking_signals": ["public_api", "dynamic_reference_unknown"],
                "symbol_id": symbol.id,
                "symbol_type": symbol.type,
                "symbol_name": "unused_helper",
                "qualified_name": "app.unused_helper",
                "line_start": 4,
                "line_end": 5,
            },
        )
        for node in (symbol, caller, source, finding):
            self.graph.add_node(node)
        self.graph.add_edge(MemoryEdge(id="edge:symbol-source", from_id=symbol.id, to_id=source.id, type="EVIDENCED_BY", properties={"relative_path": "app.py", "line_start": 4, "line_end": 5}))
        self.graph.add_edge(MemoryEdge(id="edge:symbol-finding", from_id=symbol.id, to_id=finding.id, type="HAS_FINDING", properties={"relative_path": "app.py", "line_start": 4, "line_end": 5}))
        self.graph.add_edge(MemoryEdge(id="edge:caller-symbol", from_id=caller.id, to_id=symbol.id, type="CALLS", properties={"relative_path": "app.py", "line_start": 9, "line_end": 9, "evidence": "unused_helper()"}))

        result = self.graph.query("VERIFY FINDING static-analysis-finding:unused-helper")

        self.assertEqual(result.command, "VERIFY FINDING")
        self.assertEqual(result.row_count if hasattr(result, "row_count") else len(result.rows), 1)
        row = result.rows[0]
        self.assertEqual(row["finding"]["id"], finding.id)
        self.assertEqual(row["finding"]["symbol"]["id"], symbol.id)
        self.assertEqual(row["minimal_snippet"]["source_node_id"], source.id)
        self.assertIn("def unused_helper", row["minimal_snippet"]["text"])
        self.assertEqual([item["edge_id"] for item in row["uses_found"]], ["edge:caller-symbol"])
        self.assertEqual(row["uses_found"][0]["direction"], "incoming")
        self.assertTrue(any(scope["scope"] == "artifact" and scope["evidence_scope"] == "public_api_local_artifact" for scope in row["scopes_checked"]))
        self.assertIn("deterministic_incoming_usage_edges_present", row["risks"])
        self.assertIn("Do not remove", row["recommended_action"])


if __name__ == "__main__":
    unittest.main()
