from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory.artifacts.scanner import ProjectScanner
from memory.cli import _append_config_exclude_patterns
from memory.config import ConfigError, load_effective_config, resolve_scan_exclude_pattern


class ScanExcludeRuleTests(unittest.TestCase):
    def test_supported_grammar_is_resolved_uniformly(self) -> None:
        supported = {
            "dir": "dir",
            "dir/": "dir",
            "./dir": "./dir",
            "./dir/": "./dir",
            "file.py": "file.py",
            "./file.py": "./file.py",
            "*.extension": "*.extension",
            "./*.extension": "./*.extension",
            "dir/*.ext": "dir/*.ext",
            "./dir/*ext": "./dir/*ext",
        }

        for raw, normalized in supported.items():
            with self.subTest(raw=raw):
                self.assertEqual(resolve_scan_exclude_pattern(raw).normalized, normalized)

    def test_unsupported_formats_are_rejected(self) -> None:
        unsupported = [
            "",
            ".",
            "./",
            "/dir",
            "C:/dir",
            "../dir",
            "dir/../file",
            "dir//file",
            "dir\\file",
            "*",
            "**/*.py",
            "dir/*",
            "dir/file*.py",
            "dir/?ile.py",
            "dir/[ab].py",
            " dir",
            "dir ",
        ]

        for raw in unsupported:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "Invalid scan.exclude pattern"):
                resolve_scan_exclude_pattern(raw)


