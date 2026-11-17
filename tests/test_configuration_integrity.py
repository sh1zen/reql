"""Configuration parsing and override isolation at the public loader boundary."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from memory.config import ConfigError, default_config, load_config, load_effective_config, merge_config


class ConfigurationIntegrityTests(unittest.TestCase):
    """Keep supported YAML values and caller-owned overrides intact."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "reql.conf"

    def test_single_quoted_values_preserve_hashes_apostrophes_and_double_quotes(self) -> None:
        """Comment stripping follows YAML quoting rather than truncating the scalar."""
        for scalar, expected in (
            ("'invoice # archive' # comment", "invoice # archive"),
            ("'it''s # archived' # comment", "it's # archived"),
            ("'say \"invoice # archive\"' # comment", 'say "invoice # archive"'),
            ("don't-truncate#this # comment", "don't-truncate#this"),
            ('"invoice \\\"#\\\" archive" # comment', 'invoice "#" archive'),
        ):
            with self.subTest(scalar=scalar):
                self.path.write_text(f"project:\n  id: {scalar}\n", encoding="utf-8")
                self.assertEqual(load_config(self.path).project.id, expected)

    def test_unterminated_or_invalid_quoted_values_are_configuration_errors(self) -> None:
        """Malformed quoting must not become an accepted plain string."""
        for scalar in ("'unterminated", '"unterminated', "'bad'quote'", "'ok' trailing", "'"):
            with self.subTest(scalar=scalar):
                self.path.write_text(f"project:\n  id: {scalar}\n", encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "line 2"):
                    load_config(self.path)

    def test_duplicate_yaml_keys_are_rejected_with_location(self) -> None:
        """A duplicate section or option cannot silently replace earlier settings."""
        for text in ("project:\n  id: first\n  id: second\n",
                     "project:\n  id: first\nproject:\n  id: second\n"):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "[Dd]uplicate.*line 3|line 3.*[Dd]uplicate"):
                    load_config(self.path)

    def test_nested_and_dotted_overrides_do_not_mutate_caller_values(self) -> None:
        """Path updates use private mappings even when callers reuse their inputs."""
        overrides = {"compile": {"documents": {"markdown": False}}, "compile.documents.pdf": True}
        before = deepcopy(overrides)
        config = merge_config(default_config(), overrides)
        self.assertTrue(config.compile.documents["pdf"])
        self.assertFalse(config.compile.documents["markdown"])
        self.assertEqual(overrides, before)

    def test_nonfinite_file_size_limits_fail_at_configuration_boundary(self) -> None:
        """Reject unusable limits before filesystem scanning converts them to bytes."""
        for value in (float("nan"), float("inf"), float("-inf"), 10 ** 400):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "scan.max_file_size_mb"):
                merge_config(default_config(), {"scan.max_file_size_mb": value})
        with self.assertRaisesRegex(ConfigError, "scan.max_file_size_mb"):
            load_effective_config(start_dir=self.root, env={"REQL_CONFIG_OVERRIDES": '{"scan.max_file_size_mb": NaN}'})

if __name__ == "__main__":
    unittest.main()
