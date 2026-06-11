from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.domain.models import MemoryEdge, MemoryNode
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
            properties={"relative_path": "src/memory/services/retrieval.py", "path": str(source_path), "project_id": "project:test"},
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
            properties={"relative_path": "src/memory/services/retrieval.py", "line_start": 1, "line_end": 1000},
            salience=0.95,
        )
        generated_fragment = MemoryNode(
            id="fragment:generated-pkg-info",

            type="SourceFragment",
            label="src/reql.egg-info/PKG-INFO#1",
            text=query,
            canonical_key="fragment:generated-pkg-info",
            properties={"relative_path": "src/reql.egg-info/PKG-INFO", "line_start": 1, "line_end": 3},
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
            properties={"relative_path": "docs/query_context.md", "line_start": 1, "line_end": 4},
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
        self.assertIn("## Code results", context)
        self.assertIn("## Research queries", context)
        self.assertIn("## Summary", context)
        self.assertNotIn("## Best matches", context)
        self.assertNotIn("## Source evidence", context)
        self.assertIn("src/memory/services/retrieval.py [2-4]", context)
        self.assertNotIn("signals:", context)
        self.assertIn("src/memory/services/retrieval.py", context)
        self.assertIn("query_context", context)
        self.assertNotIn("src/reql.egg-info/PKG-INFO", context)
        self.assertNotIn("docs/query_context.md", context)
        self.assertNotIn("tests/test_retrieval.py", context)

        payload = self.graph.query_context_payload(query, top_k=8, scopes=["code"])
        self.assertEqual(payload["kind"], "code")
        self.assertEqual(payload["query_mode"], "informative")
        self.assertEqual(payload["scopes"], ["code"])
        self.assertNotIn("context", payload)
        self.assertTrue(payload["usage_guidance"])
        self.assertTrue(any(item["id"] == "function:query-context" for item in payload["owner_candidates"]))
        self.assertNotIn("primary_targets", payload)
        self.assertNotIn("intervention_targets", payload)
        self.assertTrue(any(row["path"] == "src/memory/services/retrieval.py" for row in payload["working_set"]))
        retrieval_rows = [row for row in payload["working_set"] if row["path"] == "src/memory/services/retrieval.py"]
        self.assertTrue(retrieval_rows)
        self.assertEqual(retrieval_rows[0]["role"], "read")
        self.assertEqual(retrieval_rows[0]["line_start"], 2)
        self.assertEqual(retrieval_rows[0]["line_end"], 4)
        self.assertFalse(any(row["path"] == "tests/test_retrieval.py" for row in payload["working_set"]))
        self.assertFalse(any(row["path"] == "docs/query_context.md" for row in payload["working_set"]))
        self.assertFalse(payload["contracts"])
        self.assertEqual(payload["impact"], {})
        self.assertTrue(payload["targeted_reads"])
        self.assertFalse(payload["snippets"])
        self.assertFalse(payload["test_targets"])
        self.assertTrue(any(item["label"] == "Retrieve ranked rows" for item in payload["followups"]))
        self.assertNotIn("symbols", payload)
        self.assertNotIn("code_links", payload)

        informative_payload = self.graph.query_context_payload("query_context project structure context retrieval", top_k=8)
        self.assertEqual(informative_payload["kind"], "code")
        self.assertEqual(informative_payload["query_mode"], "informative")
        self.assertNotIn("context", informative_payload)
        self.assertNotIn("intervention_targets", informative_payload)
        self.assertFalse(informative_payload["snippets"])
        self.assertFalse(informative_payload["edit_plan"])
        self.assertTrue(all(row["role"] == "read" for row in informative_payload["working_set"]))

        cleanup_payload = self.graph.query_context_payload("unused variable cleanup query_context noise", top_k=8, mode="cleanup")
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
        self.assertIn("## Research queries", cleanup_context)
        self.assertIn("## Summary", cleanup_context)

        default_payload = self.graph.query_context_payload("modifica unused cleanup query_context noise", top_k=8)
        self.assertEqual(default_payload["query_mode"], "informative")
        self.assertNotIn("intervention_targets", default_payload)
        self.assertFalse(default_payload["cleanup_candidates"])

    def test_query_context_scopes_retrieve_inside_requested_section_before_top_k_cutoff(self) -> None:
        query = "shared scoped query_context target"
        code_node = MemoryNode(
            id="function:scoped-code",
            type="Function",
            label="shared_scoped_code",
            text=query,
            canonical_key="src.scoped.shared_scoped_code",
            properties={"relative_path": "src/scoped.py", "qualified_name": "src.scoped.shared_scoped_code", "line_start": 3, "line_end": 7},
            salience=0.95,
        )
        test_node = MemoryNode(
            id="function:scoped-test",
            type="Function",
            label="test_shared_scoped_code",
            text=query,
            canonical_key="tests.test_scoped.test_shared_scoped_code",
            properties={"relative_path": "tests/test_scoped.py", "qualified_name": "tests.test_scoped.test_shared_scoped_code", "line_start": 10, "line_end": 16},
            salience=0.2,
        )
        docs_node = MemoryNode(
            id="fragment:scoped-docs",
            type="SourceFragment",
            label="docs/scoped.md#1",
            text=query,
            canonical_key="fragment:scoped-docs",
            properties={"relative_path": "docs/scoped.md", "line_start": 2, "line_end": 4},
            salience=0.1,
        )
        self.graph.add_node(code_node)
        self.graph.add_node(test_node)
        self.graph.add_node(docs_node)

        code_payload = self.graph.query_context_payload(query, top_k=1, scopes=["code"])
        test_payload = self.graph.query_context_payload(query, top_k=1, scopes=["test"])
        docs_payload = self.graph.query_context_payload(query, top_k=1, scopes=["docs"])

        self.assertTrue(any(row["path"] == "src/scoped.py" for row in code_payload["working_set"]))
        self.assertFalse(any(row["path"] == "tests/test_scoped.py" for row in code_payload["working_set"]))
        self.assertTrue(any(row["path"] == "tests/test_scoped.py" for row in test_payload["working_set"]))
        self.assertFalse(any(row["path"] == "src/scoped.py" for row in test_payload["working_set"]))
        self.assertTrue(any(item["location"] == "docs/scoped.md:2-4" for item in docs_payload["results"]))

    def test_query_context_ignores_single_token_install_false_positive(self) -> None:
        query = "install nome database non trovato database name not found installing"
        database_handler = MemoryNode(
            id="function:database-install-error",
            type="Function",
            label="handle_database_name_not_found",
            text="Handle database name not found while installing a database.",
            canonical_key="src.memory.database.handle_database_name_not_found",
            properties={
                "relative_path": "src/memory/database.py",
                "name": "handle_database_name_not_found",
                "qualified_name": "src.memory.database.handle_database_name_not_found",
                "line_start": 12,
                "line_end": 18,
            },
            salience=0.7,
        )
        install_launcher = MemoryNode(
            id="function:agent-install-launcher",
            type="Function",
            label="install",
            text="Install agent launcher commands and shell scripts.",
            canonical_key="src.agents.install.install",
            properties={
                "relative_path": "src/agents/install.py",
                "name": "install",
                "qualified_name": "src.agents.install.install",
                "line_start": 40,
                "line_end": 80,
            },
            salience=0.95,
        )
        self.graph.add_node(database_handler)
        self.graph.add_node(install_launcher)

        payload = self.graph.query_context_payload(query, top_k=8, scopes=["code"])

        self.assertEqual(payload["kind"], "code")
        self.assertTrue(any(row["path"] == "src/memory/database.py" for row in payload["working_set"]))
        self.assertFalse(any(row["path"] == "src/agents/install.py" for row in payload["working_set"]))
        self.assertTrue(any(item["id"] == "function:database-install-error" for item in payload["owner_candidates"]))
        self.assertFalse(any(item["id"] == "function:agent-install-launcher" for item in payload["owner_candidates"]))

        no_match = self.graph.query_context_payload("unmatched archive restore marker", top_k=8, scopes=["code"])
        self.assertFalse(no_match["results"])
        self.assertTrue(any(item["label"] == "Retrieve source rows" for item in no_match["followups"]))
        self.assertTrue(any(item["label"] == "Expand graph context" for item in no_match["followups"]))

    def test_query_context_keeps_structured_identifier_matches_actionable(self) -> None:
        target = MemoryNode(
            id="function:code-targeted-reads",
            type="Method",
            label="src.memory.services.retrieval.RetrievalEngine._code_targeted_reads",
            text="",
            canonical_key="src.memory.services.retrieval.RetrievalEngine._code_targeted_reads",
            properties={
                "relative_path": "src/memory/services/retrieval.py",
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
            properties={"relative_path": "src/memory/artifacts/compiler.py", "line_start": 540, "line_end": 856},
            salience=0.95,
        )
        self.graph.add_node(target)
        self.graph.add_node(broad_fragment)

        payload = self.graph.query_context_payload(
            "RetrievalEngine _code_targeted_reads SourceFragment owner symbol targeted reads",
            top_k=8,
            scopes=["code"],
        )

        self.assertTrue(any(row["path"] == "src/memory/services/retrieval.py" for row in payload["working_set"]))
        self.assertFalse(any(row["path"] == "src/memory/artifacts/compiler.py" for row in payload["working_set"]))
        self.assertTrue(any(item["id"] == "function:code-targeted-reads" for item in payload["owner_candidates"]))
        reads = [item for item in payload["targeted_reads"] if item["node_id"] == "function:code-targeted-reads"]
        self.assertTrue(reads)
        self.assertEqual(reads[0]["line_start"], 2024)
        self.assertEqual(reads[0]["line_end"], 2075)

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

    def test_free_search_ranks_chain_coverage_over_isolated_seed(self) -> None:
        capture = MemoryNode(
            id="function:payment-capture",
            type="Function",
            label="payment_capture",
            text="Payment capture workflow.",
            canonical_key="src.payments.payment_capture",
            properties={"relative_path": "src/payments.py", "line_start": 5, "line_end": 12},
        )
        repository = MemoryNode(
            id="class:order-repository",
            type="Class",
            label="OrderRepository",
            text="Order repository persistence for captured payments.",
            canonical_key="src.orders.OrderRepository",
            properties={"relative_path": "src/orders.py", "line_start": 20, "line_end": 35},
        )
        unrelated = MemoryNode(
            id="function:payment-logger",
            type="Function",
            label="payment_logger",
            text="Payment logger.",
            canonical_key="src.logs.payment_logger",
            properties={"relative_path": "src/logs.py", "line_start": 1, "line_end": 6},
        )
        for node in (capture, repository, unrelated):
            self.graph.add_node(node)
        self.graph.add_edge(MemoryEdge(id="edge:capture-repository", from_id=capture.id, to_id=repository.id, type="CALLS", weight=1.0))
        self.graph.add_edge(MemoryEdge(id="edge:capture-logger", from_id=capture.id, to_id=unrelated.id, type="CALLS", weight=1.0))

        payload = self.graph.query_memories_payload("payment capture order repository", top_k=4, max_depth=1)
        ranked_nodes = payload["ranked_nodes"]
        ids = [item["id"] for item in ranked_nodes]

        self.assertLess(ids.index(repository.id), ids.index(unrelated.id))
        self.assertGreater(ranked_nodes[ids.index(repository.id)]["reasons"]["coverage"], ranked_nodes[ids.index(unrelated.id)]["reasons"]["coverage"])

    def test_free_search_commands_share_top_ranked_pipeline(self) -> None:
        service = MemoryNode(
            id="function:query-context-service",
            type="Function",
            label="query_context_service",
            text="query context service targeted reads snippets",
            canonical_key="src.context.query_context_service",
            properties={"relative_path": "src/context.py", "line_start": 4, "line_end": 9},
        )
        source = MemoryNode(
            id="fragment:query-context-service",
            type="SourceFragment",
            label="src/context.py#query_context_service",
            text="def query_context_service(): return targeted_reads",
            canonical_key="fragment:query-context-service",
            properties={"relative_path": "src/context.py", "line_start": 4, "line_end": 9},
        )
        self.graph.add_node(service)
        self.graph.add_node(source)
        self.graph.add_edge(MemoryEdge(id="edge:service-source", from_id=service.id, to_id=source.id, type="EVIDENCED_BY", weight=1.0))

        query = "query context service targeted reads"
        graph_payload = self.graph.query_graph(query, top_k=4, max_depth=1, max_nodes=20, max_edges=20)
        memories = self.graph.query_memories(query, top_k=4, max_depth=1, limit=4)
        memories_payload = self.graph.query_memories_payload(query, top_k=4, max_depth=1, limit=4)
        explore = self.graph.query_explore(query, views=["owners", "code"], top_k=4, max_depth=1, limit=4)
        context_payload = self.graph.query_context_payload(query, top_k=4, max_depth=1, scopes=["code"])

        self.assertEqual(graph_payload["ranked_nodes"][0]["id"], service.id)
        self.assertEqual(memories[0]["id"], service.id)
        self.assertEqual(memories_payload["memories"][0]["id"], service.id)
        self.assertEqual(memories_payload["ranked_nodes"][0]["id"], service.id)
        self.assertEqual(memories_payload["nodes"][0]["id"], service.id)
        self.assertIn("trace_id", memories_payload)
        self.assertIn("seed_node_ids", memories_payload)
        self.assertEqual(explore["seed_nodes"][0]["id"], service.id)
        self.assertIn("context", explore)
        self.assertNotIn("## Follow-Up Queries", explore["context"])
        self.assertIn("followups", explore)
        self.assertTrue(any(item["id"] == service.id for item in context_payload["owner_candidates"]))
        self.assertTrue(any(item["node_id"] == service.id for item in context_payload["targeted_reads"]))

    def test_query_context_is_agent_ready_with_ids_sources_without_rendered_followups(self) -> None:
        plant = MemoryNode(
            id="fact:plant",

            type="Fact",
            label="Office plant watering",
            text="Office plant watering should happen every Monday.",
            canonical_key="office_plant_watering",
            salience=0.9,
            properties={"relative_path": "facilities.md", "line_start": 12, "line_end": 12},
        )
        source = MemoryNode(
            id="fragment:plant-note",

            type="SourceFragment",
            label="Facilities source note",
            text="Facilities source note says office plant watering is due every Monday.",
            canonical_key="office_plant_source_note",
            salience=0.8,
            properties={"metadata": {"source_path": "facilities.md", "start_line": 12, "end_line": 12}},
        )
        self.graph.add_node(plant)
        self.graph.add_node(source)
        self.graph.add_edge(
            MemoryEdge(
                id="edge:plant-source",

                from_id=plant.id,
                to_id=source.id,
                type="EVIDENCED_BY",
                weight=1.0,
                properties={"source_file": "facilities.md", "line_start": 12, "line_end": 12},
            )
        )

        context = self.graph.query_context("office plant watering", top_k=3, max_depth=1, max_items=6)

        self.assertIn("# REQL Context", context)
        self.assertNotIn("## Best matches", context)
        self.assertIn("## Results", context)
        self.assertIn("## Graph links", context)
        self.assertIn("## Research queries", context)
        self.assertIn("## Summary", context)
        self.assertIn("`fact:plant`", context)
        self.assertIn("facilities.md:12", context)
        self.assertNotIn("## Source evidence", context)
        self.assertIn("`fragment:plant-note`", context)
        self.assertIn("`edge:plant-source`", context)
        self.assertNotIn("## Follow-Up Queries", context)
        self.assertIn("inspect --node-id fact:plant --json", context)
        self.assertIn('RETRIEVE "office plant watering"', context)

        payload = self.graph.query_context_payload("office plant watering", top_k=3, max_depth=1, max_items=6)
        self.assertEqual(payload["kind"], "general")
        self.assertNotIn("context", payload)
        self.assertIn("results", payload)
        self.assertNotIn("best_matches", payload)
        self.assertNotIn("source_evidence", payload)
        self.assertNotIn("source_evidence_items", payload)
        self.assertTrue(any(item["label"] == "Inspect top node" for item in payload["followups"]))
        self.assertTrue(any('RETRIEVE "office plant watering"' in item["command"] for item in payload["followups"]))
        self.assertNotIn("working_set", payload)

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


if __name__ == "__main__":
    unittest.main()
