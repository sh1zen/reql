"""Check generated REQL guidance against the supported Agent Workspace workflow."""
from __future__ import annotations

import unittest
from pathlib import Path

from agents.install import _skill_generator


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
        self.assertLess(main.index("reql project overview"), main.index("agent dashboard"))
        self.assertIn("goals, direction, workstreams", main)
        self.assertIn("--revision", main)
        self.assertIn("--supersedes", main)
        self.assertLess(len(main.split()), 400)
        self.assertIn("Stop when the owner, effects, and tests are clear", main)
        self.assertIn("Change the behavior owner", main)
        self.assertIn("Preserve supported extension points", main)
        self.assertIn("For individual record formats or session recovery", main)
        self.assertNotIn("For assigned tasks, single records", main)
        self.assertIn("It returns task context and the current revision", main)
        self.assertIn("Retrieve only missing context", main)
        self.assertIn("Use text output unless you need structured fields", main)
        self.assertIn("use write receipts for ids and revisions", main)
        self.assertIn("Read again for missing evidence, concurrent changes, or stale revisions", main)
        self.assertIn("inspect --node-id ID --limit 1`", main)
        self.assertNotIn("inspect --node-id ID --limit 1 --json", main)
        self.assertIn("coordinator runs integration checks and refreshes once after parallel work", main)
        self.assertIn("Workers reuse its context and return results", main)
        self.assertIn("Independent agents use `agent init", main)
        self.assertIn("Keep the host's identity and activity", main)
        self.assertIn("project context --task ID", main)
        self.assertIn("Reuse a supplied working set", main)
        self.assertIn("one stdin batch for linked records", main)
        self.assertIn("Use one full-suite run when it covers worker checks", main)
        self.assertIn("Pass that id instead of writing another handoff", main)
        self.assertIn("agent dashboard` only to recover a session", main)
        self.assertIn("Read only the necessary reference", main)
        self.assertIn("reql agent reject", workspace)
        self.assertIn("`project overview`", workspace)
        self.assertIn("needs no path or agent id", workspace)
        self.assertIn("Do not paste source files, broad search output", workspace)
        self.assertIn("removes the private store and sidecars", workspace)
        self.assertIn("Shared records remain after finish", workspace)
        self.assertIn("last eight revisions", workspace)
        self.assertIn("retention.agent_sessions", workspace)
        self.assertNotIn("without deleting private history", workspace)
        self.assertIn("Do not repeatedly compile a usable graph", bootstrap)
        self.assertIn("Keep the selected files with the task", query)
        self.assertLess(len(query.split()), 300)
        self.assertIn("Check base implementations, wrappers, and callers before editing", query)
        self.assertIn("Avoid replacement definitions or duplicate overrides", query)
        self.assertIn("query-advanced.md", query)
        advanced = resources["references/query-advanced.md"]
        self.assertIn("FINDINGS WHERE", advanced)
        self.assertIn("MATCH (s)-[:REFERENCES]->(t)", advanced)
        self.assertIn("serialization_paths", advanced)
        self.assertIn("agent init --name", main)
        self.assertIn("--task ID --revision N", main)
        self.assertIn("project compile --if-stopped", main)
        self.assertIn("Save each outcome once", main)
        self.assertIn("--next=", workspace)
        self.assertNotIn('--next ""', workspace)
        batch = resources["references/agent-batch.md"]
        self.assertIn("agent batch --name", batch)
        self.assertIn('"parent":{"ref":"goal"}', batch)
        self.assertIn("The whole batch is not atomic", batch)
        self.assertIn("They need no private sessions for that work", batch)
        self.assertIn("The coordinator records acceptance under its own identity", batch)
        self.assertIn('"expected_revision":1', batch)
        self.assertIn("omit unchanged `kind` and `content`", batch)
        self.assertIn("inside the revision-checked transaction", batch)
        self.assertIn("Do not query all records to confirm a successful write", batch)
        self.assertIn("Check failed or partial batches before retrying", batch)
        self.assertIn("'@ | reql agent batch", batch)
        self.assertIn("project context --task CHECKPOINT_ID", batch)
        self.assertIn("Do not create another receipt file or handoff document", batch)
        self.assertIn("Do not omit checks required by repository instructions", batch)

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
        self.assertIn("Save a failed approach", instructions)
        self.assertIn("Keep source reads within the task", instructions)
        self.assertIn("Change the implementation that owns the behavior", instructions)
        self.assertIn("Avoid duplicate wrappers and overrides", instructions)
