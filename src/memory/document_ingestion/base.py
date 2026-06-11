"""Parser protocol and registry."""
from __future__ import annotations

from typing import Protocol

from ..artifacts.models import SourceArtifact
from .models import DocumentParseResult


class DocumentParser(Protocol):
    parser_name: str
    parser_version: str

    def supports(self, artifact: SourceArtifact) -> bool: ...
    def parse(self, artifact: SourceArtifact, content: bytes) -> DocumentParseResult: ...


class ParserRegistry:
    def __init__(self, parsers: list[DocumentParser]) -> None:
        self.parsers = parsers

    def parser_for(self, artifact: SourceArtifact) -> DocumentParser:
        for parser in self.parsers:
            if parser.supports(artifact):
                return parser
        from .text import PlainTextParser

        return PlainTextParser()


def default_parser_registry(*, enable_pdf: bool = True) -> ParserRegistry:
    from .markdown import MarkdownParser
    from .pdf import PDFParser
    from .text import PlainTextParser

    parsers: list[DocumentParser] = [MarkdownParser()]
    if enable_pdf:
        parsers.append(PDFParser())
    parsers.append(PlainTextParser())
    return ParserRegistry(parsers)
