"""Shared parser layer and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar, Protocol

from ..artifacts.models import SourceArtifact
from .metadata import basic_file_metadata
from .models import DocumentParseResult


class DocumentParser(Protocol):
    parser_name: str
    parser_version: str

    def supports(self, artifact: SourceArtifact) -> bool: ...
    def parse(self, artifact: SourceArtifact, content: bytes) -> DocumentParseResult: ...


class BaseDocumentParser(ABC):
    """Common base for concrete document format parsers."""

    parser_name: str
    parser_version: str
    artifact_types: ClassVar[frozenset[str]] = frozenset()
    languages: ClassVar[frozenset[str]] = frozenset()
    encoding: str = "utf-8"

    def supports(self, artifact: SourceArtifact) -> bool:
        artifact_type = artifact.artifact_type.casefold()
        language = (artifact.language or "").casefold()
        return artifact_type in self.artifact_types or language in self.languages

    def decode_text(self, content: bytes) -> str:
        return content.decode(self.encoding, errors="replace")

    def base_metadata(self, artifact: SourceArtifact) -> dict[str, object]:
        return basic_file_metadata(artifact.path)

    def title_from_path(self, artifact: SourceArtifact) -> str:
        return Path(artifact.path).stem

    def artifact_name(self, artifact: SourceArtifact) -> str:
        return Path(artifact.path).name

    @abstractmethod
    def parse(self, artifact: SourceArtifact, content: bytes) -> DocumentParseResult:
        raise NotImplementedError


class ParserRegistry:
    def __init__(self, parsers: list[DocumentParser]) -> None:
        self.parsers = parsers

    def parser_for(self, artifact: SourceArtifact) -> DocumentParser:
        for parser in self.parsers:
            if parser.supports(artifact):
                return parser
        from .formats.text import PlainTextParser

        return PlainTextParser()


def default_parser_registry(*, enable_pdf: bool = True) -> ParserRegistry:
    from .formats.markdown import MarkdownParser
    from .formats.pdf import PDFParser
    from .formats.text import PlainTextParser

    parsers: list[DocumentParser] = [MarkdownParser()]
    if enable_pdf:
        parsers.append(PDFParser())
    parsers.append(PlainTextParser())
    return ParserRegistry(parsers)
