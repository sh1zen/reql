from __future__ import annotations

from pathlib import Path
import unittest

from agents.install import _skill_generator


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

        self.assertIn("Agent memory never copies or synchronizes those records", agent_reference)
        self.assertNotIn("agent sync", agent_reference)
        self.assertNotIn("agent link-task", agent_reference)
        self.assertNotIn("STANDARD_FILE", agent_reference)
        self.assertNotIn("STANDARD_SYMBOL", agent_reference)

    def test_agent_reference_uses_dashboard_for_intra_and_inter_session_coordination(self) -> None:
        resources = {path: content for _, path, content in self.generator.skill_resources(**self.values)}
        agent_reference = resources["references/agent-workspace.md"]

        self.assertIn("agent dashboard", agent_reference)
        self.assertIn("compact attention index", agent_reference)
        self.assertIn("intra-session", agent_reference)
        self.assertIn("inter-session", agent_reference)
        self.assertIn("stage transitions", agent_reference)
        self.assertIn("follow that command", agent_reference)
        self.assertIn("agent finish", agent_reference)
        self.assertIn("agent note add", agent_reference)
        self.assertIn("agent dashboard --agents", agent_reference)
        self.assertNotIn("{command_name} agent add", agent_reference)
        self.assertNotIn("{command_name} agent overview", agent_reference)
        self.assertNotIn("{command_name} agent publish", agent_reference)
        self.assertNotIn("{command_name} agent link-many", agent_reference)


if __name__ == "__main__":
    unittest.main()
