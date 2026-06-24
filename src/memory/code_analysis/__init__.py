"""Code parsing and graph extraction."""
from __future__ import annotations

from typing import Any

from .graph_utils import calls_by_caller, imported_modules
from .models import CodeCall, CodeImport, CodeModule, CodeParseResult, CodeSymbol, CodeText

__all__ = [
    "CodeCall",
    "CodeImport",
    "CodeModule",
    "CodeParseResult",
    "CodeParser",
    "CodeParserRegistry",
    "CodeSymbol",
    "CodeText",
    "TreeSitterCodeParser",
    "calls_by_caller",
    "default_code_parser_registry",
    "imported_modules",
]


def __getattr__(name: str) -> Any:
    if name in {"CodeParser", "CodeParserRegistry", "default_code_parser_registry"}:
        from . import parser_base

        return getattr(parser_base, name)
    if name == "TreeSitterCodeParser":
        from .base import TreeSitterCodeParser

        return TreeSitterCodeParser
    raise AttributeError(name)
