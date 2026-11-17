"""Exercise installed skill workflows through real compilation and CLI processes."""
from __future__ import annotations

import json
import os
import re
import shlex
from argparse import ArgumentParser
from contextlib import closing
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.install import PLATFORMS_CONFIG, install_agent_files
from api import MemoryGraph
from memory.artifacts.project import ProjectRegistry
from memory.artifacts.scanner import ProjectScanner
from memory.cli import build_parser
from memory.query import parse_reql


class SkillWorkflowTests(unittest.TestCase):
    """Protect failures found during the isolated skill evaluation."""

    def setUp(self) -> None:
        scratch = Path(".tmp")
        scratch.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "invoice service"
        self.root.mkdir()
        self.storage = self.root / ".reql" / "memory.reql"

    def write(self, relative: str, content: str) -> None:
        """Keep source and generated evaluation artifacts within the temporary root."""
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_status_distinguishes_registration_failed_compile_and_recovery(self) -> None:
        self.write("invoices.py", "def settle_invoice(amount):\n    return amount > 0\n")
        for index in range(6):
            self.write(f"batch_{index}.py", "value = 1\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertIsNone(graph.project_status(self.root))
            ProjectRegistry(graph.store).scan_path(self.root)
            self.assertIsNone(graph.project_status(self.root)["last_compilation"])
            with patch("memory.code_analysis.base.TreeSitterCodeParser.parse_artifact",
                       side_effect=ModuleNotFoundError("missing parser dependency")):
                failed = graph.compile_project(self.root)
            self.assertEqual(failed.run.status, "failed")
            status = graph.project_status(self.root)
            self.assertEqual(status["project"]["status"], "active")
            self.assertEqual(status["last_compilation"]["status"], "failed")
            self.assertEqual(status["last_compilation"]["id"], failed.run.id)
            self.assertEqual(status["last_compilation"]["error_count"], 7)
            self.assertEqual(len(status["last_compilation"]["errors"]), 5)
            self.assertIn("missing parser dependency", status["last_compilation"]["errors"][0])
            # Another project's newer run must not mask this project's failure.
            other = self.root / "other"
            other.mkdir()
            (other / "other.py").write_text("value = 1\n", encoding="utf-8")
            graph.compile_project(other)
            self.assertEqual(graph.project_status(self.root)["last_compilation"]["id"], failed.run.id)
            recovered = graph.compile_project(self.root)
            status = graph.project_status(self.root)
            self.assertEqual(status["last_compilation"]["status"], "completed")
            self.assertEqual(status["last_compilation"]["id"], recovered.run.id)
            self.assertEqual(status["last_compilation"]["error_count"], 0)
            self.assertEqual(status["last_compilation"]["errors"], [])

    def test_installed_command_supports_bootstrap_question_resolution_and_recovery(self) -> None:
        """Recover questions and file-scoped failures through installed commands across sessions."""
        self.write("invoices.py", "def settle_invoice(amount):\n    return amount > 0\n")
        test_paths = {"qa/test_invoices.py", "packages/billing/invoices_test.py", "tests/test_contract.py"}
        for path in sorted(test_paths):
            self.write(path, "from invoices import settle_invoice\n\ndef test_positive_amount():\n    assert settle_invoice(10)\n")
        self.write("docs/requirements.md", "# Invoice settlement\nOPEN: Does the budget include the first attempt?\n")
        installed = install_agent_files(("codex", "claude"), project=True, project_dir=self.root,
                                        command_dir=self.root.parent / "command tools", hooks=False)
        command = next(item.path for item in installed.actions if item.kind == "command")
        self.write(".claude/skills/team-conventions/SKILL.md", "# Maintained team conventions\n")
        paths = {artifact.relative_path for artifact in ProjectScanner().scan(self.root).artifacts}
        self.assertFalse(any("skills/reql-agent/" in path or "skills/reql-context-compact/" in path for path in paths))
        self.assertIn(".claude/skills/team-conventions/SKILL.md", paths)
        self.assertIn("AGENTS.md", paths)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")

        def run(*args: str, expected: int = 0) -> str:
            """Invoke the real installed shim, including executable paths containing spaces."""
            result = subprocess.run([str(command), *args], cwd=self.root, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            return result.stdout

        run("project", "status", expected=1)
        run("project", "compile")
        self.assertIn("Last compilation: completed", run("project", "status"))
        status = json.loads(run("project", "status", "--json"))
        self.assertEqual(status["last_compilation"]["status"], "completed")
        self.assertEqual(status["last_compilation"]["error_count"], 0)
        context = json.loads(run("query_context", "--query", "settle_invoice", "--code", "--json"))
        self.assertEqual({row["path"] for row in context["payload"]["test_targets"]}, test_paths)
        for target in context["payload"]["test_targets"]:
            self.assertEqual(target["kind"], "test")
            self.assertTrue((self.root / target["path"]).is_file())
            self.assertGreater(target["line_start"], 0)
        self.assertIn("requirements.md", run("query_context", "--query", "docs/requirements.md", "--docs"))
        run("agent", "init", "--name", "Clarify settlement budget")
        question = json.loads(run("agent", "record", "question", "Does the budget include the first attempt?",
                                  "--file", "invoices.py", "--next", "Ask product", "--json"))["node"]
        resolved = json.loads(run("agent", "record", "question", question["content"], "--id", question["id"],
                                  "--revision", str(question["revision"]), "--status", "resolved",
                                  "--why", "Confirmed: three total attempts", "--next", "", "--json"))["node"]
        self.assertEqual(resolved["next_action"], "")
        failure = json.loads(run("agent", "record", "failure", "Collapse duplicate guards",
                                 "--why", "test_contract rejects omitted keys",
                                 "--file", "invoices.py", "--file", "tests/test_contract.py", "--json"))["node"]
        run("agent", "finish", "Budget clarified; implementation remains")
        fresh = json.loads(run("agent", "--agent", "agent:fresh", "init",
                               "--name", "Review current contracts", "--json"))
        self.assertNotEqual(fresh["agent_id"], failure["agent_id"])
        recovered = json.loads(run("project", "context", "--file", "invoices.py", "--json"))
        retained = next(item for item in recovered["records"] if item["id"] == question["id"])
        self.assertEqual(retained["status"], "resolved")
        self.assertEqual(retained["next_action"], "")
        self.assertIn("three total attempts", retained["rationale"])
        for path in ("invoices.py", "tests/test_contract.py"):
            scoped = json.loads(run("project", "context", "--file", path, "--json"))
            remembered = next(item for item in scoped["records"] if item["id"] == failure["id"])
            self.assertEqual(remembered["content"], failure["content"])
            self.assertEqual(remembered["rationale"], failure["rationale"])
            self.assertEqual(remembered["status"], "active")
            self.assertIn("file", remembered["reasons"])
            self.assertIn(failure["rationale"], run("project", "context", "--file", path))
        unrelated = json.loads(run("project", "context", "--file", "docs/requirements.md", "--json"))
        self.assertNotIn(failure["id"], {item["id"] for item in unrelated["records"]})
        current = json.loads(run("query_context", "--query", "settle_invoice", "--code", "--json"))
        attached = next(item for item in current["payload"]["engineering_context"]["records"]
                        if item["id"] == failure["id"])
        self.assertIn("file", attached["reasons"])
        self.assertIn(failure["rationale"], run("query_context", "--query", "settle_invoice", "--code"))
        run("agent", "--agent", "agent:fresh", "finish", "Shared history recovered; no changes")
        for platform in ("codex", "claude"):
            bundle = self.root / f".{platform}" / "skills" / "reql-agent"
            self.assertTrue((bundle / "SKILL.md").is_file())
            self.assertTrue((bundle / "references" / "bootstrap.md").is_file())
            self.assertTrue((bundle / "references" / "query-advanced.md").is_file())

    def test_generated_bundles_have_reachable_references_and_valid_command_examples(self) -> None:
        """Check every platform and scope for broken routes and unsupported CLI examples."""
        parser = build_parser()
        for project in (True, False):
            for platform in PLATFORMS_CONFIG:
                with self.subTest(project=project, platform=platform):
                    root = self.root / platform / ("project" if project else "user")
                    home = root / "home"
                    with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home / ".claude")}):
                        installed = install_agent_files(
                            (platform,), project=project, project_dir=root, home_dir=home,
                            command_dir=root / "bin", hooks=True)
                    for action in installed.actions:
                        if action.kind == "hook":
                            self.check_hook_output(action.path, platform=platform, root=root)
                        if action.kind == "skill":
                            self.check_bundle(action.path, parser=parser)

    def check_bundle(self, skill: Path, *, parser: ArgumentParser) -> None:
        """Traverse installed reference links and parse their executable examples."""
        bundle = skill.parent
        pending = [skill]
        visited = set()
        while pending:
            document = pending.pop()
            if document in visited:
                continue
            visited.add(document)
            text = document.read_text(encoding="utf-8")
            for reference in re.findall(r"`((?:references/)?[a-z][a-z-]*\.md)`", text):
                target = (document.parent / reference).resolve()
                self.assertTrue(target.is_relative_to(bundle), reference)
                self.assertTrue(target.is_file(), reference)
                pending.append(target)
            for line in text.splitlines():
                if line.startswith("reql "):
                    arguments = shlex.split(line)[1:]
                    arguments = ["1" if value in {"N", "CURRENT_N"} else value for value in arguments]
                    parsed = parser.parse_args(arguments)
                    if parsed.command == "query":
                        parse_reql(" ".join(parsed.statement))
        self.assertTrue(set((bundle / "references").glob("*.md")).issubset(visited))

    def check_hook_output(self, settings_path: Path, *, platform: str, root: Path) -> None:
        """Execute installed shell hooks with and without a REQL project marker."""
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = [hook for entries in settings["hooks"].values() for entry in entries for hook in entry["hooks"]]
        for hook in hooks:
            absent = subprocess.run(hook["command"], shell=True, cwd=root, capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(absent.returncode, 0, absent.stderr)
            self.assertEqual(absent.stdout, "")
            (root / "reql.conf").write_text("", encoding="utf-8")
            present = subprocess.run(hook["command"], shell=True, cwd=root, capture_output=True,
                                     text=True, timeout=10)
            self.assertEqual(present.returncode, 0, present.stderr)
            payload = json.loads(present.stdout)
            if platform == "claude":
                context = payload["hookSpecificOutput"]["additionalContext"]
            else:
                self.assertEqual(payload["decision"], "allow")
                context = payload["additionalContext"]
            self.assertTrue(context)
            (root / "reql.conf").unlink()


if __name__ == "__main__":
    unittest.main()
