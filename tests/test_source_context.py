"""Exercise source-backed inspection and context through compilation and providers."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from mcp.tools import MCPToolError, inspect_node as mcp_inspect_node, list_tools, query_context as mcp_query_context
from memory.artifacts.compiler import ArtifactCompiler
from memory.artifacts.revision import RevisionRepository
from memory.cli import main
from memory.domain.query_context import QueryContextRequest
from memory.services.retrieval.context.source import capture_source_excerpts


class SourceContextTests(unittest.TestCase):
    """Keep source excerpts accurate, bounded, optional, and independent of the cwd."""

    def setUp(self) -> None:
        test_root = Path.cwd() / ".tmp" / "regression-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.root = Path(self.temp.name) / "catalog"
        self.root.mkdir()
        self.path = self.root / "tax_rules.py"
        self.body = ('def calculate_invoice_total(prices):\n'
                     '    """Apply invoice rules to the supplied prices."""\n'
                     '    total = sum(prices)\n'
                     + ''.join(f'    total += {value}\n' for value in range(20))
                     + '    return total\n')
        self.path.write_text(self.body, encoding="utf-8")
        self.db = Path(self.temp.name) / ".reql" / "memory.reql"
        self.graph = MemoryGraph.open(self.db)
        compiled = self.graph.compile_project(self.root)
        self.assertFalse(compiled.run.errors)
        self.artifact_id = compiled.scan.artifacts[0].id
        self.project_id = compiled.scan.project.id
        self.node = next(node for node in self.graph.store.all_nodes()
                         if node.type == "Function" and node.properties.get("name") == "calculate_invoice_total")

    def tearDown(self) -> None:
        self.graph.close()
        self.temp.cleanup()

    def _payload(self, **kwargs):
        """Use the public Python boundary with explicit source scope."""
        return self.graph.query_context_payload("calculate_invoice_total", scopes=["code"], **kwargs)

    def _read(self, start: int = 1, end: int = 24):
        """Construct a graph-provenanced source request for boundary tests."""
        return {"path": "tax_rules.py", "line_start": start, "line_end": end, "node_id": self.node.id}

    def _inspect_cli(self, *arguments: str) -> tuple[int, str, str]:
        """Read the persisted fixture through the normal CLI and capture both formats."""
        self.graph.close()
        output, errors = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = main(["inspect", "--node-id", self.node.id, "--limit", "1", *arguments])
        finally:
            os.chdir(previous)
        return status, output.getvalue(), errors.getvalue()

    def test_inspection_reports_changed_or_missing_files_without_old_body(self) -> None:
        """Structured and plain inspection refuse stale bodies after edits or deletion."""
        self.graph.close()
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.path.unlink()
                else:
                    self.path.write_text('\n\n' + self.body, encoding='utf-8')
                with contextlib.closing(MemoryGraph.open(self.db, read_only=True)) as persisted:
                    excerpt = persisted.inspect_node(self.node.id)['source_excerpt']
                self.assertEqual(excerpt['source_status'], 'stale')
                self.assertNotIn('text', excerpt)
                status, text, _ = self._inspect_cli()
                self.assertEqual(status, 0)
                self.assertIn('Source: stale', text)
                self.assertIn('Read the current file directly', text)
                self.assertNotIn('total = sum(prices)', text)

    def test_inspect_python_cli_mcp_and_generated_read_command_agree(self) -> None:
        """All inspection formats retain verified source, graph facts and read limits."""
        read = next(row for row in self._payload()['payload']['read_plan'] if row['node_id'] == self.node.id)
        arguments = read['command'].split()[1:]
        from memory.cli import build_parser
        parsed = build_parser().parse_args(arguments)
        self.assertEqual(parsed.limit, 1)
        self.assertTrue(parsed.json)
        before = self.graph.get_node(self.node.id).to_dict()
        self.graph.inspect_node(self.node.id)
        self.assertEqual(before, self.graph.get_node(self.node.id).to_dict())
        self.graph.close()
        with contextlib.closing(MemoryGraph.open(self.db, read_only=True)) as persisted:
            expected = persisted.inspect_node(self.node.id, limit=parsed.limit)
        excerpt = expected['source_excerpt']
        self.assertEqual(excerpt['source_status'], 'verified')
        self.assertEqual(excerpt['text'], self.body.rstrip('\n'))
        self.assertFalse(excerpt['truncated'])
        for field in ('node', 'location', 'sources', 'neighbors', 'incoming', 'outgoing'):
            self.assertIn(field, expected)
        result = mcp_inspect_node(storage_path=str(self.db), node_id=self.node.id, limit=parsed.limit)
        output, errors = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = main(arguments)
        finally:
            os.chdir(previous)
        text, errors = output.getvalue(), errors.getvalue()
        self.assertEqual((status, errors), (0, ''))
        self.assertEqual(json.loads(text), expected)
        self.assertEqual(result['source_excerpt'], excerpt)
        self.assertLessEqual(len(result['neighbors']), 2)
        status, text, errors = self._inspect_cli()
        self.assertEqual((status, errors), (0, ''))
        self.assertIn('Source: verified', text)
        self.assertIn(excerpt['sha256'], text)
        self.assertEqual(text.count(self.body.rstrip('\n')), 1)
        for neighbor in expected['neighbors']:
            self.assertIn(neighbor['edge_type'], text)
            self.assertIn(neighbor['other_id'], text)
        self.assertIn('Relations are bounded by --limit', text)
        self.assertNotIn('"outgoing":', text)

    def test_opt_in_returns_complete_verified_body(self) -> None:
        payload = self._payload(include_source=True)
        snippets = payload["payload"]["snippets"]
        row = next(item for item in snippets if item.get("text"))
        self.assertEqual(row["text"], self.body.rstrip("\n"))
        self.assertEqual(row["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(row["source_status"], "verified")
        self.assertFalse(row["truncated"])
        self.assertTrue(payload["payload"]["read_plan"][0]["snippet_embedded"])
        rendered = self.graph.query_context("calculate_invoice_total", scopes=["code"], include_source=True)
        self.assertIn("Current source", rendered)
        self.assertIn("    return total", rendered)
        self.assertNotIn(payload["payload"]["read_plan"][0]["command"], rendered)
        compiled = self.graph.compile_project(self.root, cache_enabled=False)
        self.assertFalse(compiled.run.errors)
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(any(item.get("text") == self.body.rstrip("\n") for item in rows))

    def test_default_does_not_capture_source_or_change_the_payload(self) -> None:
        """Committed source stays current without a watcher or opt-in source capture."""
        with patch("memory.services.retrieval.context.projections.code.capture_source_excerpts",
                   side_effect=AssertionError("unexpected source capture")):
            original = self._payload()
            explicit = self._payload(include_source=False)
            rendered = self.graph.query_context("calculate_invoice_total", scopes=["code"])
        for result in (original, explicit):
            result["payload"].pop("trace_id", None)
        self.assertEqual(original, explicit)
        self.assertNotIn("source_mode", original["payload"])
        self.assertIsNotNone(original["source_revision"])
        self.assertEqual(original["freshness"]["status"], "current")
        self.assertNotIn("snapshot", original["freshness"])
        self.assertIn("Graph freshness: current", rendered)
        self.assertNotIn("snapshot", rendered.casefold())

    def test_changed_deleted_and_refreshed_source(self) -> None:
        self.path.write_text(self.body.replace("sum(prices)", "sum(prices) * 2"), encoding="utf-8")
        stale = self._payload(include_source=True)
        self.assertEqual(stale["freshness"]["status"], "stale")
        self.assertTrue(stale["payload"]["snippets"])
        self.assertTrue(all(item["source_status"] == "stale" and "text" not in item
                            for item in stale["payload"]["snippets"]))
        compiled = self.graph.compile_project(self.root)
        self.assertFalse(compiled.run.errors)
        refreshed = self._payload(include_source=True)
        self.assertTrue(any("sum(prices) * 2" in item.get("text", "") for item in refreshed["payload"]["snippets"]))
        self.path.unlink()
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(all(item["source_status"] == "stale" and "text" not in item for item in rows))

    def test_partial_failed_run_then_reverted_scan_cannot_validate_wrong_spans(self) -> None:
        other = self.root / "other.py"
        other.write_text("def another_rule():\n    return 1\n", encoding="utf-8")
        self.graph.compile_project(self.root)
        other.write_text("def another_rule():\n    return 2\n", encoding="utf-8")
        self.path.write_text("\n\n" + self.body, encoding="utf-8")
        compile_artifact = ArtifactCompiler.compile_artifact

        def partial_compile(compiler, store, artifact):
            if artifact.relative_path == "other.py":
                raise RuntimeError("other parser failed")
            return compile_artifact(compiler, store, artifact)

        with patch.object(ArtifactCompiler, "compile_artifact", partial_compile):
            failed = self.graph.compile_project(self.root)
        self.assertTrue(failed.run.errors)
        self.path.write_text(self.body, encoding="utf-8")
        with patch.object(ArtifactCompiler, "compile_artifact", side_effect=RuntimeError("parser failed")):
            failed = self.graph.compile_project(self.root)
        self.assertTrue(failed.run.errors)
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(rows)
        self.assertTrue(all(item["source_status"] != "verified" and "text" not in item for item in rows))

    def test_bom_crlf_and_non_utf8_source_are_handled_explicitly(self) -> None:
        self.path.write_bytes(b"\xef\xbb\xbf" + self.body.replace("\n", "\r\n").encode("utf-8"))
        self.graph.compile_project(self.root)
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(any(item.get("text") == self.body.rstrip("\n") for item in rows))
        self.path.write_bytes(self.body.encode("utf-8") + b"# non-UTF8 comment: \xff\n")
        self.graph.compile_project(self.root)
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(rows)
        self.assertTrue(all(item["source_status"] == "unavailable" and "text" not in item for item in rows))

    def test_byte_limit_and_read_errors_never_return_graph_text(self) -> None:
        with patch("memory.services.retrieval.context.source.MAX_SOURCE_BYTES", 32):
            rows = capture_source_excerpts(self.graph.store, [self._read()])
        self.assertEqual(rows[0]["source_status"], "unavailable")
        self.assertNotIn("text", rows[0])
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            rows = capture_source_excerpts(self.graph.store, [self._read()])
        self.assertEqual(rows[0]["source_status"], "unavailable")
        self.assertIn("PermissionError", rows[0]["reason"])
        self.assertNotIn("text", rows[0])

    def test_file_region_and_total_budgets_apply_across_the_entire_response(self) -> None:
        for index in range(3):
            (self.root / f"zone_{index}.py").write_text(
                f"def zone_{index}():\n    return {index}\n", encoding="utf-8")
        self.graph.compile_project(self.root)
        other_nodes = [node for node in self.graph.store.all_nodes()
                       if node.type == "Function" and node.id != self.node.id]
        reads = [self._read(line, line) for line in range(1, 24, 2)]
        regions = capture_source_excerpts(self.graph.store, reads)
        self.assertEqual(len(regions), 6)
        reads = [self._read()] + [dict(path=node.properties["relative_path"], line_start=1,
                                      line_end=2, node_id=node.id) for node in other_nodes]
        rows = capture_source_excerpts(self.graph.store, reads)
        self.assertEqual(len({row["artifact_id"] for row in rows}), 3)
        self.assertLessEqual(sum(len(row.get("text", "").splitlines()) for row in rows), 120)
        self.assertLessEqual(sum(len(row.get("text", "")) for row in rows), 6000)

    def test_outside_paths_and_resolved_links_are_rejected_before_open(self) -> None:
        artifact = self.graph.store.get_node(self.artifact_id)
        revision = RevisionRepository(self.graph.store).latest(self.project_id)
        for unsafe in ("../outside.py", "C:/outside.py", "/outside.py", "//server/share/outside.py"):
            with self.subTest(path=unsafe):
                props = {**artifact.properties, "relative_path": unsafe}
                self.graph.store.update_node_fields(artifact.id, properties=props)
                revision.manifest[unsafe] = revision.manifest["tax_rules.py"]
                with patch.object(RevisionRepository, "latest", return_value=revision), \
                        patch.object(Path, "open", side_effect=AssertionError("outside file was opened")):
                    rows = capture_source_excerpts(self.graph.store, [self._read()])
                self.assertEqual(rows[0]["source_status"], "unavailable")
                self.assertIn("outside", rows[0]["reason"])
        self.graph.store.update_node_fields(artifact.id, properties=artifact.properties)
        original_resolve = Path.resolve

        def linked_resolve(path, *args, **kwargs):
            return self.root.parent / "outside.py" if path == self.path else original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", linked_resolve), \
                patch.object(Path, "open", side_effect=AssertionError("resolved link was opened")):
            rows = capture_source_excerpts(self.graph.store, [self._read()])
        self.assertEqual(rows[0]["source_status"], "unavailable")

    def test_python_cli_and_mcp_provide_the_same_verified_source(self) -> None:
        expected = self._payload(include_source=True)["payload"]["snippets"]
        self.graph.close()
        mcp = mcp_query_context(storage_path=str(self.db), query="calculate_invoice_total", code=True, include_source=True)
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            with contextlib.redirect_stdout(output):
                code = main(["query_context", "--query", "calculate_invoice_total", "--code", "--source", "--json"])
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, output.getvalue())
        cli = json.loads(output.getvalue())
        self.assertEqual(mcp["payload"]["snippets"], expected)
        self.assertEqual(cli["payload"]["snippets"], expected)

    def test_invalid_source_requests_are_rejected_at_shared_boundaries(self) -> None:
        for scopes in (None, ["docs"], ["code", "docs"]):
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                QueryContextRequest.from_raw(text="invoice", scopes=scopes, include_source=True)
        with self.assertRaises(TypeError):
            QueryContextRequest.from_raw(text="invoice", scopes=["code"], include_source="yes")
        with self.assertRaises(MCPToolError):
            mcp_query_context(storage_path=str(self.db), query="invoice", include_source=True)
        schema = next(item for item in list_tools() if item["name"] == "query_context")
        self.assertEqual(schema["inputSchema"]["properties"]["include_source"]["default"], False)


if __name__ == "__main__":
    unittest.main()
