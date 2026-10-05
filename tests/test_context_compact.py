"""Verify conservative transcript compaction and explicit skill installation."""
from __future__ import annotations

import unittest
from pathlib import Path

from agents.context_compact import compact_transcript
from agents.install import _planned_files, _skill_generator


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

    def test_skill_is_explicit_only_for_supported_agents(self) -> None:
        generator = _skill_generator()
        options = {
            "project": True,
            "command_name": "reql",
            "command_path": Path("reql.cmd"),
            "fallback_command": "python cli.py",
        }
        codex = dict(generator.skill_markdowns(platform_name="codex", **options))["reql-context-compact"]
        claude = dict(generator.skill_markdowns(platform_name="claude", **options))["reql-context-compact"]
        cursor = dict(generator.skill_markdowns(platform_name="cursor", **options))["reql-context-compact"]
        resources = {
            path: content for skill, path, content in generator.skill_resources(platform_name="codex", **options)
            if skill == "reql-context-compact"
        }
        self.assertIn("/reql-context-compact", codex)
        self.assertIn("disable-model-invocation: true", claude)
        self.assertIn("disable-model-invocation: true", cursor)
        self.assertIn("allow_implicit_invocation: false", resources["agents/openai.yaml"])
        planned = _planned_files("codex", project_dir=Path("example"), **options)
        self.assertTrue(any(path.name == "SKILL.md" and path.parent.name == "reql-context-compact" for _, path, _ in planned))
        for platform, expected in (("cursor", ".cursor"), ("copilot", ".github")):
            platform_files = _planned_files(platform, project_dir=Path("example"), **options)
            self.assertTrue(any(path.name == "SKILL.md" and expected in path.parts for _, path, _ in platform_files))


if __name__ == "__main__":
    unittest.main()
