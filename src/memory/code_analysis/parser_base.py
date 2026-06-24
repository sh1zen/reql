"""Code parser protocol and registry."""
from __future__ import annotations

from typing import Protocol

from ..artifacts.models import SourceArtifact
from .catalog import detect_code_language
from .models import CodeParseResult


class CodeParser(Protocol):
    parser_name: str
    parser_version: str

    def supports(self, language: str) -> bool: ...
    def parse_artifact(self, artifact: SourceArtifact, text: str) -> CodeParseResult: ...


class CodeParserRegistry:
    def __init__(self, parsers: list[CodeParser]) -> None:
        self.parsers = parsers

    def parser_for(self, artifact: SourceArtifact) -> CodeParser | None:
        language = detect_code_language(artifact)
        if not language:
            return None
        for parser in self.parsers:
            if parser.supports(language):
                return parser
        return None


def default_code_parser_registry() -> CodeParserRegistry:
    from .base import TreeSitterCodeParser

    return CodeParserRegistry([TreeSitterCodeParser()])
