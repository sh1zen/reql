"""Protect MCP stdio framing, read-only policy and error responses."""
from __future__ import annotations

from io import StringIO
import json
import unittest
from unittest.mock import patch

from mcp.server import serve
from mcp.tools import MCPToolError


class MCPResponseEncodingTests(unittest.TestCase):
    """Exercise the common response path and stdio framing at the handler boundary."""

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


if __name__ == "__main__":
    unittest.main()
