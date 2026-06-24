from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory.artifacts.models import SourceArtifact
from memory.document_ingestion import BaseDocumentParser, default_parser_registry
from memory.document_ingestion.formats import MarkdownParser, PDFParser, PlainTextParser


class DocumentIngestionLayerTests(unittest.TestCase):
    def test_format_parsers_inherit_common_base(self) -> None:
        parsers = [MarkdownParser(), PDFParser(), PlainTextParser()]

        self.assertTrue(all(isinstance(parser, BaseDocumentParser) for parser in parsers))

    def test_default_registry_uses_format_package_parsers(self) -> None:
        registry = default_parser_registry(enable_pdf=True)

        self.assertEqual([type(parser) for parser in registry.parsers], [MarkdownParser, PDFParser, PlainTextParser])

    def test_common_support_logic_matches_artifact_type_and_language(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "README.txt"
            path.write_text("# Title\n", encoding="utf-8")
            markdown_by_language = SourceArtifact(
                id="artifact:readme",
                project_id="project:test",
                uri=path.as_uri(),
                path=str(path),
                relative_path="README.txt",
                artifact_type="text",
                language="Markdown",
                size_bytes=path.stat().st_size,
                sha256="abc",
                mtime=path.stat().st_mtime,
            )
            text_artifact = SourceArtifact(
                id="artifact:notes",
                project_id="project:test",
                uri=path.as_uri(),
                path=str(path),
                relative_path="README.txt",
                artifact_type="text",
                language=None,
                size_bytes=path.stat().st_size,
                sha256="abc",
                mtime=path.stat().st_mtime,
            )

            self.assertTrue(MarkdownParser().supports(markdown_by_language))
            self.assertTrue(PlainTextParser().supports(text_artifact))


if __name__ == "__main__":
    unittest.main()
