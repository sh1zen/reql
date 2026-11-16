"""Exercise installed skill workflows through real compilation and CLI processes."""
from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.install import install_agent_files
from api import MemoryGraph
from memory.artifacts.project import ProjectRegistry
from memory.artifacts.scanner import ProjectScanner


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

    def test_code_context_retains_supported_test_locations(self) -> None:
        self.write("invoices.py", "def settle_invoice(amount):\n    return amount > 0\n")
        for path in ("qa/test_invoices.py", "packages/billing/invoices_test.py", "tests/test_contract.py"):
            self.write(path, "from invoices import settle_invoice\n\ndef test_positive_amount():\n    assert settle_invoice(10)\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            compiled = graph.compile_project(self.root)
            self.assertFalse(compiled.run.errors)
            result = graph.query_context_payload("settle_invoice", scopes=["code"])
            targets = {item["path"] for item in result["payload"]["test_targets"]}
            self.assertEqual(targets, {"qa/test_invoices.py", "packages/billing/invoices_test.py", "tests/test_contract.py"})
            for target in result["payload"]["test_targets"]:
                self.assertEqual(target["kind"], "test")
                self.assertTrue((self.root / target["path"]).is_file())
                self.assertGreater(target["line_start"], 0)

    def test_installed_command_supports_bootstrap_question_resolution_and_recovery(self) -> None:
        self.write("invoices.py", "def settle_invoice(amount):\n    return amount > 0\n")
        self.write("qa/test_invoices.py", "from invoices import settle_invoice\n\ndef test_positive_amount():\n    assert settle_invoice(10)\n")
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
        context = json.loads(run("query_context", "--query", "settle_invoice", "--code", "--json"))
        self.assertIn("qa/test_invoices.py", {row["path"] for row in context["payload"]["test_targets"]})
        self.assertIn("requirements.md", run("query_context", "--query", "docs/requirements.md", "--docs"))
        run("agent", "init", "--name", "Clarify settlement budget")
        question = json.loads(run("agent", "record", "question", "Does the budget include the first attempt?",
                                  "--file", "invoices.py", "--next", "Ask product", "--json"))["node"]
        resolved = json.loads(run("agent", "record", "question", question["content"], "--id", question["id"],
                                  "--revision", str(question["revision"]), "--status", "resolved",
                                  "--why", "Confirmed: three total attempts", "--next", "", "--json"))["node"]
        self.assertEqual(resolved["next_action"], "")
        run("agent", "finish", "Budget clarified; implementation remains")
        recovered = json.loads(run("project", "context", "--file", "invoices.py", "--json"))
        retained = next(item for item in recovered["records"] if item["id"] == question["id"])
        self.assertEqual(retained["status"], "resolved")
        self.assertEqual(retained["next_action"], "")
        self.assertIn("three total attempts", retained["rationale"])
        for platform in ("codex", "claude"):
            bundle = self.root / f".{platform}" / "skills" / "reql-agent"
            self.assertTrue((bundle / "SKILL.md").is_file())
            self.assertTrue((bundle / "references" / "bootstrap.md").is_file())
            self.assertTrue((bundle / "references" / "query-advanced.md").is_file())


if __name__ == "__main__":
    unittest.main()