class ProjectScannerExclusionTests(unittest.TestCase):
    def test_anchor_is_relative_to_config_directory_not_compile_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            compiled_subpath = project / "service"
            compiled_subpath.mkdir(parents=True)
            config_path = project / "reql.conf"
            config_path.write_text("scan:\n  exclude: []\n", encoding="utf-8")
            self._write_files(compiled_subpath, ["only-here.py", "everywhere.py"])

            result = ProjectScanner(
                exclude_patterns=["./only-here.py", "everywhere.py"],
                config_path=config_path,
                use_default_ignores=False,
            ).scan(compiled_subpath)
            included = {artifact.relative_path for artifact in result.artifacts}

            self.assertIn("only-here.py", included)
            self.assertNotIn("everywhere.py", included)

    def test_anchoring_depth_and_final_suffix_share_one_matching_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            config_path = root / "reql.conf"
            config_path.write_text("scan:\n  exclude: []\n", encoding="utf-8")
            files = [
                "current_dir/root.py",
                "nested/current_dir/nested.py",
                "current.txt",
                "nested/current.txt",
                "nested/everywhere/hidden.py",
                "all.txt",
                "nested/all.txt",
                "root.tmp",
                "nested/nested.tmp",
                "root.log",
                "nested/nested.log",
                "bucket/root.gen",
                "bucket/deeper/root.gen",
                "nested/bucket/nested.gen",
                "nested/bucket/deeper/nested.gen",
                "scoped/root.cache",
                "scoped/deeper/root.cache",
                "nested/scoped/nested.cache",
            ]
            self._write_files(root, files)

            result = ProjectScanner(
                exclude_patterns=[
                    "./current_dir",
                    "./current.txt",
                    "everywhere",
                    "all.txt",
                    "./*.tmp",
                    "*.log",
                    "bucket/*.gen",
                    "./scoped/*cache",
                ],
                config_path=config_path,
                use_default_ignores=False,
            ).scan(root)
            included = {artifact.relative_path for artifact in result.artifacts}
            excluded = {item.relative_path for item in result.skipped_files if item.reason == "excluded"}

            self.assertIn("nested/current_dir/nested.py", included)
            self.assertIn("nested/current.txt", included)
            self.assertIn("nested/nested.tmp", included)
            self.assertIn("nested/scoped/nested.cache", included)
            self.assertIn("current_dir", excluded)
            self.assertIn("current.txt", excluded)
            self.assertIn("nested/everywhere", excluded)
            self.assertNotIn("all.txt", included)
            self.assertNotIn("nested/all.txt", included)
            self.assertNotIn("root.log", included)
            self.assertNotIn("nested/nested.log", included)
            self.assertNotIn("bucket/root.gen", included)
            self.assertNotIn("bucket/deeper/root.gen", included)
            self.assertNotIn("nested/bucket/nested.gen", included)
            self.assertNotIn("nested/bucket/deeper/nested.gen", included)
            self.assertNotIn("root.tmp", included)
            self.assertNotIn("scoped/root.cache", included)
            self.assertNotIn("scoped/deeper/root.cache", included)

    def test_nested_config_exclusions_apply_only_to_their_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            module = root / "module"
            module.mkdir(parents=True)
            (module / "reql.conf").write_text(
                "scan:\n"
                "  exclude:\n"
                "    - ./local.py\n"
                "    - everywhere.py\n"
                "    - '*.tmp'\n"
                "    - generated/*.snap\n"
                "    - ./direct/*cache\n",
                encoding="utf-8",
            )
            files = [
                "local.py",
                "root.tmp",
                "generated/root.snap",
                "module/local.py",
                "module/deeper/local.py",
                "module/everywhere.py",
                "module/deeper/everywhere.py",
                "module/direct/a.cache",
                "module/direct/deeper/a.cache",
                "module/deeper/direct/a.cache",
                "module/a.tmp",
                "module/deeper/a.tmp",
                "module/generated/a.snap",
                "module/generated/deeper/a.snap",
                "module/deeper/generated/a.snap",
            ]
            self._write_files(root, files)

            result = ProjectScanner(use_default_ignores=False).scan(root)
            included = {artifact.relative_path for artifact in result.artifacts}

            self.assertIn("local.py", included)
            self.assertIn("root.tmp", included)
            self.assertIn("generated/root.snap", included)
            self.assertIn("module/deeper/local.py", included)
            self.assertIn("module/deeper/direct/a.cache", included)
            for path in {
                "module/local.py",
                "module/everywhere.py",
                "module/deeper/everywhere.py",
                "module/direct/a.cache",
                "module/direct/deeper/a.cache",
                "module/a.tmp",
                "module/deeper/a.tmp",
                "module/generated/a.snap",
                "module/generated/deeper/a.snap",
                "module/deeper/generated/a.snap",
            }:
                self.assertNotIn(path, included)

    def test_invalid_nested_config_stops_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            nested = root / "nested"
            nested.mkdir(parents=True)
            (nested / "reql.conf").write_text("scan:\n  exclude:\n    - '**/*.py'\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "Invalid scan.exclude pattern"):
                ProjectScanner(use_default_ignores=False).scan(root)

    @staticmethod
    def _write_files(root: Path, relative_paths: list[str]) -> None:
        for relative in relative_paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative + "\n", encoding="utf-8")


class ConfigExclusionValidationTests(unittest.TestCase):
    def test_config_merge_deduplicates_equivalent_rules_but_keeps_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "reql.conf"
            config_path.write_text("scan:\n  exclude:\n    - .git\n    - ./.git/\n", encoding="utf-8")

            config = load_effective_config(config_path, env={})
            self.assertIn(".git/", config.scan.exclude)
            self.assertNotIn(".git", config.scan.exclude)
            self.assertIn("./.git/", config.scan.exclude)

    def test_root_config_rejects_unsupported_exclusion_format(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "reql.conf"
            config_path.write_text("scan:\n  exclude:\n    - file?.py\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "Invalid scan.exclude pattern"):
                load_effective_config(config_path, env={})

    def test_project_exclude_preserves_anchor_semantics_when_deduplicating(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = _append_config_exclude_patterns(td, ["dir/", "./dir", "*.tmp"])
            self.assertEqual(result["added"], ["dir/", "./dir", "*.tmp"])

            repeated = _append_config_exclude_patterns(td, ["dir", "./dir/"])
            self.assertEqual(repeated["added"], [])
            self.assertEqual(repeated["skipped"], ["dir", "./dir/"])

            with self.assertRaisesRegex(ValueError, "Invalid scan.exclude pattern"):
                _append_config_exclude_patterns(td, ["dir/**"])


if __name__ == "__main__":
    unittest.main()
