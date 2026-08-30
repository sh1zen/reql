from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
from memory import cli as cli_mod
from memory.domain.models import MemoryEdge, MemoryNode
from memory.reporting.project_pipeline import (
    render_pipeline_html,
    render_pipeline_mermaid,
)


class ProjectPipelineTests(unittest.TestCase):
    def test_projection_merges_shared_components_preserves_cycles_and_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            storage = Path(td) / "memory.reql"
            graph = self._graph_with_shared_pipeline(root, storage)
            try:
                counts_before = (graph.store.count_nodes(), graph.store.count_edges())
                first = graph.project_pipeline(root)
                second = graph.project_pipeline(root)

                self.assertEqual(counts_before, (graph.store.count_nodes(), graph.store.count_edges()))
                self.assertEqual(first.to_dict(), second.to_dict())
                self.assertEqual(first.schema_version, 1)
                self.assertEqual(len(first.workflows), 2)
                self.assertTrue(all(not workflow.inferred for workflow in first.workflows))
                self.assertEqual(first.basis["llm_required"], False)

                component_by_path = {
                    path: component
                    for component in first.components
                    for path in component.paths
                }
                self.assertEqual(set(component_by_path), {"src/api.py", "src/core/service.py", "src/infrastructure/repository.py"})
                self.assertEqual(len(component_by_path["src/core/service.py"].workflow_ids), 2)
                self.assertEqual(len(component_by_path["src/infrastructure/repository.py"].workflow_ids), 2)
                self.assertTrue(component_by_path["src/core/service.py"].cyclic)
                self.assertTrue(component_by_path["src/infrastructure/repository.py"].cyclic)
                self.assertTrue(
                    any(
                        symbol.label == "_dispatch" and symbol.private
                        for symbol in component_by_path["src/core/service.py"].symbols
                    )
                )
                self.assertFalse(
                    any(symbol.node_id == "function:test-only" for component in first.components for symbol in component.symbols)
                )
                self.assertTrue(any(edge.cyclic for edge in first.edges))
                self.assertTrue(any(outcome.kind == "WRITES" for outcome in first.outcomes))
                self._assert_workflows_are_connected(first)
            finally:
                graph.close()

    def test_projection_excludes_tests_from_the_entire_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            graph = self._graph_with_shared_pipeline(root, Path(td) / "memory.reql")
            try:
                project_id = str(graph.project_status(root)["project"]["id"])
                test_outcome = self._node(
                    "variable:test-result",
                    "Variable",
                    "test_output_only",
                    "tests/fixtures.py",
                    project_id,
                    8,
                )
                pathless_test = self._node(
                    "test:pathless",
                    "Test",
                    "pathless_test_node",
                    "",
                    project_id,
                    1,
                )
                graph.add_node(test_outcome)
                graph.add_node(pathless_test)
                graph.add_edge(
                    self._edge("edge:test-write", "method:save", test_outcome.id, "WRITES", project_id)
                )
                graph.add_edge(
                    self._edge("edge:pathless-test-return", "method:save", pathless_test.id, "RETURNS", project_id)
                )

                payload = json.dumps(graph.project_pipeline(root).to_dict(), sort_keys=True)

                for forbidden in (
                    "function:test-only",
                    "tests/test_api.py",
                    "variable:test-result",
                    "tests/fixtures.py",
                    "test_output_only",
                    "test:pathless",
                    "pathless_test_node",
                ):
                    self.assertNotIn(forbidden, payload)
            finally:
                graph.close()

    def test_projection_follows_exact_resolved_imports_used_by_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            graph = self._empty_project_graph(root, Path(td) / "memory.reql")
            try:
                project_id = str(graph.project_status(root)["project"]["id"])
                entry = self._node("function:run", "Function", "run", "src/entry.py", project_id, 2)
                target = self._node("function:execute", "Function", "execute", "src/service/impl.py", project_id, 4)
                entry.properties["artifact_id"] = "artifact:entry"
                target.properties["artifact_id"] = "artifact:service"
                import_node = MemoryNode(
                    id="import:execute",
                    type="Import",
                    label="from service import execute",
                    canonical_key="import:execute",
                    properties={
                        "project_id": project_id,
                        "artifact_id": "artifact:entry",
                        "name": "execute",
                        "module": "service",
                        "resolved_relative_path": "src/service/__init__.py",
                    },
                )
                reexport_node = MemoryNode(
                    id="import:reexport-execute",
                    type="Import",
                    label="from .impl import execute",
                    canonical_key="import:reexport-execute",
                    properties={
                        "project_id": project_id,
                        "artifact_id": "artifact:service-init",
                        "source_file": "src/service/__init__.py",
                        "name": "execute",
                        "module": ".impl",
                        "resolved_relative_path": "src/service/impl.py",
                    },
                )
                fragment = MemoryNode(
                    id="fragment:run",
                    type="SourceFragment",
                    label="run source",
                    canonical_key="fragment:run",
                    text="def run():\n    return execute()\n",
                    properties={
                        "project_id": project_id,
                        "artifact_id": "artifact:entry",
                        "symbol_id": entry.id,
                    },
                )
                for node in (entry, target, import_node, reexport_node, fragment):
                    graph.add_node(node)

                pipeline = graph.project_pipeline(root)

                self.assertEqual(len(pipeline.workflows), 1)
                self.assertEqual(len(pipeline.components), 2)
                self.assertEqual(len(pipeline.edges), 1)
                self.assertEqual(pipeline.edges[0].relation_types, ("IMPORTS_FROM",))
                self.assertEqual(len(pipeline.workflows[0].component_ids), 2)
                self._assert_workflows_are_connected(pipeline)
            finally:
                graph.close()

    def test_projection_falls_back_to_public_call_graph_roots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            graph = self._empty_project_graph(root, Path(td) / "memory.reql")
            try:
                project_id = str(graph.project_status(root)["project"]["id"])
                start = self._node("function:start", "Function", "begin_work", "lib/domain/task.py", project_id, 2)
                finish = self._node("function:finish", "Function", "finish_work", "lib/domain/task.py", project_id, 8)
                graph.add_node(start)
                graph.add_node(finish)
                graph.add_edge(self._edge("edge:fallback", start.id, finish.id, "CALLS", project_id))

                pipeline = graph.project_pipeline(root)

                self.assertEqual(len(pipeline.workflows), 1)
                self.assertTrue(pipeline.workflows[0].inferred)
                self.assertEqual(pipeline.workflows[0].trigger_reason, "inferred call-graph root")
                self.assertTrue(pipeline.basis["fallback_entrypoints"])
            finally:
                graph.close()

    def test_empty_projection_and_renderers_produce_valid_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            graph = self._empty_project_graph(root, Path(td) / "memory.reql")
            try:
                pipeline = graph.project_pipeline(root)
                mermaid = render_pipeline_mermaid(pipeline)
                html = render_pipeline_html(pipeline)

                self.assertFalse(pipeline.workflows)
                self.assertIn("flowchart LR", mermaid)
                self.assertIn("No deterministic project pipeline detected", mermaid)
                self.assertIn("REQL Project Pipeline", html)
                self.assertIn("vis-network@9.1.6", html)
                self.assertIn("Fit graph", html)
                self.assertIn("workflow-filter", html)
            finally:
                graph.close()

    def test_renderers_escape_payloads_and_describe_source_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            graph = self._graph_with_shared_pipeline(root, Path(td) / "memory.reql")
            try:
                pipeline = graph.project_pipeline(root)
                mermaid = render_pipeline_mermaid(pipeline)
                html = render_pipeline_html(pipeline)

                self.assertIn("flowchart LR", mermaid)
                self.assertIn("%% component", mermaid)
                self.assertIn("_dispatch", mermaid)
                self.assertIn("&quot;", mermaid)
                self.assertNotIn('</script><script>alert("pipeline")</script>', html)
                self.assertIn(r"\u003c/script\u003e\u003cscript\u003ealert", html)
                self.assertIn("navigationButtons:true", html)
                self.assertIn("search-results", html)
                self.assertIn("DENSE_GRAPH_THRESHOLD = 40", html)
                self.assertIn("denseGraphFocus", html)
                self.assertIn("vis-navigation .vis-button", html)
                self.assertIn("groupPipelineOutcomes", html)
                self.assertIn("randomSeed:1729", html)
                self.assertNotIn("layout:{hierarchical:", html)
                self.assertIn("positionPipelineNodes", html)
                self.assertIn("rowsPerColumn=10", html)
                self.assertIn("dragNodes:true", html)
                self.assertIn("network.storePositions()", html)
                self.assertIn("physics:{enabled:false}", html)
            finally:
                graph.close()

    def test_cli_writes_html_by_default_and_mermaid_on_request(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            storage = Path(td) / "memory.reql"
            graph = self._graph_with_shared_pipeline(root, storage)
            graph.close()
            (root / "pipeline.html").write_text("stale export", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(cli_mod.sys, "stdout", stdout), patch.object(cli_mod.sys, "stderr", stderr):
                result = cli_mod.main(["--storage", str(storage), "project", "pipeline", str(root)])
            html_path = root / "pipeline.html"
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertTrue(html_path.exists())
            self.assertNotIn("stale export", html_path.read_text(encoding="utf-8"))
            self.assertEqual(stdout.getvalue().strip(), str(html_path.resolve()))

            output_dir = Path(td) / "exports"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(cli_mod.sys, "stdout", stdout), patch.object(cli_mod.sys, "stderr", stderr):
                result = cli_mod.main(
                    ["--storage", str(storage), "project", "pipeline", str(root), "--code", "--out", str(output_dir)]
                )
            mermaid_path = output_dir / "pipeline.mmd"
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertTrue(mermaid_path.exists())
            self.assertIn("flowchart LR", mermaid_path.read_text(encoding="utf-8"))
            self.assertEqual(stdout.getvalue().strip(), str(mermaid_path.resolve()))

    def test_cli_rejects_conflicting_formats_and_incompatible_extensions(self) -> None:
        parser = cli_mod.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["project", "pipeline", ".", "--code", "--html"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            storage = Path(td) / "memory.reql"
            graph = self._empty_project_graph(root, storage)
            graph.close()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(cli_mod.sys, "stdout", stdout), patch.object(cli_mod.sys, "stderr", stderr):
                result = cli_mod.main(
                    ["--storage", str(storage), "project", "pipeline", str(root), "--code", "--out", str(Path(td) / "wrong.html")]
                )
            self.assertEqual(result, 2)
            self.assertIn("must use one of", stderr.getvalue())

            html_file = cli_mod._project_pipeline_output_path(
                str(Path(td) / "custom.htm"),
                project_root=root,
                output_format="html",
            )
            mermaid_file = cli_mod._project_pipeline_output_path(
                str(Path(td) / "custom.mermaid"),
                project_root=root,
                output_format="mermaid",
            )
            self.assertEqual(html_file.suffix, ".htm")
            self.assertEqual(mermaid_file.suffix, ".mermaid")

            missing_output = Path(td) / "missing-output.html"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(cli_mod.sys, "stdout", stdout), patch.object(cli_mod.sys, "stderr", stderr):
                result = cli_mod.main(
                    [
                        "--storage",
                        str(storage),
                        "project",
                        "pipeline",
                        str(Path(td) / "not-registered"),
                        "--out",
                        str(missing_output),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertIn("Project not found", stderr.getvalue())
            self.assertFalse(missing_output.exists())

    @classmethod
    def _graph_with_shared_pipeline(cls, root: Path, storage: Path) -> MemoryGraph:
        graph = cls._empty_project_graph(root, storage)
        project_id = str(graph.project_status(root)["project"]["id"])
        nodes = [
            cls._node("endpoint:checkout", "Endpoint", "POST /checkout", "src/api.py", project_id, 2),
            cls._node("endpoint:refund", "Endpoint", "POST /refund", "src/api.py", project_id, 10),
            cls._node("function:checkout", "Function", "checkout_handler", "src/api.py", project_id, 3, roles=["handler"]),
            cls._node("function:refund", "Function", "refund_handler", "src/api.py", project_id, 11, roles=["handler"]),
            cls._node("function:dispatch", "Function", "_dispatch", "src/core/service.py", project_id, 4),
            cls._node("method:save", "Method", 'save_"record"', "src/infrastructure/repository.py", project_id, 7),
            cls._node("function:test-only", "Function", "test_only", "tests/test_api.py", project_id, 3),
            cls._node(
                "variable:result",
                "Variable",
                '</script><script>alert("pipeline")</script>',
                "src/infrastructure/repository.py",
                project_id,
                12,
            ),
        ]
        for node in nodes:
            graph.add_node(node)
        for edge in [
            cls._edge("edge:route-checkout", "function:checkout", "endpoint:checkout", "HANDLES_ROUTE", project_id),
            cls._edge("edge:route-refund", "function:refund", "endpoint:refund", "HANDLES_ROUTE", project_id),
            cls._edge("edge:checkout-dispatch", "function:checkout", "function:dispatch", "CALLS", project_id),
            cls._edge("edge:refund-dispatch", "function:refund", "function:dispatch", "CALLS", project_id),
            cls._edge("edge:dispatch-save", "function:dispatch", "method:save", "CALLS", project_id),
            cls._edge("edge:save-dispatch", "method:save", "function:dispatch", "CALLS", project_id),
            cls._edge("edge:test", "function:checkout", "function:test-only", "CALLS", project_id),
            cls._edge("edge:write", "method:save", "variable:result", "WRITES", project_id),
        ]:
            graph.add_edge(edge)
        return graph

    @staticmethod
    def _assert_workflows_are_connected(pipeline: object) -> None:
        for workflow in pipeline.workflows:
            reachable = {workflow.trigger_component_id}
            changed = True
            while changed:
                changed = False
                for edge in pipeline.edges:
                    if workflow.id not in edge.workflow_ids or edge.from_component_id not in reachable:
                        continue
                    if edge.to_component_id not in reachable:
                        reachable.add(edge.to_component_id)
                        changed = True
            assert set(workflow.component_ids) <= reachable

    @staticmethod
    def _empty_project_graph(root: Path, storage: Path) -> MemoryGraph:
        root.mkdir(parents=True)
        (root / "placeholder.py").write_text("VALUE = 1\n", encoding="utf-8")
        graph = MemoryGraph.open(storage)
        result = graph.compile_project(root)
        if result.run.status != "completed":
            graph.close()
            raise AssertionError(result.run.errors)
        return graph

    @staticmethod
    def _node(
        node_id: str,
        node_type: str,
        label: str,
        path: str,
        project_id: str,
        line: int,
        *,
        roles: list[str] | None = None,
    ) -> MemoryNode:
        return MemoryNode(
            id=node_id,
            type=node_type,
            label=label,
            canonical_key=node_id,
            properties={
                "project_id": project_id,
                "relative_path": path,
                "name": label,
                "line_start": line,
                "line_end": line + 1,
                "semantic_roles": roles or [],
            },
        )

    @staticmethod
    def _edge(edge_id: str, from_id: str, to_id: str, relation: str, project_id: str) -> MemoryEdge:
        return MemoryEdge(
            id=edge_id,
            from_id=from_id,
            to_id=to_id,
            type=relation,
            properties={"project_id": project_id},
        )


if __name__ == "__main__":
    unittest.main()
