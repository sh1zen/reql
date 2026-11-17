"""Fallback grammar loading must never initiate implicit network work."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from memory.artifacts.models import SourceArtifact
from memory.code_analysis import TreeSitterCodeParser
from memory.code_analysis.catalog import _load_language_pack_language
from memory.code_analysis.base import _parser_for
from reql import MemoryGraph


class ParserAvailabilityTests(unittest.TestCase):
    """Fake only the external grammar inventory and loader, keeping parser selection real."""

    def test_missing_local_fallback_is_reported_without_calling_download_capable_loader(self) -> None:
        """The normal parser path preserves module metadata and explains missing setup."""
        provider = SimpleNamespace(available_languages=Mock(return_value=[]), get_language=Mock())
        artifact = SourceArtifact(id="artifact:docker", project_id="project:local", uri="file:///Dockerfile",
            path="Dockerfile", relative_path="Dockerfile", artifact_type="code", language="Dockerfile",
            size_bytes=13, sha256="", mtime=0.0)
        with patch("memory.code_analysis.catalog.import_module", return_value=provider):
            result = TreeSitterCodeParser().parse_artifact(artifact, "FROM scratch\n")
        provider.get_language.assert_not_called()
        self.assertEqual(result.module.metadata["parser_status"], "skipped")
        self.assertIn("not available locally", result.module.metadata["parser_warning"])
        self.assertIn("download", result.module.metadata["parser_warning"])
        self.assertFalse(result.symbols)

    def test_cached_alias_loads_locally_and_missing_aliases_are_not_probed(self) -> None:
        """A cached alternate grammar name remains usable without trying missing names."""
        grammar = object()
        provider = SimpleNamespace(available_languages=Mock(return_value=["docker"]),
                                   get_language=Mock(return_value=grammar))
        with patch("memory.code_analysis.catalog.import_module", return_value=provider):
            self.assertIs(_load_language_pack_language("dockerfile"), grammar)
        provider.get_language.assert_called_once_with("docker")

    def test_explicitly_prepared_grammar_recompiles_previously_skipped_source(self) -> None:
        """A successful empty parse must not block recovery after grammar setup."""
        from tree_sitter import Language
        import tree_sitter_python

        grammar = Language(tree_sitter_python.language())
        provider = SimpleNamespace(available_languages=Mock(side_effect=[[], ["python"]]),
                                   get_language=Mock(return_value=grammar))

        def import_provider(name: str):
            """Model an absent direct wheel and a separately prepared local fallback."""
            if name == "tree_sitter_python":
                raise ModuleNotFoundError(name)
            self.assertEqual(name, "tree_sitter_language_pack")
            return provider

        _parser_for.cache_clear()
        self.addCleanup(_parser_for.cache_clear)
        with TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            (project / "invoice.py").write_text("def invoice_total(amount):\n    return amount + 1\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(temporary) / "memory.reql")
            try:
                with patch("memory.code_analysis.catalog.import_module", side_effect=import_provider):
                    first = graph.compile_project(project)
                    self.assertFalse(first.run.errors)
                    self.assertFalse(graph.store.find_nodes_by_types(["Function"]))
                    recovered = graph.compile_project(project)
                    self.assertFalse(recovered.run.errors)
                    self.assertEqual(recovered.run.files_changed, 1)
                    self.assertEqual([n.properties["name"] for n in graph.store.find_nodes_by_types(["Function"])], ["invoice_total"])
                    self.assertEqual(graph.compile_project(project).run.files_changed, 0)
                provider.get_language.assert_called_once_with("python")
            finally:
                graph.close()


if __name__ == "__main__":
    unittest.main()
