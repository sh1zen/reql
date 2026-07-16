from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory.config import ConfigError, canonical_config_path, find_config_path, load_config, load_effective_config


class ProjectConfigTests(unittest.TestCase):
    def test_internal_config_contains_protected_defaults(self) -> None:
        config = load_config(start_dir=Path(tempfile.gettempdir()))

        self.assertEqual(canonical_config_path().name, "conf.yaml")
        self.assertEqual(config.scan.max_file_size_mb, 10)
        self.assertFalse(config.scan.use_gitignore)
        self.assertFalse(config.scan.ignore_defaults)
        self.assertIn(".git/", config.scan.exclude)
        self.assertIn("node_modules/", config.scan.exclude)
        self.assertTrue(config.compile.documents)
        self.assertTrue(all(enabled is False for enabled in config.compile.documents.values()))
        self.assertEqual(config.compile.document_formats["markdown"]["extensions"], [".md", ".markdown"])

    def test_nearest_reql_conf_overrides_scalars_and_joins_lists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "services" / "search"
            child.mkdir(parents=True)
            local = child / "reql.conf"
            local.write_text(
                "project:\n  id: search\nscan:\n  max_file_size_mb: 3\n  use_gitignore: true\n  exclude:\n    - root-only/\n",
                encoding="utf-8",
            )

            config = load_config(start_dir=child)

            self.assertEqual(find_config_path(child), local)
            self.assertEqual(config.project.id, "search")
            self.assertEqual(config.scan.max_file_size_mb, 3)
            self.assertTrue(config.scan.use_gitignore)
            self.assertIn(".git/", config.scan.exclude)
            self.assertIn("root-only/", config.scan.exclude)

    def test_scan_ignore_defaults_replaces_internal_include_and_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "reql.conf"
            local.write_text(
                "scan:\n  ignore_defaults: true\n  include:\n    - src/**\n  exclude:\n    - project-only/\n",
                encoding="utf-8",
            )

            config = load_config(local)

            self.assertTrue(config.scan.ignore_defaults)
            self.assertEqual(config.scan.include, ["src/**"])
            self.assertEqual(config.scan.exclude, ["project-only/"])

    def test_project_can_override_internal_document_format_definition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "reql.conf"
            local.write_text(
                'compile:\n  document_formats:\n    markdown: {"extensions": [".mdx"]}\n',
                encoding="utf-8",
            )

            config = load_config(local)

            self.assertEqual(config.compile.document_formats["markdown"]["extensions"], [".mdx"])

    def test_nearest_reql_conf_wins_over_parent_reql_conf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "child"
            child.mkdir()
            (root / "reql.conf").write_text("project:\n  id: parent\n", encoding="utf-8")
            (child / "reql.conf").write_text("project:\n  id: child\n", encoding="utf-8")

            config = load_config(start_dir=child)

            self.assertEqual(config.project.id, "child")

    def test_explicit_reql_conf_uses_internal_config_as_its_base(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "reql.conf"
            local.write_text("project:\n  id: local\n", encoding="utf-8")

            config = load_config(local)

            self.assertEqual(config.project.id, "local")
            self.assertEqual(config.reporting.output_dir, "reports")
            self.assertIn("__pycache__/", config.scan.exclude)

    def test_environment_and_explicit_overrides_join_after_reql_conf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "reql.conf").write_text(
                "project:\n  id: local\nscan:\n  exclude:\n    - local/\n",
                encoding="utf-8",
            )

            config = load_effective_config(
                start_dir=root,
                env={"REQL_CONFIG_OVERRIDES": '{"project.id": "environment", "scan.exclude": ["environment/"]}'},
                overrides={"project.id": "explicit", "scan.exclude": ["explicit/"]},
            )

            self.assertEqual(config.project.id, "explicit")
            self.assertIn(".git/", config.scan.exclude)
            self.assertIn("local/", config.scan.exclude)
            self.assertIn("environment/", config.scan.exclude)
            self.assertIn("explicit/", config.scan.exclude)

    def test_project_can_toggle_selected_document_formats(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "reql.conf"
            local.write_text(
                "compile:\n  ingest_documents: false\n  documents:\n    markdown: true\n",
                encoding="utf-8",
            )

            config = load_config(local)
            self.assertFalse(config.compile.ingest_documents)
            self.assertTrue(config.compile.documents["markdown"])
            self.assertFalse(config.compile.documents["pdf"])
            self.assertEqual(config.compile.document_formats["markdown"]["extensions"], [".md", ".markdown"])

    def test_project_conf_yaml_is_not_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "conf.yaml").write_text("project:\n  id: must-not-load\n", encoding="utf-8")

            config = load_config(start_dir=root)

            self.assertEqual(config.project.id, "default")

    def test_invalid_reql_conf_reports_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "reql.conf"
            local.write_text("unknown:\n  value: true\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "reql.conf"):
                load_config(start_dir=local.parent)


if __name__ == "__main__":
    unittest.main()
