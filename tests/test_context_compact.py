"""Verify conservative transcript compaction and explicit skill installation."""
from __future__ import annotations

import unittest

from agents.context_compact import compact_transcript


class ContextCompactTests(unittest.TestCase):
    """Protect transcript fidelity and opt-in skill routing."""

    def test_only_old_exact_duplicate_tool_text_is_replaced(self) -> None:
        body = "source line\n" * 100
        messages = [
            {"role": "system", "content": body},
            {"role": "tool", "name": "read_file", "tool_call_id": "a", "content": body},
            {"role": "assistant", "content": body},
            {"role": "tool", "name": "read_file", "tool_call_id": "b", "content": body},
            {"role": "tool", "name": "other", "content": body},
            {"role": "tool", "name": "read_file", "content": body, "isError": True},
            {"role": "user", "content": "continue"},
            {"role": "tool", "name": "read_file", "content": body, "status": "failed"},
            {"role": "tool", "name": "read_file", "content": body, "success": False},
            {"role": "assistant", "content": "working"},
            {"role": "tool", "name": "read_file", "content": body},
        ]
        compacted, saved = compact_transcript({"messages": messages, "metadata": 3})
        self.assertEqual(compacted["metadata"], 3)
        self.assertEqual(compacted["messages"][1], messages[1])
        self.assertEqual(compacted["messages"][3]["tool_call_id"], "b")
        self.assertIn("exact duplicate of tool result at message 1", compacted["messages"][3]["content"])
        self.assertEqual(compacted["messages"][4:], messages[4:])
        self.assertEqual(saved, len(body) - len(compacted["messages"][3]["content"]))
        self.assertEqual(messages[3]["content"], body)

    def test_unknown_and_different_content_pass_through(self) -> None:
        body = "x" * 1000
        messages = [
            {"role": "tool", "name": "read", "content": body},
            {"role": "tool", "name": "read", "content": body + "changed"},
            {"role": "tool", "name": "read", "content": [body]},
            {"role": "tool", "content": body},
            {"role": "tool", "name": "read", "content": body, "status": ["failed"]},
        ]
        self.assertEqual(compact_transcript(messages), (messages, 0))
        with self.assertRaises(ValueError):
            compact_transcript({"other": messages})

if __name__ == "__main__":
    unittest.main()
