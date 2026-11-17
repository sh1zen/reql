from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
from memory.artifacts.models import SourceArtifact
from memory.artifacts.cache import DiskArtifactCache, artifact_cache_path
from memory.artifacts.structural_links import refresh_project_structural_links
from memory.code_analysis import TreeSitterCodeParser


class CodeGraphCompilationTests(unittest.TestCase):

    def test_computed_receivers_remain_unresolved_instead_of_inventing_local_calls(self) -> None:
        """Computed receivers do not establish which same-named method is called."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "dispatch.py").write_text(
                "class Base:\n    def execute(self):\n        return 1\n\n"
                "class Derived(Base):\n"
                "    def execute(self):\n        return super().execute()\n"
                "    def computed(self):\n        return factory().execute()\n"
                "    def indexed(self, items):\n        return items[0].execute()\n"
                "    def recursive(self):\n        return self.recursive()\n\n"
                "def factory():\n    return Base()\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                self.assertFalse(graph.compile_project(root).run.errors)
                nodes = {node.properties.get("qualified_name"): node for node in graph.store.all_nodes()
                         if node.type in {"Function", "Method"}}
                for method, target in (("execute", "super().execute"), ("computed", "factory().execute"),
                                       ("indexed", "items[0].execute")):
                    with self.subTest(method=method):
                        owner = nodes[f"dispatch.Derived.{method}"]
                        calls = graph.store.get_edges(from_id=owner.id, type_="CALLS", limit=20)
                        self.assertFalse(any(edge.to_id in {nodes["dispatch.Derived.execute"].id, nodes["dispatch.Base.execute"].id}
                                             for edge in calls))
                        wraps = graph.store.get_edges(from_id=owner.id, type_="WRAPS", limit=20)
                        self.assertFalse(any(edge.to_id in {nodes["dispatch.Derived.execute"].id, nodes["dispatch.Base.execute"].id}
                                             for edge in wraps))
                        self.assertIn(target, {item["target"] for item in owner.properties.get("unresolved_calls", [])})
                recursive = nodes["dispatch.Derived.recursive"]
                self.assertTrue(graph.store.get_edges(from_id=recursive.id, to_id=recursive.id, type_="CALLS", limit=1))
                cached = graph.compile_project(root)
                self.assertEqual(cached.run.files_changed, 0)
                artifact = cached.scan.artifacts[0]
                cache = DiskArtifactCache(artifact_cache_path(root))
                entry = cache.get_entry(artifact.project_id, artifact.id)
                if entry is None:
                    self.fail("Compilation did not persist its artifact cache")
                cache.upsert_entry(replace(entry, parser_version="project-scan-v10"))
                refreshed = graph.compile_project(root)
                self.assertFalse(refreshed.run.errors)
                self.assertEqual(refreshed.run.files_changed, 1)
                self.assertEqual(graph.compile_project(root).run.files_changed, 0)
            finally:
                graph.close()

    def test_tree_sitter_syntax_recovery_does_not_fail_project_compile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            js_fixture = root / "extensions" / "plugin-check" / "vendor" / "package" / "fixture.js"
            ps_helper = root / "extensions" / "xml-importer" / "linecounter.ps1"
            js_fixture.parent.mkdir(parents=True)
            ps_helper.parent.mkdir(parents=True)
            js_fixture.write_text("<<<<<<< bad fixture\n", encoding="utf-8")
            ps_helper.write_text("function broken {\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)

                self.assertFalse(result.run.errors)
                relative_paths = {artifact.relative_path for artifact in result.scan.artifacts}
                self.assertIn("extensions/plugin-check/vendor/package/fixture.js", relative_paths)
                self.assertIn("extensions/xml-importer/linecounter.ps1", relative_paths)

                modules = {
                    node.properties.get("relative_path"): node
                    for node in graph.store.all_nodes()
                    if node.type == "Module"
                }
                for relative_path in (
                    "extensions/plugin-check/vendor/package/fixture.js",
                    "extensions/xml-importer/linecounter.ps1",
                ):
                    module = modules.get(relative_path)
                    self.assertIsNotNone(module)
                    assert module is not None
                    metadata = module.properties.get("metadata")
                    self.assertIsInstance(metadata, dict)
                    self.assertEqual(metadata.get("parser_status"), "partial")
                    self.assertTrue(metadata.get("tree_sitter_has_error"))
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
                encoding="utf-8-sig",
            )
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                first = graph.compile_project(root)
                second = graph.compile_project(root)
                node_types = {node.type for node in graph.store.all_nodes()}
                edge_types = {edge.type for edge in graph.store.all_edges()}

                self.assertFalse(first.run.errors)
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

    def test_project_structural_links_make_overrides_reexports_and_wrappers_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            package = root / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "from .base import Base\nfrom .derived import *\n",
                encoding="utf-8",
            )
            (package / "base.py").write_text(
                "\n".join(
                    [
                        "class Base:",
                        "    def execute(self, value):",
                        "        return value",
                        "",
                        "    def close(self):",
                        "        return None",
                        "",
                        "def target(value):",
                        "    return value",
                    ]
                ),
                encoding="utf-8",
            )
            (package / "derived.py").write_text(
                "\n".join(
                    [
                        "from .base import Base, target",
                        "",
                        "class Derived(Base):",
                        "    def execute(self, value):",
                        "        return super().execute(value)",
                        "",
                        "    def close(self):",
                        "        return super().close()",
                        "",
                        "def proxy(value):",
                        "    return target(value)",
                    ]
                ),
                encoding="utf-8",
            )

            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)
                nodes = {node.id: node for node in graph.store.all_nodes()}

                override_edges = [
                    edge
                    for edge in graph.store.get_edges(type_="OVERRIDES", limit=20)
                    if edge.properties.get("status") == "active"
                ]
                override_pairs = {
                    (
                        nodes[edge.from_id].properties.get("qualified_name"),
                        nodes[edge.to_id].properties.get("qualified_name"),
                    )
                    for edge in override_edges
                }
                self.assertEqual(
                    override_pairs,
                    {
                        ("pkg.derived.Derived.execute", "pkg.base.Base.execute"),
                        ("pkg.derived.Derived.close", "pkg.base.Base.close"),
                    },
                )
                self.assertTrue(
                    all("override" in nodes[edge.from_id].properties.get("semantic_roles", []) for edge in override_edges)
                )

                proxy = next(node for node in nodes.values() if node.properties.get("qualified_name") == "pkg.derived.proxy")
                target = next(node for node in nodes.values() if node.properties.get("qualified_name") == "pkg.base.target")
                self.assertIn("wrapper", proxy.properties.get("semantic_roles", []))
                self.assertTrue(
                    any(
                        edge.from_id == proxy.id and edge.to_id == target.id and edge.properties.get("status") == "active"
                        for edge in graph.store.get_edges(type_="WRAPS", limit=20)
                    )
                )

                initializer_nodes = [
                    node
                    for node in nodes.values()
                    if node.type in {"File", "Module"} and node.properties.get("relative_path") == "pkg/__init__.py"
                ]
                self.assertTrue(initializer_nodes)
                self.assertTrue(
                    all(
                        {"package-initializer", "re-export"}.issubset(node.properties.get("semantic_roles", []))
                        for node in initializer_nodes
                    )
                )
                init_module = next(node for node in initializer_nodes if node.type == "Module")
                re_exported_names = {
                    nodes[edge.to_id].properties.get("name")
                    for edge in graph.store.get_edges(from_id=init_module.id, type_="RE_EXPORTS", limit=30)
                    if edge.properties.get("status") == "active" and edge.to_id in nodes
                }
                self.assertTrue({"Base", "Derived", "proxy"}.issubset(re_exported_names))

                override_rows = graph.query("RETRIEVE 'override' LIMIT 10 RETURN type,text,relative_path")
                self.assertTrue(any(row["type"] == "Method" and row["relative_path"] == "pkg/derived.py" for row in override_rows.rows))
                wrapper_rows = graph.query("RETRIEVE 'wrapper' LIMIT 10 RETURN type,text,relative_path")
                self.assertTrue(any(row["text"] == "pkg.derived.proxy" for row in wrapper_rows.rows))
                initializer_rows = graph.query("RETRIEVE 're-export package initializer' LIMIT 10 RETURN type,text,relative_path")
                self.assertTrue(any(row["relative_path"] == "pkg/__init__.py" for row in initializer_rows.rows))
                initializer_context = graph.query_context_payload(
                    "re-export initializer",
                    scopes=["code"],
                    top_k=10,
                )["payload"]
                self.assertIn("pkg/__init__.py", {row["path"] for row in initializer_context["working_set"]})

                unchanged_links = refresh_project_structural_links(graph.store, result.scan.project.id)
                self.assertFalse(unchanged_links.added_nodes)
                self.assertFalse(unchanged_links.updated_nodes)
                self.assertFalse(unchanged_links.added_edges)
                self.assertFalse(unchanged_links.updated_edges)
                self.assertFalse(unchanged_links.archived_edges)

                (package / "__init__.py").write_text("# no public exports\n", encoding="utf-8")
                (package / "derived.py").write_text(
                    "\n".join(
                        [
                            "from .base import Base, target",
                            "",
                            "class Derived(Base):",
                            "    def execute(self, value):",
                            "        return super().execute(value)",
                            "",
                            "def proxy(value):",
                            "    return target(value)",
                        ]
                    ),
                    encoding="utf-8",
                )
                refreshed = graph.compile_project(root)
                self.assertFalse(refreshed.run.errors)
                active_override_edges = [
                    edge
                    for edge in graph.store.get_edges(type_="OVERRIDES", limit=20)
                    if edge.properties.get("status") == "active" and edge.weight > 0
                ]
                self.assertEqual(len(active_override_edges), 1)
                refreshed_initializers = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type in {"File", "Module"} and node.properties.get("relative_path") == "pkg/__init__.py"
                ]
                self.assertTrue(all("re-export" not in node.properties.get("semantic_roles", []) for node in refreshed_initializers))
            finally:
                graph.close()

    def test_field_query_returns_end_to_end_data_trace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            package = root / "pipeline"
            tests_dir = root / "tests"
            package.mkdir(parents=True)
            tests_dir.mkdir()
            files = {
                package / "prompts.py": 'FIELD_PROMPT = "Explain Field.type"\n',
                package / "wire_model.py": (
                    "from dataclasses import dataclass\n\n"
                    "@dataclass\n"
                    "class WireField:\n"
                    "    type: str\n"
                ),
                package / "validation.py": (
                    "from .wire_model import WireField\n\n"
                    "def validate_field(field: WireField) -> str:\n"
                    "    return field.type.strip()\n"
                ),
                package / "persisted_model.py": (
                    "from dataclasses import dataclass\n\n"
                    "@dataclass\n"
                    "class PersistedField:\n"
                    "    type: str\n"
                ),
                package / "graph_projection.py": (
                    "from .persisted_model import PersistedField\n\n"
                    "def project_graph(field: PersistedField) -> dict[str, str]:\n"
                    "    return {\"type\": field.type}\n"
                ),
                package / "reconciliation.py": (
                    "from dataclasses import dataclass\n\n"
                    "@dataclass\n"
                    "class ReconciledField:\n"
                    "    type: str\n\n"
                    "def reconcile(field: ReconciledField) -> str:\n"
                    "    return field.type\n"
                ),
                package / "openapi_compiler.py": (
                    "from .reconciliation import ReconciledField\n\n"
                    "def compile_openapi(field: ReconciledField) -> dict[str, str]:\n"
                    "    return {\"type\": field.type}\n"
                ),
                tests_dir / "field_fixture.py": 'EXPECTED_TRACE = "Field.type"\n',
            }
            for path, content in files.items():
                path.write_text(content, encoding="utf-8")

            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)
                payload = graph.query_context_payload(
                    "Field.type",
                    top_k=30,
                    max_depth=3,
                    max_items=30,
                )["payload"]

                stages = {row["stage"] for row in payload["data_trace"]}
                expected_stages = [
                    "prompt",
                    "wire model",
                    "validation",
                    "persisted model",
                    "graph projection",
                    "reconciliation",
                    "OpenAPI compiler",
                    "test/fixture",
                ]
                self.assertTrue(
                    set(expected_stages).issubset(stages),
                    payload["data_trace"],
                )
                unique_stages = list(dict.fromkeys(row["stage"] for row in payload["data_trace"]))
                self.assertEqual([stage for stage in unique_stages if stage in expected_stages], expected_stages)
                rendered = graph.query_context("Field.type", top_k=30, max_items=30)
                self.assertIn("End-to-end data trace", rendered)

                nodes = {node.id: node for node in graph.store.all_nodes()}
                typed_reads = [
                    edge
                    for edge in graph.store.get_edges(type_="READS", limit=200)
                    if edge.properties.get("relation") == "typed_field_reference"
                ]
                self.assertTrue(
                    any(
                        nodes[edge.from_id].properties.get("qualified_name") == "pipeline.validation.validate_field"
                        and nodes[edge.to_id].properties.get("qualified_name") == "pipeline.wire_model.WireField.type"
                        for edge in typed_reads
                    )
                )

                explored = graph.query_explore(
                    "Field.type",
                    views=["serialization_paths"],
                    top_k=30,
                    max_depth=3,
                    limit=60,
                )
                self.assertTrue(any(row.get("depth", 0) > 1 for row in explored["sections"]["serialization_paths"]))
            finally:
                graph.close()

    def test_compile_links_php_rendered_views_and_surface_assets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            controller = root / "app" / "Controllers" / "AuthController.php"
            view = root / "app" / "Views" / "home" / "index.php"
            partial = root / "app" / "Views" / "partials" / "banner.php"
            css = root / "public" / "assets" / "css" / "app.css"
            js = root / "public" / "assets" / "js" / "app.js"
            controller.parent.mkdir(parents=True)
            view.parent.mkdir(parents=True)
            partial.parent.mkdir(parents=True)
            css.parent.mkdir(parents=True)
            js.parent.mkdir(parents=True)
            controller.write_text(
                "\n".join(
                    [
                        "<?php",
                        "class AuthController {",
                        "    public function login() {",
                        "        $badges = [];",
                        "        $unused = [];",
                        "        return view('home/index', array_merge(['user' => $user, 'role' => $role], compact('badges')));",
                        "    }",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            view.write_text(
                "\n".join(
                    [
                        "<form>",
                        "  <input class=\"password-field\" name=\"password\" type=\"password\">",
                        "  <?php require __DIR__ . '/../partials/banner.php'; ?>",
                        "  <?php // require __DIR__ . '/../partials/ghost.php'; ?>",
                        "</form>",
                    ]
                ),
                encoding="utf-8",
            )
            partial.write_text("<aside class=\"banner\"><?= $user ?></aside>\n", encoding="utf-8")
            css.write_text(
                "\n".join(
                    [
                        ".password-field { border-color: #555; }",
                        ".js-only { display: none; }",
                        ".unused-css { opacity: .5; }",
                        ".duplicate { color: red; color: blue; }",
                        "input[type=\"password\"] { letter-spacing: 0; }",
                    ]
                ),
                encoding="utf-8",
            )
            js.write_text(
                "document.querySelector('.password-field')?.focus();\n"
                "document.querySelectorAll('.js-only').forEach((item) => item.remove());\n",
                encoding="utf-8",
            )

            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertFalse(result.run.errors)

                files = {
                    node.properties.get("relative_path"): node
                    for node in graph.store.all_nodes()
                    if node.type == "File"
                }
                self.assertEqual(files["public/assets/css/app.css"].properties.get("context_scope"), "code")
                self.assertEqual(files["public/assets/css/app.css"].properties.get("language"), "CSS")

                template_edges = [
                    edge
                    for edge in graph.store.all_edges()
                    if edge.type == "REFERENCES"
                    and edge.properties.get("relation") == "template_render"
                    and edge.to_id == files["app/Views/home/index.php"].id
                ]
                self.assertTrue(template_edges)
                self.assertEqual(template_edges[0].properties.get("target_name"), "home/index")
                self.assertEqual(template_edges[0].properties.get("view_variables"), ["badges", "role", "user"])

                unused_variables = {
                    node.properties.get("symbol_name")
                    for node in graph.store.all_nodes()
                    if node.type == "StaticAnalysisFinding"
                    and node.properties.get("finding_type") == "unused_variable"
                    and node.properties.get("relative_path") == "app/Controllers/AuthController.php"
                }
                self.assertNotIn("badges", unused_variables)
                self.assertIn("unused", unused_variables)

                include_edges = [
                    edge
                    for edge in graph.store.all_edges()
                    if edge.type == "REFERENCES"
                    and edge.properties.get("relation") == "template_include"
                    and edge.to_id == files["app/Views/partials/banner.php"].id
                ]
                self.assertTrue(include_edges)
                self.assertTrue(include_edges[0].properties.get("inherits_scope"))
                self.assertFalse(
                    any(
                        "ghost.php" in str(node.properties.get("module") or "")
                        for node in graph.store.all_nodes()
                        if node.type == "Import"
                    )
                )

                node_by_id = {node.id: node for node in graph.store.all_nodes()}
                surface_edges = [
                    edge
                    for edge in graph.store.all_edges()
                    if edge.type == "REFERENCES" and edge.properties.get("relation") == "shared_surface_identifier"
                ]
                linked_paths = {
                    (
                        node_by_id[edge.from_id].properties.get("relative_path") if edge.from_id in node_by_id else None,
                        node_by_id[edge.to_id].properties.get("relative_path") if edge.to_id in node_by_id else None,
                    )
                    for edge in surface_edges
                }
                self.assertTrue(
                    ("public/assets/css/app.css", "app/Views/home/index.php") in linked_paths
                    or ("app/Views/home/index.php", "public/assets/css/app.css") in linked_paths
                )
                self.assertTrue(
                    ("public/assets/js/app.js", "app/Views/home/index.php") in linked_paths
                    or ("app/Views/home/index.php", "public/assets/js/app.js") in linked_paths
                )
                self.assertTrue(
                    ("public/assets/css/app.css", "public/assets/js/app.js") in linked_paths
                    or ("public/assets/js/app.js", "public/assets/css/app.css") in linked_paths
                )

                css_findings = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type == "StaticAnalysisFinding"
                    and node.properties.get("relative_path") == "public/assets/css/app.css"
                    and node.status == "active"
                ]
                finding_names = {(node.properties.get("finding_type"), node.properties.get("symbol_name")) for node in css_findings}
                self.assertIn(("unused_css_class", ".unused-css"), finding_names)
                self.assertNotIn(("unused_css_class", ".password-field"), finding_names)
                self.assertNotIn(("unused_css_class", ".js-only"), finding_names)
                self.assertIn(("always_overridden_css_declaration", ".duplicate color"), finding_names)

                payload = graph.query_context_payload(
                    "AuthController password field",
                    top_k=12,
                    scopes=["code"],
                )["payload"]
                working_paths = {row["path"] for row in payload["working_set"]}
                self.assertIn("app/Controllers/AuthController.php", working_paths)
                self.assertIn("app/Views/home/index.php", working_paths)
                self.assertIn("public/assets/css/app.css", working_paths)
                php_symbols = [
                    node
                    for node in graph.store.all_nodes()
                    if node.type in {"Class", "Method"}
                    and node.properties.get("relative_path") == "app/Controllers/AuthController.php"
                ]
                if php_symbols:
                    self.assertTrue(all(node.properties.get("line_start") for node in php_symbols))
                    self.assertTrue(all(node.properties.get("line_end") for node in php_symbols))
                    self.assertTrue(all(node.properties.get("source_path") for node in php_symbols))
                    symbol_rows = graph.query(
                        "SYMBOLS WHERE relative_path = 'app/Controllers/AuthController.php' "
                        "RETURN type,name,start_line,end_line,line_start,line_end LIMIT 10"
                    )
                    owner_rows = [row for row in symbol_rows.rows if row["type"] in {"Class", "Method"}]
                    self.assertTrue(owner_rows)
                    self.assertTrue(all(row["line_start"] for row in owner_rows))
                    self.assertTrue(all(row["line_end"] for row in owner_rows))
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

    def test_generic_tree_sitter_profiles_extract_useful_language_graphs(self) -> None:
        samples = {
            "java": (
                "Main.java",
                "package demo; import java.util.List; class Main extends Base { void run(){ helper(); } void helper(){} }\n",
                {"Main", "run", "helper"},
                {"java.util.List"},
                {"helper"},
            ),
            "go": (
                "main.go",
                'package main\nimport "fmt"\ntype Runner struct{}\nfunc helper(){}\nfunc (r Runner) Run(){ value := helper(); fmt.Println(value) }\n',
                {"Runner", "helper", "Run", "value"},
                {"fmt"},
                {"helper", "fmt.Println"},
            ),
            "rust": (
                "lib.rs",
                "use std::fmt; struct Runner; impl Runner { fn run(&self){ helper(); } } fn helper(){}\n",
                {"Runner", "run", "helper"},
                {"std::fmt"},
                {"helper"},
            ),
            "ruby": (
                "app.rb",
                'require "json"\nclass Runner < Base\n def run(x)\n  helper(x)\n end\n def helper(x); x; end\nend\n',
                {"Runner", "run", "helper"},
                {"json"},
                {"helper"},
            ),
            "lua": (
                "app.lua",
                'local json = require("json")\nfunction helper(x) return x end\nlocal function run() helper(1) end\n',
                {"helper", "run"},
                {"json"},
                {"helper"},
            ),
            "elixir": (
                "demo.ex",
                "defmodule Demo do\n import Enum\n def run(x) do helper(x) end\n defp helper(x), do: x\nend\n",
                {"Demo", "run", "helper"},
                {"Enum"},
                {"helper"},
            ),
            "powershell": (
                "demo.ps1",
                "Import-Module Pester\nfunction Invoke-Run { Invoke-Helper }\nfunction Invoke-Helper { return 1 }\n",
                {"Invoke-Run", "Invoke-Helper"},
                {"Pester"},
                {"Invoke-Helper"},
            ),
            "fortran": (
                "demo.f90",
                "module demo\ncontains\nsubroutine run()\ncall helper()\nend subroutine\nsubroutine helper()\nend subroutine\nend module\n",
                {"demo", "run", "helper"},
                set(),
                {"helper"},
            ),
            "zig": (
                "demo.zig",
                'const std = @import("std"); pub const Point = struct { x: i32 }; pub fn run() void { helper(); } fn helper() void {}\n',
                {"Point", "run", "helper"},
                {"std"},
                {"helper"},
            ),
        }
        parser = TreeSitterCodeParser()
        for language, (relative_path, source, expected_symbols, expected_imports, expected_calls) in samples.items():
            with self.subTest(language=language):
                artifact = SourceArtifact(
                    id=f"artifact:{language}",
                    project_id="project:generic",
                    uri=f"file:///{relative_path}",
                    path=relative_path,
                    relative_path=relative_path,
                    artifact_type="code",
                    language=language,
                    size_bytes=len(source),
                    sha256="",
                    mtime=0.0,
                )
                result = parser.parse_artifact(artifact, source)
                self.assertFalse(result.errors)
                self.assertTrue(expected_symbols.issubset({symbol.name for symbol in result.symbols}))
                self.assertTrue(expected_imports.issubset({item.module for item in result.imports}))
                self.assertTrue(expected_calls.issubset({call.target for call in result.calls}))
                self.assertTrue(all(call.caller for call in result.calls if call.target in expected_calls))

    def test_tree_sitter_parser_unavailable_is_non_fatal(self) -> None:
        artifact = SourceArtifact(
            id="artifact:sql",
            project_id="project:generic",
            uri="file:///schema.sql",
            path="schema.sql",
            relative_path="schema.sql",
            artifact_type="code",
            language="sql",
            size_bytes=20,
            sha256="",
            mtime=0.0,
        )
        parser = TreeSitterCodeParser()
        with patch("memory.code_analysis.base._parser_for", side_effect=ValueError("Unsupported Tree-sitter language: sql")):
            result = parser.parse_artifact(artifact, "select * from users;\n")

        self.assertFalse(result.errors)
        self.assertEqual(result.module.metadata.get("parser_status"), "skipped")
        self.assertIn("Unsupported Tree-sitter language: sql", result.module.metadata.get("parser_warning", ""))

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

if __name__ == "__main__":
    unittest.main()

