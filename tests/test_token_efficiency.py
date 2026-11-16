"""Preserve complete evidence while reducing transport and presentation repetition."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from mcp.server import _handle_request, serve
from mcp.tools import MCPToolError
from memory.services.coordination import CoordinationStore, render_work_context, render_work_overview


class MCPResponseEncodingTests(unittest.TestCase):
    """Exercise the common response path and stdio framing at the handler boundary."""

    def test_success_json_is_compact_and_lossless(self) -> None:
        """Formatting must not change nested types, source whitespace or Unicode text."""
        payload = {"text": 'Città\n    return "{}"\n', "rows": [0, False, None, {"id": "n1", "score": 0.25}],
                   "empty": {}, "literal": "\\n"}
        request = {"jsonrpc": "2.0", "id": "req1", "method": "tools/call",
                   "params": {"name": "query_context", "arguments": {"query": "billing"}}}
        original = deepcopy(payload)
        with patch("mcp.server.call_tool", return_value=payload) as handler:
            response = _handle_request(request, include_write=False, allowed_tools={"query_context"})
        handler.assert_called_once_with("query_context", {"query": "billing"})
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["id"], "req1")
        text = response["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), original)
        self.assertEqual(payload, original)
        self.assertEqual(text, json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        self.assertLess(len(text), len(json.dumps(original, ensure_ascii=False, indent=2, sort_keys=True)))

    def test_stdio_retains_framing_read_only_policy_and_actionable_errors(self) -> None:
        """An encoded source newline cannot split frames or alter error envelopes."""
        requests = [{"jsonrpc": "2.0", "id": index, "method": "tools/call",
                     "params": {"name": name, "arguments": {}}}
                    for index, name in enumerate(("query_context", "query_context", "reql_work_record"), 1)]
        output = StringIO()
        with patch("mcp.server.call_tool", side_effect=[{"source": "a\n b"}, MCPToolError("Missing query")]) as handler:
            self.assertEqual(serve(StringIO("\n".join(json.dumps(item) for item in requests)), output,
                                   include_write=False), 0)
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(frames), 3)
        self.assertEqual(json.loads(frames[0]["result"]["content"][0]["text"]), {"source": "a\n b"})
        self.assertEqual(frames[1]["error"], {"code": -32602, "message": "Missing query"})
        self.assertIn("Tool is not available", frames[2]["error"]["message"])
        self.assertEqual(handler.call_count, 2)


class ContextTokenEfficiencyTests(unittest.TestCase):
    """Use real stored work and compiled files to check presentation provenance."""

    def setUp(self) -> None:
        scratch = Path.cwd() / ".tmp" / "regression-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = CoordinationStore(self.root / "work.reql")

    def put(self, content: str, *, agent: str = "agent:0123456789abcdef01234567", **fields) -> dict:
        """Create independent records through the shared transactional owner."""
        return self.work.put("task", content, agent_id=agent, session_id=None, **fields)["node"]

    def test_repeated_owners_keep_one_exact_identity_and_distinct_evidence(self) -> None:
        """An attribution key saves repeated hashes without merging tasks or owners."""
        owner = "agent:0123456789abcdef01234567"
        first = self.put("billing contract", files=["src/billing.py"])
        second = self.put("billing consumers", depends_on=[first["id"]], files=["tests/test_billing.py"])
        other = self.put("billing integration", agent="agent:fedcba9876543210fedcba98")
        payload = self.work.context("billing")
        original = deepcopy(payload)
        text = render_work_context(payload)
        self.assertEqual(text.count(owner), 1)
        self.assertRegex(text, r"\(owner1=agent:0123456789abcdef01234567\)")
        self.assertIn("(owner1)", text)
        for item in (first, second, other):
            self.assertIn(item["id"] + " r1", text)
            self.assertIn(item["content"], text)
        self.assertIn("waiting: " + first["id"], text)
        self.assertIn("task/blocked", text)
        self.assertIn("src/billing.py", text)
        self.assertIn("tests/test_billing.py", text)
        self.assertIn(other["agent_id"], text)
        self.assertEqual(payload, original)
        self.assertEqual(render_work_context(payload), text)

    def test_single_or_short_owner_needs_no_attribution_key(self) -> None:
        """Do not add a legend when repeating the complete identity is cheaper."""
        task = self.put("one record")
        text = render_work_context(self.work.context(record_id=task["id"], limit=1))
        self.assertIn("(" + task["agent_id"] + ")", text)
        self.assertNotIn("owner1", text)
        self.put("short owner first", agent="a")
        self.put("short owner second", agent="a")
        text = render_work_context(self.work.context("short owner"))
        self.assertNotIn("owner1", text)

    def test_attribution_keys_cannot_shadow_an_actual_agent_identity(self) -> None:
        """A short real id such as owner1 must remain distinct from a response key."""
        self.put("billing first")
        self.put("billing second")
        self.put("billing third", agent="owner1")
        text = render_work_context(self.work.context("billing"))
        self.assertNotIn("(owner1=", text)
        self.assertIn("(owner1)", text)
        self.assertIn("(owner2=agent:0123456789abcdef01234567)", text)
        self.assertIn("(owner2)", text)

    def test_overview_keys_are_declared_before_use_even_with_budget_cuts(self) -> None:
        """No alias may point into a row omitted by a section's character budget."""
        self.put("Very long contract. " * 50)
        self.put("Validate consumer")
        payload = self.work.overview()
        original = deepcopy(payload)
        for budget in (200, 350, 700, 1200):
            with self.subTest(budget=budget):
                text = render_work_overview(payload, max_chars_per_section=budget)
                for match in re.finditer(r"\((owner\d+)\)", text):
                    self.assertIn("(" + match[1] + "=", text[:match.start()])
        self.assertEqual(payload, original)
        independent = render_work_context(self.work.context("consumer"))
        self.assertNotIn("(owner1)", independent)

    def test_module_identity_is_supplied_by_the_file_and_tests_remain_visible(self) -> None:
        """Test files already in the working set must not be reported as missing tests."""
        project = self.root / "project"
        test_dir = project / "tests"
        test_dir.mkdir(parents=True)
        (test_dir / "test_invoice.py").write_text("def test_invoice_total():\n    assert sum([2, 3]) == 5\n", encoding="utf-8")
        with closing(MemoryGraph.open(self.root / "memory.reql")) as graph:
            self.assertFalse(graph.compile_project(project).run.errors)
            payload = graph.query_context_payload("tests/test_invoice.py", scopes=["test"])["payload"]
            self.assertTrue(payload["test_targets"])
            self.assertTrue(any(row["path"] == "tests/test_invoice.py" for row in payload["working_set"]))
            original = deepcopy(payload)
            text = graph.retrieval.render_context_payload(payload)
            self.assertNotIn("No associated tests found", text)
            self.assertIn("Tests included in Files above", text)
            self.assertNotIn("tests.test_invoice", text.split("## Files", 1)[1].split("##", 1)[0])
            self.assertIn("(module)", text)
            self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
