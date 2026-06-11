from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from api import MemoryGraph
from memory.artifacts.fingerprint import artifact_id, normalize_path, project_id
from memory.domain.models import MemoryNode


class CodeGraphCompilationTests(unittest.TestCase):
    def test_compile_project_runs_without_external_extraction_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                graph.compile_project(root)
            finally:
                graph.close()

    def test_graph_compilation_creates_symbols_relations_and_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text(
                "\n".join(
                    [
                        "import os",
                        "",
                        "def helper():",
                        "    return os.getcwd()",
                        "",
                        "def main():",
                        "    return helper()",
                        "",
                        "class Runner:",
                        "    def run(self):",
                        "        return main()",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                first = graph.compile_project(root)
                second = graph.compile_project(root)
                node_types = {node.type for node in graph.store.all_nodes()}
                edge_types = {edge.type for edge in graph.store.all_edges()}

                self.assertEqual(first.run.files_changed, 1)
                self.assertEqual(second.run.files_changed, 0)
                self.assertIn("Module", node_types)
                self.assertIn("Function", node_types)
                self.assertIn("Class", node_types)
                self.assertIn("Method", node_types)
                self.assertIn("Import", node_types)
                self.assertIn("DEFINES", edge_types)
                self.assertIn("CONTAINS", edge_types)
                self.assertIn("IMPORTS", edge_types)
                self.assertIn("IMPORTS_FROM", edge_types)
                self.assertIn("CALLS", edge_types)
                self.assertIn("METHOD", edge_types)
                self.assertNotIn("CONTAINS_SYMBOL", edge_types)
                self.assertNotIn("LOCATED_IN", edge_types)
                self.assertNotIn("EXTENDS", edge_types)
                self.assertNotIn("TYPE_HINTS", edge_types)

                functions = [node for node in graph.store.all_nodes() if node.type == "Function"]
                self.assertEqual(sorted(node.properties["name"] for node in functions), ["helper", "main"])
                call_edges = graph.store.get_edges(type_="CALLS", limit=20)
                targets = {graph.get_node(edge.to_id).properties.get("name") for edge in call_edges if graph.get_node(edge.to_id)}
                self.assertIn("helper", targets)
                self.assertIn("main", targets)

                match = graph.query("MATCH (a:SourceArtifact)-[:DEFINES]->(f:Function) RETURN a.path,f.name,f.start_line")
                names = {row["f.name"] for row in match.rows}
                self.assertIn("helper", names)
                self.assertIn("main", names)

                call_match = graph.query("MATCH (f:Function)-[:CALLS]->(g) RETURN f.name,g.name")
                self.assertTrue(any(row["f.name"] == "main" and row["g.name"] == "helper" for row in call_match.rows))
                self.assertFalse(any("os.getcwd" in str(node.properties.get("unresolved_calls", [])) for node in functions))

                method_edges = graph.store.get_edges(type_="METHOD", limit=20)
                method_pairs = {
                    (graph.get_node(edge.from_id).properties.get("name"), graph.get_node(edge.to_id).properties.get("name"))
                    for edge in method_edges
                    if graph.get_node(edge.from_id) and graph.get_node(edge.to_id)
                }
                self.assertIn(("Runner", "run"), method_pairs)
                imports_from_edges = graph.store.get_edges(type_="IMPORTS_FROM", limit=20)
                self.assertTrue(any(graph.get_node(edge.to_id).type == "Dependency" for edge in imports_from_edges if graph.get_node(edge.to_id)))

                source_fragments = [node for node in graph.store.all_nodes() if node.type == "SourceFragment"]
                self.assertTrue(any(node.properties.get("symbol_name") == "helper" for node in source_fragments))
                helper_node = next(node for node in functions if node.properties.get("name") == "helper")
                helper_fragment = next(node for node in source_fragments if node.properties.get("symbol_name") == "helper")
                evidence_edges = graph.store.get_edges(from_id=helper_node.id, to_id=helper_fragment.id, type_="EVIDENCED_BY", limit=10)
                self.assertEqual(len(evidence_edges), 1)

                retrieved = graph.query('RETRIEVE "helper" LIMIT 8 RETURN type,source_for,relation,text')
                self.assertTrue(
                    any(
                        row["type"] == "SourceFragment"
                        and row["source_for"] == helper_node.id
                        and row["relation"] == "EVIDENCED_BY"
                        and "def helper" in row["text"]
                        for row in retrieved.rows
                    )
                )

                query_graph = graph.query_graph("helper", top_k=5, max_depth=1, max_nodes=20, max_edges=30)
                self.assertTrue(
                    any(
                        any(ref.get("node_id") == helper_node.id and ref.get("relation") == "EVIDENCED_BY" for ref in source.get("source_for", []))
                        for source in query_graph["sources"]
                    )
                )
                self.assertIn("EVIDENCED_BY incoming", query_graph["context"])
            finally:
                graph.close()

    def test_repeated_compile_does_not_duplicate_code_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def once():\n    return 1\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                for _ in range(3):
                    graph.compile_project(root)
                functions = [node for node in graph.store.all_nodes() if node.type == "Function"]
                self.assertEqual(len(functions), 1)
                self.assertEqual(functions[0].properties["name"], "once")
            finally:
                graph.close()

    def test_code_fragment_identity_survives_line_shifts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            source = root / "app.py"
            source.write_text("def stable():\n    return 1\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                graph.compile_project(root)
                before = {
                    node.properties.get("symbol_qualified_name"): node.id
                    for node in graph.store.all_nodes()
                    if node.type == "SourceFragment"
                }

                source.write_text("# inserted header\n\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
                graph.compile_project(root)
                after = {
                    node.properties.get("symbol_qualified_name"): node.id
                    for node in graph.store.all_nodes()
                    if node.type == "SourceFragment" and node.status != "archived"
                }

                self.assertEqual(before["app.stable"], after["app.stable"])
            finally:
                graph.close()

    def test_compile_links_symbols_using_persisted_node_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text("def stale_symbol():\n    return 1\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                project_id_ = project_id(normalize_path(root))
                artifact_id_ = artifact_id(project_id_, "app.py")
                stale_symbol = MemoryNode(
                    id="code-symbol:stale-id",

                    type="Function",
                    label="app.stale_symbol",
                    canonical_key=f"{artifact_id_}:function:app.stale_symbol",
                    properties={"project_id": project_id_, "artifact_id": artifact_id_, "name": "stale_symbol"},
                )
                graph.add_node(stale_symbol)

                result = graph.compile_project(root)

                self.assertFalse(result.run.errors)
                persisted = graph.get_node("code-symbol:stale-id")
                self.assertIsNotNone(persisted)
                defines_edges = [
                    edge
                    for edge in graph.store.get_edges(type_="DEFINES", limit=100)
                    if edge.to_id == "code-symbol:stale-id"
                ]
                self.assertTrue(defines_edges)
            finally:
                graph.close()

    def test_compile_python_file_with_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "bom.py").write_text("def clean():\n    return 1\n", encoding="utf-8-sig")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)
                functions = [node for node in graph.store.all_nodes() if node.type == "Function"]
                self.assertEqual([node.properties["name"] for node in functions], ["clean"])
            finally:
                graph.close()

    def test_external_none_type_hint_does_not_create_generic_code_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "typed.py").write_text(
                "\n".join(
                    [
                        "def noop() -> None:",
                        "    return None",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                graph.compile_project(root)
                code_symbols = [node for node in graph.store.all_nodes() if node.type == "CodeSymbol"]
                labels = {str(node.label) for node in code_symbols}
                names = {str(node.properties.get("name")) for node in code_symbols}
                self.assertNotIn("None", labels)
                self.assertNotIn("None", names)
            finally:
                graph.close()

    def test_compile_mode_builds_deterministic_technical_graph_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "config.toml").write_text("[tool]\nname = 'demo'\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_app.py").write_text(
                "\n".join(
                    [
                        "from typing import Protocol",
                        "from pydantic import BaseModel",
                        "",
                        "VALUE = 1",
                        "",
                        "class ServiceProtocol(Protocol):",
                        "    def run(self) -> int:",
                        "        ...",
                        "",
                        "class Payload(BaseModel):",
                        "    name: str",
                        "",
                        "def route(path):",
                        "    def wrap(fn):",
                        "        return fn",
                        "    return wrap",
                        "",
                        "@route('/items')",
                        "def handler() -> Payload:",
                        "    local = VALUE",
                        "    if local < 0:",
                        "        raise ValueError('bad')",
                        "    return Payload()",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                graph.compile_project(root)
                node_types = {node.type for node in graph.store.all_nodes()}
                edge_types = {edge.type for edge in graph.store.all_edges()}

                for expected in {"Project", "Directory", "File", "Module", "Interface", "Function", "Variable", "Dependency", "Endpoint", "Schema", "Config", "Test"}:
                    self.assertIn(expected, node_types)
                self.assertNotIn("Route", node_types)
                code_symbol_names = {str(node.properties.get("name")) for node in graph.store.all_nodes() if node.type == "CodeSymbol"}
                self.assertNotIn("fn", code_symbol_names)
                for expected in {"CONTAINS", "DEFINES", "IMPORTS", "DEPENDS_ON", "READS", "WRITES", "RETURNS", "RAISES", "DECORATED_BY", "HANDLES_ROUTE"}:
                    self.assertIn(expected, edge_types)
                self.assertIn("IMPORTS_FROM", edge_types)

                technical_edges = [
                    edge
                    for edge in graph.store.all_edges()
                    if edge.properties.get("mode") == "compile" and edge.properties.get("is_technical") is True
                ]
                self.assertTrue(technical_edges)
                for edge in technical_edges:
                    self.assertEqual(edge.confidence, 1.0)
                    self.assertEqual(edge.properties.get("source_id"), edge.from_id)
                    self.assertEqual(edge.properties.get("target_id"), edge.to_id)
                    self.assertEqual(edge.properties.get("type"), edge.type)
                    self.assertIn("source_file", edge.properties)
                    self.assertIn("extractor", edge.properties)
                    self.assertIn("evidence", edge.properties)
                    self.assertEqual(edge.properties.get("is_semantic"), False)
            finally:
                graph.close()

    def test_compile_records_context_scope_for_query_context_filters(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            src_dir = root / "src"
            tests_dir = root / "tests"
            docs_dir = root / "docs"
            root.mkdir()
            src_dir.mkdir(parents=True)
            tests_dir.mkdir()
            docs_dir.mkdir()
            (src_dir / "app.py").write_text(
                "def feature_marker():\n    return 'shared_scope_marker'\n",
                encoding="utf-8",
            )
            (tests_dir / "test_app.py").write_text(
                "def test_feature_marker():\n    assert 'shared_scope_marker'\n",
                encoding="utf-8",
            )
            (docs_dir / "guide.md").write_text(
                "# Soup Guide\n\nshared_scope_marker soup docs evidence.\n",
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)

                artifact_scopes = {
                    node.properties.get("relative_path"): node.properties.get("context_scope")
                    for node in graph.store.all_nodes()
                    if node.type == "SourceArtifact"
                }
                self.assertEqual(artifact_scopes["src/app.py"], "code")
                self.assertEqual(artifact_scopes["tests/test_app.py"], "test")
                self.assertEqual(artifact_scopes["docs/guide.md"], "docs")

                scoped_nodes = [
                    node
                    for node in graph.store.all_nodes()
                    if node.properties.get("relative_path") in {"src/app.py", "tests/test_app.py", "docs/guide.md"}
                    and node.type in {"File", "SourceFragment", "Function", "Module", "Concept", "RawEvent", "Test"}
                ]
                self.assertTrue(scoped_nodes)
                for node in scoped_nodes:
                    rel = node.properties.get("relative_path")
                    expected = "test" if rel == "tests/test_app.py" else "docs" if rel == "docs/guide.md" else "code"
                    self.assertEqual(node.properties.get("context_scope"), expected)

                code_payload = graph.query_context_payload("shared_scope_marker", top_k=1, scopes=["code"])
                test_payload = graph.query_context_payload("shared_scope_marker", top_k=1, scopes=["test"])
                docs_payload = graph.query_context_payload("shared_scope_marker", top_k=1, scopes=["docs"])

                self.assertTrue(any(row["path"] == "src/app.py" for row in code_payload["working_set"]))
                self.assertTrue(any(row["path"] == "tests/test_app.py" for row in test_payload["working_set"]))
                self.assertTrue(any(item["location"].startswith("docs/guide.md") for item in docs_payload["results"]))
            finally:
                graph.close()

    def test_recognized_non_python_language_builds_tree_sitter_code_graph(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "Main.java").write_text("class Main { void run() {} }\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                graph.compile_project(root)
                node_types = {node.type for node in graph.store.all_nodes()}
                edge_types = {edge.type for edge in graph.store.all_edges()}

                self.assertIn("Project", node_types)
                self.assertIn("File", node_types)
                self.assertIn("SourceArtifact", node_types)
                self.assertIn("Module", node_types)
                self.assertIn("Class", node_types)
                self.assertIn("Method", node_types)
                self.assertIn("CONTAINS", edge_types)
                self.assertIn("DEFINES", edge_types)
                self.assertIn("SourceFragment", node_types)
                classes = [node for node in graph.store.all_nodes() if node.type == "Class"]
                methods = [node for node in graph.store.all_nodes() if node.type == "Method"]
                self.assertIn("Main", {node.properties.get("name") for node in classes})
                self.assertIn("run", {node.properties.get("name") for node in methods})
            finally:
                graph.close()

    def test_unresolved_and_builtin_calls_do_not_create_callsite_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text(
                "\n".join(
                    [
                        "def main(items):",
                        "    print(len(items))",
                        "    return unknown_helper(items)",
                        "",
                        "from unittest.mock import Mock",
                        "",
                        "def imported_test_helper():",
                        "    return Mock()",
                        "",
                        "def missing_test_helper():",
                        "    return MagicMock()",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)
                main = next(node for node in graph.store.all_nodes() if node.type == "Function" and node.properties.get("name") == "main")
                unresolved = main.properties.get("unresolved_calls", [])
                self.assertEqual([item["target"] for item in unresolved], ["unknown_helper"])
                self.assertNotIn("print", str(unresolved))
                self.assertNotIn("len", str(unresolved))
                imported_helper = next(node for node in graph.store.all_nodes() if node.type == "Function" and node.properties.get("name") == "imported_test_helper")
                missing_helper = next(node for node in graph.store.all_nodes() if node.type == "Function" and node.properties.get("name") == "missing_test_helper")
                self.assertEqual(imported_helper.properties.get("unresolved_calls", []), [])
                self.assertEqual([item["target"] for item in missing_helper.properties.get("unresolved_calls", [])], ["MagicMock"])
            finally:
                graph.close()

    def test_compile_records_unused_code_findings_for_queries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text(
                "\n".join(
                    [
                        "import os",
                        "import sys",
                        "",
                        "def used():",
                        "    local_used = 1",
                        "    local_unused = 2",
                        "    print(os.getcwd(), local_used)",
                        "    return local_used",
                        "",
                        "def unused():",
                        "    return 1",
                        "",
                        "used()",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)

                findings = [node for node in graph.store.all_nodes() if node.type == "StaticAnalysisFinding"]
                finding_types = {node.properties.get("finding_type") for node in findings}
                self.assertIn("unused_variable", finding_types)
                self.assertIn("unused_import", finding_types)
                self.assertIn("possibly_unused_function", finding_types)
                self.assertTrue(any(node.properties.get("symbol_name") == "local_unused" for node in findings))
                self.assertTrue(any(node.properties.get("symbol_name") == "sys" for node in findings))
                self.assertTrue(any(node.properties.get("symbol_name") == "unused" for node in findings))
                self.assertFalse(any(node.properties.get("symbol_name") == "local_used" for node in findings))
                self.assertFalse(any(node.properties.get("symbol_name") == "os" for node in findings))
                self.assertFalse(any(node.properties.get("symbol_name") == "used" for node in findings))
                self.assertTrue(all(node.properties.get("evidence_scope") in {"local_artifact", "public_api_local_artifact"} for node in findings))
                self.assertTrue(all(node.properties.get("cleanup_priority") in {"high", "medium", "low"} for node in findings))
                self.assertTrue(all(node.properties.get("cleanup_rank") in {1, 2, 3} for node in findings))
                self.assertTrue(all("confidence" in node.properties for node in findings))
                self.assertTrue(all(node.properties.get("removal_safety") in {"safe", "validate", "risky"} for node in findings))
                self.assertTrue(all("removal_reason" in node.properties for node in findings))
                self.assertTrue(all("validation_reason" in node.properties for node in findings))
                self.assertTrue(all(isinstance(node.properties.get("blocking_signals"), list) for node in findings))

                query = graph.query('FINDINGS WHERE finding_type = "unused_variable" RETURN symbol_name,relative_path,line_start')
                self.assertTrue(any(row["symbol_name"] == "local_unused" for row in query.rows))

                linked = graph.query("MATCH (v:Variable)-[:HAS_FINDING]->(f:StaticAnalysisFinding) RETURN v.name,f.finding_type")
                self.assertTrue(any(row["v.name"] == "local_unused" and row["f.finding_type"] == "unused_variable" for row in linked.rows))
            finally:
                graph.close()

    def test_unused_findings_treat_defaults_and_exceptions_as_usage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "config.py").write_text("class ConfigError(Exception):\n    pass\n", encoding="utf-8")
            (root / "app.py").write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from config import ConfigError",
                        "import sys",
                        "",
                        "DEFAULT_TIMEOUT = 1",
                        "",
                        "def used(path: Path = Path('.'), timeout: int = DEFAULT_TIMEOUT):",
                        "    try:",
                        "        return str(path), timeout",
                        "    except ConfigError as exc:",
                        "        return 'error'",
                        "",
                        "used()",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)
                findings = [node for node in graph.store.all_nodes() if node.type == "StaticAnalysisFinding"]

                def has_unused_import(symbol_name: str) -> bool:
                    return any(
                        node.properties.get("finding_type") == "unused_import"
                        and node.properties.get("symbol_name") == symbol_name
                        and node.properties.get("relative_path") == "app.py"
                        for node in findings
                    )

                self.assertFalse(has_unused_import("Path"))
                self.assertFalse(has_unused_import("ConfigError"))
                self.assertTrue(has_unused_import("sys"))
                sys_findings = [node for node in findings if node.properties.get("finding_type") == "unused_import" and node.properties.get("symbol_name") == "sys"]
                self.assertTrue(sys_findings)
                self.assertTrue(all(node.properties.get("removal_safety") == "safe" for node in sys_findings))
                self.assertTrue(all(node.properties.get("cleanup_priority") == "high" for node in sys_findings))
            finally:
                graph.close()

    def test_framework_like_method_names_are_not_globally_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "app.py").write_text(
                "\n".join(
                    [
                        "class Worker:",
                        "    def setUp(self):",
                        "        return 1",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)
                findings = [node for node in graph.store.all_nodes() if node.type == "StaticAnalysisFinding"]
                setup_findings = [
                    node
                    for node in findings
                    if node.properties.get("finding_type") == "possibly_unused_method"
                    and node.properties.get("symbol_name") == "setUp"
                ]
                self.assertTrue(setup_findings)
                self.assertTrue(all(node.properties.get("evidence_scope") == "public_api_local_artifact" for node in setup_findings))
                self.assertTrue(all(node.properties.get("removal_safety") == "risky" for node in setup_findings))
                self.assertTrue(all("framework_lifecycle" in node.properties.get("blocking_signals", []) for node in setup_findings))
            finally:
                graph.close()

    def test_unused_code_findings_avoid_annotation_reexport_and_test_noise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            package = root / "pkg"
            tests_dir = root / "tests"
            package.mkdir(parents=True)
            tests_dir.mkdir()
            (package / "__init__.py").write_text(
                "\n".join(
                    [
                        "from .models import PublicThing",
                        "",
                        "__all__ = ['PublicThing']",
                    ]
                ),
                encoding="utf-8",
            )
            (package / "models.py").write_text(
                "\n".join(
                    [
                        "class PublicThing:",
                        "    pass",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "app.py").write_text(
                "\n".join(
                    [
                        "from __future__ import annotations",
                        "from dataclasses import dataclass",
                        "from typing import Any",
                        "import os",
                        "import sys",
                        "",
                        "@dataclass(frozen=True, slots=True)",
                        "class Payload:",
                        "    text: str",
                        "    metadata: dict[str, Any]",
                        "",
                        "def used(context: dict[str, Any]) -> str:",
                        "    local_used = 1",
                        "    local_unused = 2",
                        "    forwarded = context",
                        "    holder = context",
                        "    text_value = holder.get('text')",
                        "    print(os.getcwd(), local_used)",
                        "    consumed = consume(value=forwarded)",
                        "    items = [consumed]",
                        "    for item in items:",
                        "        final = item",
                        "    counts: dict[str, int] = {}",
                        "    counts[str(text_value)] += 1",
                        "    return final + str(text_value)",
                        "",
                        "def consume(value: Any) -> str:",
                        "    return str(value)",
                        "",
                        "used({'text': 'ok'})",
                    ]
                ),
                encoding="utf-8",
            )
            (tests_dir / "test_app.py").write_text(
                "\n".join(
                    [
                        "import unittest",
                        "",
                        "class AppTests(unittest.TestCase):",
                        "    def test_feature(self):",
                        "        local_unused = 1",
                        "        self.assertTrue(True)",
                    ]
                ),
                encoding="utf-8",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)
                findings = [node for node in graph.store.all_nodes() if node.type == "StaticAnalysisFinding"]

                def has_finding(finding_type: str, symbol_name: str, relative_path: str | None = None) -> bool:
                    return any(
                        node.properties.get("finding_type") == finding_type
                        and node.properties.get("symbol_name") == symbol_name
                        and (relative_path is None or node.properties.get("relative_path") == relative_path)
                        for node in findings
                    )

                self.assertTrue(has_finding("unused_import", "sys", "app.py"))
                self.assertTrue(has_finding("unused_variable", "local_unused", "app.py"))
                self.assertFalse(has_finding("unused_import", "annotations", "app.py"))
                self.assertFalse(has_finding("unused_import", "dataclass", "app.py"))
                self.assertFalse(has_finding("unused_import", "Any", "app.py"))
                self.assertFalse(has_finding("unused_variable", "text", "app.py"))
                self.assertFalse(has_finding("unused_variable", "metadata", "app.py"))
                self.assertFalse(has_finding("unused_variable", "forwarded", "app.py"))
                self.assertFalse(has_finding("unused_variable", "holder", "app.py"))
                self.assertFalse(has_finding("unused_variable", "text_value", "app.py"))
                self.assertFalse(has_finding("unused_variable", "consumed", "app.py"))
                self.assertFalse(has_finding("unused_variable", "items", "app.py"))
                self.assertFalse(has_finding("unused_variable", "counts", "app.py"))
                self.assertFalse(has_finding("unused_variable", "str", "app.py"))
                self.assertFalse(has_finding("unused_import", "PublicThing", "pkg/__init__.py"))
                re_exports = graph.query("MATCH (m:Module)-[:RE_EXPORTS]->(i:Import) RETURN m.name,i.name")
                self.assertTrue(any(row["m.name"] == "pkg" and row["i.name"] == "PublicThing" for row in re_exports.rows))
                re_export_imports = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type == "Import"
                    and node.properties.get("relative_path") == "pkg/__init__.py"
                    and node.properties.get("name") == "PublicThing"
                ]
                self.assertTrue(re_export_imports)
                self.assertTrue(all(node.properties.get("is_re_export") is True for node in re_export_imports))
                self.assertFalse(any(node.properties.get("finding_type", "").startswith("possibly_unused") and node.properties.get("relative_path") == "tests/test_app.py" for node in findings))

                public_candidates = [
                    node
                    for node in findings
                    if node.properties.get("finding_type") == "possibly_unused_class"
                    and node.properties.get("symbol_name") == "PublicThing"
                ]
                self.assertTrue(public_candidates)
                self.assertTrue(all(node.properties.get("cleanup_priority") == "low" for node in public_candidates))
                self.assertTrue(all(node.properties.get("evidence_scope") == "public_api_local_artifact" for node in public_candidates))
                self.assertTrue(all(node.properties.get("removal_safety") == "risky" for node in public_candidates))
                self.assertTrue(all("public_api" in node.properties.get("blocking_signals", []) for node in public_candidates))
                test_variables = [
                    node
                    for node in findings
                    if node.properties.get("finding_type") == "unused_variable"
                    and node.properties.get("relative_path") == "tests/test_app.py"
                ]
                self.assertTrue(test_variables)
                self.assertTrue(all(node.properties.get("cleanup_priority") == "low" for node in test_variables))
                self.assertTrue(all(node.properties.get("evidence_scope") == "test_local_artifact" for node in test_variables))
                self.assertTrue(all(node.properties.get("removal_safety") == "validate" for node in test_variables))
                safe_imports = [
                    node
                    for node in findings
                    if node.properties.get("finding_type") == "unused_import"
                    and node.properties.get("symbol_name") == "sys"
                    and node.properties.get("relative_path") == "app.py"
                ]
                self.assertTrue(safe_imports)
                self.assertTrue(all(node.properties.get("removal_safety") == "safe" for node in safe_imports))
                default_ordered = graph.query("FINDINGS RETURN symbol_name,cleanup_priority,cleanup_rank LIMIT 5")
                self.assertEqual(default_ordered.rows[0]["cleanup_priority"], "high")
                self.assertEqual(default_ordered.rows[0]["cleanup_rank"], 3)
                priority_ordered = graph.query("FINDINGS RETURN symbol_name,cleanup_priority ORDER BY cleanup_priority LIMIT 5")
                self.assertEqual(priority_ordered.rows[0]["cleanup_priority"], "high")
            finally:
                graph.close()

    def test_findings_query_hides_archived_cleanup_findings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            app = root / "app.py"
            app.write_text("import sys\n\nprint('ok')\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                first = graph.compile_project(root)
                self.assertFalse(first.run.errors)
                query = graph.query('FINDINGS WHERE finding_type = "unused_import" RETURN symbol_name,status')
                self.assertTrue(any(row["symbol_name"] == "sys" and row["status"] == "active" for row in query.rows))

                app.write_text("import sys\n\nprint(sys.version)\n", encoding="utf-8")
                second = graph.compile_project(root)
                self.assertFalse(second.run.errors)
                query = graph.query('FINDINGS WHERE finding_type = "unused_import" RETURN symbol_name,status')
                self.assertFalse(any(row["symbol_name"] == "sys" for row in query.rows))
                archived = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type == "StaticAnalysisFinding"
                    and node.properties.get("symbol_name") == "sys"
                    and node.status == "archived"
                ]
                self.assertTrue(archived)
            finally:
                graph.close()


if __name__ == "__main__":
    unittest.main()

