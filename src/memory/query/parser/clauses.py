"""Shared clause parsers for REQL commands."""
from __future__ import annotations

from typing import Iterable

from ..ast import Condition, NodeSelector, PatternEdge, PatternNode, SortSpec
from ..errors import REQLSyntaxError
from ..lexer import Token
from .expressions import ExpressionParser
from .patterns import join_pattern_tokens, parse_match_pattern

CLAUSE_KEYWORDS = {
    "WHERE",
    "RETURN",
    "ORDER",
    "LIMIT",
    "TOP",
    "DEPTH",
    "TYPES",
    "TYPE",
    "CONTEXT",
    "INCLUDE",
    "VIA",
    "MIN",
    "BY",
}

RESERVED_EXPRESSION_ENDS = {"RETURN", "ORDER", "LIMIT", "TOP", "INCLUDE"}
SYMBOL_NODE_TYPES = ("Module", "CodeSymbol", "Function", "Class", "Method", "Import", "Comment", "Docstring")


class ClauseParserMixin:
    """Parses clauses and small grammar fragments shared by commands."""

    def _parse_return_fields(self) -> tuple[str, ...]:
        if self.match_symbol("*"):
            return ("*",)
        return tuple(self._parse_field_list())

    def _parse_field_list(self) -> list[str]:
        fields: list[str] = []
        while True:
            fields.append(self._parse_field_name())
            if not self.match_symbol(","):
                break
        return fields

    def _parse_ident_list(self) -> tuple[str, ...]:
        values: list[str] = []
        while True:
            values.append(self.expect_identifier())
            if not self.match_symbol(","):
                break
        return tuple(v for v in values if v)

    def _parse_value_list_as_strings(self) -> list[str]:
        values: list[str] = []
        while True:
            value = self.expect_value()
            values.append(str(value))
            if not self.match_symbol(","):
                break
        if not values:
            raise REQLSyntaxError("Expected at least one node id")
        return values

    def _parse_order_by(self) -> SortSpec:
        field = self._parse_field_name()
        descending = True
        if self.match_keyword("ASC"):
            descending = False
        elif self.match_keyword("DESC"):
            descending = True
        return SortSpec(field=field, descending=descending)

    def _parse_positive_int(self, name: str) -> int:
        value = self.expect_value()
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise REQLSyntaxError(f"{name} must be an integer") from exc
        if parsed < 0:
            raise REQLSyntaxError(f"{name} must be >= 0")
        return parsed

    def _parse_selector(self) -> NodeSelector:
        if self.match_keyword("ID") or self.match_keyword("NODE"):
            value = self.expect_value()
            return NodeSelector("id", str(value))
        if self.match_keyword("TEXT"):
            value = self.expect_value()
            return NodeSelector("text", str(value))
        if self.match_keyword("KEY"):
            type_ = self.expect_identifier()
            value = self.expect_value()
            return NodeSelector("key", str(value), type_)
        tok = self.current
        value = self.expect_value()
        if tok.kind == "STRING":
            return NodeSelector("text", str(value))
        return NodeSelector("id", str(value))

    def _parse_condition_until(self, reserved: Iterable[str]) -> Condition:
        expr_tokens = self._collect_until(set(reserved))
        if not expr_tokens:
            raise REQLSyntaxError("WHERE requires an expression")
        parser = ExpressionParser(expr_tokens)
        return parser.parse()

    def _collect_until(self, reserved_keywords: set[str]) -> list[Token]:
        collected: list[Token] = []
        paren = 0
        bracket = 0
        while self.current.kind != "EOF":
            tok = self.current
            if tok.kind == "SYMBOL":
                if tok.value == "(":
                    paren += 1
                elif tok.value == ")":
                    paren -= 1
                elif tok.value == "[":
                    bracket += 1
                elif tok.value == "]":
                    bracket -= 1
            if paren == 0 and bracket == 0 and tok.kind == "KEYWORD" and tok.value in reserved_keywords:
                break
            collected.append(self.advance())
        return collected

    def _parse_field_name(self) -> str:
        parts = [self.expect_identifier()]
        while self.match_symbol("."):
            parts.append(self.expect_identifier())
        return ".".join(part.lower() for part in parts)

    def _parse_pattern(self, pattern: str) -> tuple[PatternNode, PatternEdge, PatternNode]:
        return parse_match_pattern(pattern)

    def _join_pattern_tokens(self, tokens: list[Token]) -> str:
        return join_pattern_tokens(tokens)
