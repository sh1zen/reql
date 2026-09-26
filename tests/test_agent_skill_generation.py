"""Check generated REQL guidance against the supported Agent Workspace workflow."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agents.install import _skill_generator
from memory.artifacts.scanner import ProjectScanner


class AgentSkillGenerationTests(unittest.TestCase):
    """Keep the shared skill source and routed reference in sync."""

    def test_generated_skill_guides_bounded_discovery_and_cross_agent_recovery(self) -> None:
        generator = _skill_generator()
        options = {
            "platform_name": "codex",
            "project": True,
            "command_name": "reql",
            "command_path": Path("reql.cmd"),
            "fallback_command": "python cli.py",
        }
        main = dict(generator.skill_markdowns(**options))["reql-agent"]
        resources = {
            path: content
            for skill_name, path, content in generator.skill_resources(**options)
            if skill_name == "reql-agent"
        }
        workspace = resources["references/agent-workspace.md"]
        bootstrap = resources["references/bootstrap.md"]
        query = resources["references/query.md"]

        self.assertIn("reql project overview", main)
        self.assertLess(main.index("reql project overview"), main.index("reql agent dashboard"))
        self.assertIn("rejected, done, and open work across registered agents", main)
        self.assertIn("rescan the whole repository once the working set is known", main)
        self.assertIn("`project overview`, or `reql agent` -> `references/agent-workspace.md`", main)
        self.assertIn("reql agent reject \"Cache every query\"", workspace)
        self.assertIn("reql project overview\n", workspace)
        self.assertIn("needs no path or agent id", workspace)
        self.assertIn("Do not paste source files, broad search output", workspace)
        self.assertIn("Do not repeatedly compile a healthy graph", bootstrap)
        self.assertIn("Keep the working set in the task", query)

    def test_shared_instruction_rule_mentions_recovery_and_bounded_reads(self) -> None:
        generator = _skill_generator()
        instructions = generator.instruction_section(
            "codex",
            project=True,
            command_name="reql",
            command_path=Path("reql.cmd"),
            fallback_command="python cli.py",
            supported_clients="Codex",
            section_start="<!-- REQL:START -->",
            section_end="<!-- REQL:END -->",
        )
        self.assertIn("read `project overview` from the project directory", instructions)
        self.assertIn("Record a discarded approach", instructions)
        self.assertIn("Keep retrieval and source reads bounded", instructions)

    def test_project_scan_skips_generated_local_skill(self) -> None:
        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            generated = root / ".codex" / "skills" / "reql-agent"
            generated.mkdir(parents=True)
            (generated / "SKILL.md").write_text("generated", encoding="utf-8")
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            paths = [artifact.relative_path for artifact in ProjectScanner().scan(root).artifacts]
            self.assertEqual(paths, ["app.py"])
