from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.config import load_config


class MarkdownIngestionTests(unittest.TestCase):
    def test_markdown_fixture_creates_structured_fragments_and_relations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "README.md").write_text(
                "\n".join(
                    [
                        "# README",
                        "",
                        "Intro with [site](https://example.com) and ![diagram](docs/diagram.png).",
                        "",
                        "## Installation",
                        "",
                        "- install package",
                        "- run command",
                        "",
                        "### CLI",
                        "",
                        "```python",
                        "print('hello')",
                        "```",
                        "",
                        "| Name | Value |",
                        "| --- | --- |",
                        "| a | b |",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                first = graph.compile_project(root)
                second = graph.compile_project(root)
                fragments = [node for node in graph.store.all_nodes() if node.type == "SourceFragment"]
                heading_concepts = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type == "Concept" and node.properties.get("fragment_type") == "heading"
                ]
                edges = graph.store.all_edges()
                types = {node.properties.get("fragment_type") for node in fragments}
                edge_types = {edge.type for edge in edges}
                section_paths = {node.properties.get("section_path") for node in fragments}

                self.assertEqual(first.run.files_changed, 1)
                self.assertEqual(second.run.files_changed, 0)
                self.assertIn("heading", types)
                self.assertIn("paragraph", types)
                self.assertIn("list", types)
                self.assertIn("code_block", types)
                self.assertIn("table", types)
                self.assertIn("README > Installation > CLI", section_paths)
                self.assertIn("CONTAINS_FRAGMENT", edge_types)
                self.assertIn("HAS_SECTION", edge_types)
                self.assertIn("LINKS_TO", edge_types)
                self.assertNotIn("EMBEDS_IMAGE", edge_types)
                self.assertIn("HAS_CODE_BLOCK", edge_types)
                self.assertIn("CONTAINS", edge_types)
                self.assertEqual(len(heading_concepts), 3)
                self.assertEqual({node.properties.get("name") for node in heading_concepts}, {"README", "Installation", "CLI"})
                self.assertTrue(all(node.properties.get("fragment_type") == "heading" for node in heading_concepts))
                self.assertEqual(len([node for node in graph.store.all_nodes() if node.type == "SourceArtifact"]), 1)
            finally:
                graph.close()

    def test_compile_ingests_documents_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "README.md").write_text("# README\n\nProject documentation should become source context.\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                node_types = {node.type for node in graph.store.all_nodes()}

                self.assertFalse(result.run.errors)
                self.assertIn("SourceFragment", node_types)
            finally:
                graph.close()

    def test_compile_ingest_documents_false_skips_document_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "notes.txt").write_text("Project documentation can be skipped by config.\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(
                    root,
                    parsing_options={
                        "compile": {"ingest_documents": False},
                    },
                )
                node_types = {node.type for node in graph.store.all_nodes()}
                artifact = next(node for node in graph.store.all_nodes() if node.type == "SourceArtifact")

                self.assertFalse(result.run.errors)
                self.assertNotIn("SourceFragment", node_types)
                self.assertEqual(artifact.properties.get("parser_name"), "document_ingest_disabled")
                self.assertEqual(artifact.properties.get("fragment_count"), 0)
            finally:
                graph.close()

    def test_compile_ignores_top_level_document_option_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "notes.txt").write_text("Top-level document options are not canonical.\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root, parsing_options={"ingest_documents": False})
                node_types = {node.type for node in graph.store.all_nodes()}

                self.assertFalse(result.run.errors)
                self.assertIn("SourceFragment", node_types)
            finally:
                graph.close()

    def test_compile_document_policy_can_skip_specific_document_type(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "README.md").write_text("# README\n\nMarkdown can be skipped by policy.\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(
                    root,
                    parsing_options={
                        "compile": {
                            "ingest_documents": True,
                            "documents": [{"format": "markdown", "extensions": [".md", ".markdown"], "ingest": False}],
                        },
                    },
                )
                node_types = {node.type for node in graph.store.all_nodes()}

                self.assertFalse(result.run.errors)
                self.assertNotIn("SourceFragment", node_types)
            finally:
                graph.close()

    def test_compile_pdf_parsing_depends_on_document_policy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "guide.pdf").write_bytes(b"%PDF-1.4\n% not a real pdf")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(
                    root,
                    parsing_options={
                        "compile": {
                            "ingest_documents": True,
                            "documents": [{"format": "pdf", "extensions": [".pdf"], "ingest": False}],
                        },
                    },
                )
                artifact = next(node for node in graph.store.all_nodes() if node.type == "SourceArtifact")

                self.assertFalse(result.run.errors)
                self.assertEqual(artifact.properties.get("parser_name"), "document_ingest_disabled")
            finally:
                graph.close()

    def test_compile_document_processor_adds_ranked_terms_raw_events_and_relations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "README.md").write_text(
                "# Payments\n\npayment service handles checkout approvals. payment service validates receipts. checkout approvals create receipt events.\n",
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                concepts = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type == "Concept" and node.properties.get("extractor") == "document_processor"
                ]
                raw_events = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type == "RawEvent" and node.properties.get("extractor") == "document_processor"
                ]
                processor_edges = [
                    edge
                    for edge in graph.store.all_edges()
                    if edge.properties.get("extractor") == "document_processor"
                ]
                edge_types = {edge.type for edge in processor_edges}
                semantic_keys = {node.properties.get("semantic_key") for node in concepts}

                self.assertFalse(result.run.errors)
                self.assertIn("payment_service", semantic_keys)
                self.assertTrue(raw_events)
                self.assertIn("MENTIONS", edge_types)
                self.assertIn("EVIDENCED_BY", edge_types)
                self.assertIn("DERIVED_FROM", edge_types)
                self.assertIn("CO_OCCURS_WITH", edge_types)
                self.assertTrue(all("rank" in node.properties for node in concepts))
                self.assertTrue(all("term_frequency" in node.properties for node in concepts))
                self.assertTrue(all("raw_event_count" in node.properties for node in concepts))
            finally:
                graph.close()

    def test_document_processor_preserves_unicode_terms(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "README.md").write_text(
                "# \u65e5\u672c\u8a9e\n\n"
                "\u6771\u4eac \u652f\u6255\u3044 \u51e6\u7406 \u306f \u9818\u53ce\u66f8 \u3092 \u751f\u6210\u3057\u307e\u3059\u3002"
                "\u6771\u4eac \u652f\u6255\u3044 \u51e6\u7406 \u306f \u76e3\u67fb \u30a4\u30d9\u30f3\u30c8 \u3092 \u8a18\u9332\u3057\u307e\u3059\u3002\n",
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                semantic_keys = {
                    node.properties.get("semantic_key")
                    for node in graph.store.all_nodes()
                    if node.type == "Concept" and node.properties.get("extractor") == "document_processor"
                }

                self.assertFalse(result.run.errors)
                self.assertTrue(any(key and "\u6771\u4eac" in str(key) for key in semantic_keys))
            finally:
                graph.close()

    def test_document_processor_links_terms_to_code_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def compile_project():\n    return 'ok'\n", encoding="utf-8")
            (root / "README.md").write_text(
                "# README\n\nlocal processor documents compile_project and project compile events.\n",
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                references = [
                    edge
                    for edge in graph.store.get_edges(type_="REFERENCES", limit=100)
                    if edge.properties.get("extractor") == "document_processor"
                ]
                target_nodes = {edge.to_id: graph.get_node(edge.to_id) for edge in references}

                self.assertFalse(result.run.errors)
                self.assertTrue(any(node and node.type == "Function" and node.properties.get("name") == "compile_project" for node in target_nodes.values()))
            finally:
                graph.close()

    def test_document_processor_archives_stale_terms_on_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            readme = root / "README.md"
            readme.write_text("# README\n\nalpha ledger pipeline records alpha receipts.\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                first = graph.compile_project(root)
                readme.write_text("# README\n\ngamma ledger pipeline records gamma receipts.\n", encoding="utf-8")
                second = graph.compile_project(root)
                alpha_nodes = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type == "Concept"
                    and node.properties.get("extractor") == "document_processor"
                    and str(node.properties.get("semantic_key", "")).startswith("alpha")
                ]
                gamma_nodes = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type == "Concept"
                    and node.properties.get("extractor") == "document_processor"
                    and str(node.properties.get("semantic_key", "")).startswith("gamma")
                    and node.status != "archived"
                ]

                self.assertFalse(first.run.errors)
                self.assertFalse(second.run.errors)
                self.assertTrue(alpha_nodes)
                self.assertTrue(all(node.status == "archived" for node in alpha_nodes))
                self.assertTrue(gamma_nodes)
            finally:
                graph.close()

    def test_query_context_suppresses_raw_document_event_noise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "info.md").write_text("written by the docs crew\n\n\nreader note: fresh basil improves soup\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                context = graph.query_context("soup", top_k=12, max_depth=2, max_items=8)

                self.assertFalse(result.run.errors)
                self.assertIn("reader note: fresh basil improves soup", context)
                self.assertIn("document_term:", context)
                self.assertNotIn("[RawEvent]", context)
                self.assertNotIn("document_raw_event:", context)
                self.assertNotIn("written by the docs crew", context)
                self.assertLessEqual(context.count("reader note: fresh basil improves soup"), 2)
            finally:
                graph.close()

    def test_config_supports_document_policy_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "conf.yaml"
            path.write_text(
                "\n".join(
                    [
                        "compile:",
                        "  documents:",
                        '    - {"format": "markdown", "extensions": [".md", ".markdown"], "ingest": true}',
                        '    - {"format": "pdf", "extensions": [".pdf"], "ingest": false}',
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual(
                config.compile.documents,
                [
                    {"format": "markdown", "extensions": [".md", ".markdown"], "ingest": True},
                    {"format": "pdf", "extensions": [".pdf"], "ingest": False},
                ],
            )

            invalid_options = [
                ("misspelled ingest", '"inj" + "est"', '{"format": "markdown", "extensions": [".md"], "injest": true}'),
                ("removed agent policy", '"sub" + "agent"', '{"format": "markdown", "extensions": [".md"], "ingest": true, "subagent": true}'),
            ]
            for label, _source_hint, document_policy in invalid_options:
                with self.subTest(label=label):
                    path.write_text(
                        "\n".join(
                            [
                                "compile:",
                                "  documents:",
                                f"    - {document_policy}",
                            ]
                        ),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(ValueError, "Unknown compile.documents\\[1\\] option"):
                        load_config(path)

    def test_compile_links_document_fragments_to_code_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def compile_project():\n    return 'ok'\n", encoding="utf-8")
            (root / "README.md").write_text("# README\n\nCall compile_project after editing docs.\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                references = [
                    edge
                    for edge in graph.store.get_edges(type_="REFERENCES", limit=100)
                    if edge.properties.get("extractor") == "document_code_linker"
                ]
                target_nodes = {edge.to_id: graph.get_node(edge.to_id) for edge in references}

                self.assertFalse(result.run.errors)
                self.assertTrue(any(node and node.type == "Function" and node.properties.get("name") == "compile_project" for node in target_nodes.values()))
            finally:
                graph.close()

    def test_document_code_linker_filters_and_bounds_matches(self) -> None:
        scenarios = [
            {
                "name": "ignores low-signal variable terms",
                "app": "project = 'demo'\n\ndef compile_project():\n    return project\n",
                "readme": "# README\n\nThe project uses compile_project.\n",
                "assert": "function_not_variable",
            },
            {
                "name": "bounds links per fragment",
                "app": "\n".join(f"def compile_project_{index}():\n    return {index}\n" for index in range(40)),
                "readme": "# README\n\n" + " ".join(f"compile_project_{index}" for index in range(40)) + "\n",
                "assert": "bounded_count",
            },
            {
                "name": "rejects generic heading matches",
                "app": "def usage():\n    return 'generic'\n",
                "readme": "# Usage\n\nFollow the guide.\n",
                "assert": "no_references",
            },
        ]
        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td) / "project"
                    root.mkdir()
                    (root / "app.py").write_text(str(scenario["app"]), encoding="utf-8")
                    (root / "README.md").write_text(str(scenario["readme"]), encoding="utf-8")
                    graph = MemoryGraph.open(Path(td) / "memory.reql")
                    try:
                        result = graph.compile_project(root)
                        references = [
                            edge
                            for edge in graph.store.get_edges(type_="REFERENCES", limit=1000)
                            if edge.properties.get("extractor") == "document_code_linker"
                        ]

                        self.assertFalse(result.run.errors)
                        if scenario["assert"] == "function_not_variable":
                            target_nodes = [graph.get_node(edge.to_id) for edge in references]
                            self.assertTrue(
                                any(node and node.type == "Function" and node.properties.get("name") == "compile_project" for node in target_nodes)
                            )
                            self.assertFalse(any(node and node.type == "Variable" and node.properties.get("name") == "project" for node in target_nodes))
                        elif scenario["assert"] == "bounded_count":
                            self.assertEqual(len(references), 8)
                        else:
                            self.assertFalse(references)
                    finally:
                        graph.close()

    def test_document_code_linker_archives_stale_links_on_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def compile_project():\n    return 'ok'\n", encoding="utf-8")
            readme = root / "README.md"
            readme.write_text("# README\n\nCall compile_project after editing docs.\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                graph.compile_project(root)
                readme.write_text("# README\n\nNo explicit code symbol here.\n", encoding="utf-8")
                result = graph.compile_project(root)
                active_references = [
                    edge
                    for edge in graph.store.get_edges(type_="REFERENCES", limit=100)
                    if edge.properties.get("extractor") == "document_code_linker" and edge.properties.get("status") != "archived"
                ]

                self.assertFalse(result.run.errors)
                self.assertFalse(active_references)
            finally:
                graph.close()


if __name__ == "__main__":
    unittest.main()
