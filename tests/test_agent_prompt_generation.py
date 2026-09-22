from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agents.install import _planned_files, _skill_generator


class AgentPromptGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = _skill_generator()
        self.values = {
            "platform_name": "Codex",
            "project": True,
            "command_name": "reql",
            "command_path": Path("/tools/reql"),
            "fallback_command": "python -m memory.cli",
        }

    def test_shared_rule_defines_a_graph_owned_working_set(self) -> None:
        rule = self.generator.shared_rule_body(
            "Codex",
            command_name="reql",
            command_path=Path("/tools/reql"),
            fallback_command="python -m memory.cli",
            section_start="<!-- start -->",
            section_end="<!-- end -->",
        )

        self.assertIn("let its paths, owners, spans, and associated tests define the working set", rule)
        self.assertIn("Deepen or refine graph queries when evidence is missing", rule)
        self.assertIn("agent dashboard", rule)
        self.assertIn("durable pipeline state", rule)
        self.assertIn("public Context", rule)
        self.assertIn("private dashboard", rule)
        self.assertNotIn("handoff", rule)
        self.assertNotIn("agent note --resume", rule)

    def test_prompt_compiles_only_for_bootstrap_or_changed_files(self) -> None:
        rule = self.generator.shared_rule_body(
            "Codex",
            command_name="reql",
            command_path=Path("/tools/reql"),
            fallback_command="python -m memory.cli",
            section_start="<!-- start -->",
            section_end="<!-- end -->",
        )
        resources = {path: content for _, path, content in self.generator.skill_resources(**self.values)}

        self.assertIn("Skip both `watch-status` and compile for read-only tasks", rule)
        self.assertIn(
            "compile only to bootstrap a missing graph or after the current task changes project files",
            resources["agents/openai.yaml"],
        )

    def test_query_reference_reserves_source_lookup_for_specific_gaps(self) -> None:
        resources = {path: content for _, path, content in self.generator.skill_resources(**self.values)}
        query_reference = resources["references/query.md"]

        self.assertIn("## Graph-led source inspection", query_reference)
        self.assertIn("specific gap the graph leaves unresolved", query_reference)
        self.assertNotIn("workspace-wide `rg`", query_reference)
        self.assertNotIn("`grep -R`", query_reference)

    def test_agent_reference_keeps_canonical_graph_out_of_operational_memory(self) -> None:
        resources = {path: content for _, path, content in self.generator.skill_resources(**self.values)}
        agent_reference = resources["references/agent-workspace.md"]

        self.assertIn("canonical project graph remains the only source of repository facts", agent_reference)
        self.assertNotIn("agent sync", agent_reference)
        self.assertNotIn("agent link-task", agent_reference)
        self.assertNotIn("STANDARD_FILE", agent_reference)
        self.assertNotIn("STANDARD_SYMBOL", agent_reference)

    def test_agent_reference_uses_dashboard_for_intra_and_inter_session_coordination(self) -> None:
        resources = {path: content for _, path, content in self.generator.skill_resources(**self.values)}
        agent_reference = resources["references/agent-workspace.md"]

        self.assertIn("agent dashboard", agent_reference)
        self.assertIn("public dashboard", agent_reference)
        self.assertIn("private dashboard", agent_reference)
        self.assertIn("agent finish", agent_reference)
        self.assertIn("agent note --public", agent_reference)
        self.assertIn("agent note --agent", agent_reference)
        self.assertIn("agent init --name", agent_reference)
        self.assertIn("agent task done TASK_ID", agent_reference)
        self.assertIn("agent terminate AGENT_ID", agent_reference)
        self.assertNotIn("{command_name} agent add", agent_reference)
        self.assertNotIn("{command_name} agent overview", agent_reference)
        self.assertNotIn("{command_name} agent publish", agent_reference)
        self.assertNotIn("agent session start", agent_reference)
        self.assertNotIn("agent checkpoint", agent_reference)
        self.assertNotIn("agent link", agent_reference)
        self.assertNotIn("{command_name} agent link-many", agent_reference)
        self.assertNotIn("agent handoff", agent_reference)
        self.assertNotIn("agent bus", agent_reference)
        self.assertNotIn("agent map", agent_reference)

    def test_codex_install_plan_generates_dashboard_only_agent_guidance(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp") as temp_dir:
            root = Path(temp_dir)
            files = _planned_files(
                "codex",
                project=True,
                project_dir=root,
                home_dir=root,
                command_name="reql",
                command_path=Path("/tools/reql"),
                fallback_command="python -m memory.cli",
            )

        generated = "\n".join(
            content
            for kind, path, content in files
            if kind in {"skill", "skill-resource"}
            and path.is_relative_to(root / ".codex" / "skills" / "reql-agent")
        )

        self.assertIn("agent dashboard", generated)
        self.assertIn("public Context", generated)
        self.assertIn("private dashboard", generated)
        self.assertNotIn("agent handoff", generated)
        self.assertNotIn("agent bus", generated)
        self.assertNotIn("agent map", generated)
        self.assertNotIn("note --resume", generated)


if __name__ == "__main__":
    unittest.main()
