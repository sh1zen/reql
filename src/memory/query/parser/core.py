"""Core parser cursor and public entrypoints."""
from __future__ import annotations

from typing import Any

from ..ast import Statement
from ..errors import REQLSyntaxError
from ..lexer import Token, tokenize_reql
from .commands import CommandParserMixin


class REQLParser(CommandParserMixin):
    """Recursive-descent parser for the compact REQL grammar."""

    def __init__(self, source: str) -> None:
        self.source = source.strip()
        if self.source.endswith(";"):
            self.source = self.source[:-1].strip()
        self.tokens = tokenize_reql(self.source)
        self.pos = 0

    def parse(self) -> Statement:
        if self.match_keyword("FIND"):
            stmt = self._parse_find()
        elif self.match_keyword("SEARCH"):
            stmt = self._parse_search()
        elif self.match_keyword("RETRIEVE"):
            stmt = self._parse_retrieve()
        elif self.match_keyword("ACTIVATE"):
            stmt = self._parse_activate()
        elif self.match_keyword("MATCH"):
            stmt = self._parse_match()
        elif self.match_keyword("PATH"):
            stmt = self._parse_path()
        elif self.match_keyword("EXPLAIN"):
            stmt = self._parse_explain()
        elif self.match_keyword("STATS"):
            stmt = self._parse_stats()
        elif self.match_keyword("PROJECTS"):
            stmt = self._parse_typed_node_list("PROJECTS", ("Project",), allow_type=False)
        elif self.match_keyword("ARTIFACTS"):
            stmt = self._parse_typed_node_list("ARTIFACTS", ("SourceArtifact",), allow_type=False)
        elif self.match_keyword("FRAGMENTS"):
            stmt = self._parse_typed_node_list("FRAGMENTS", ("SourceFragment",), allow_type=False)
        elif self.match_keyword("SYMBOLS"):
            stmt = self._parse_typed_node_list("SYMBOLS", _SYMBOL_NODE_TYPES, allow_type=True)
        elif self.match_keyword("FINDINGS"):
            stmt = self._parse_typed_node_list("FINDINGS", ("StaticAnalysisFinding",), allow_type=False)
        elif self.match_keyword("COMMUNITIES"):
            stmt = self._parse_communities()
        elif self.match_keyword("HUBS"):
            stmt = self._parse_hubs()
        elif self.match_keyword("DELTAS"):
            stmt = self._parse_typed_node_list("DELTAS", ("GraphDelta",), allow_type=False, limit=10)
        elif self.match_keyword("CACHE"):
            stmt = self._parse_cache()
        elif self.match_keyword("VERIFY"):
            stmt = self._parse_verify()
        else:
            tok = self.current
            raise REQLSyntaxError(f"Expected command at position {tok.position}; got {tok.value!r}")
        self.match_symbol(";")
        self.expect_eof()
        return stmt

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.current
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def match_keyword(self, value: str) -> bool:
        if self.current.kind == "KEYWORD" and self.current.value == value:
            self.advance()
            return True
        return False

    def expect_keyword(self, value: str) -> Token:
        if not self.match_keyword(value):
            raise REQLSyntaxError(f"Expected {value} at position {self.current.position}; got {self.current.value!r}")
        return self.tokens[self.pos - 1]

    def match_symbol(self, value: str) -> bool:
        if self.current.kind == "SYMBOL" and self.current.value == value:
            self.advance()
            return True
        return False

    def expect_symbol(self, value: str) -> Token:
        if not self.match_symbol(value):
            raise REQLSyntaxError(f"Expected symbol {value!r} at position {self.current.position}; got {self.current.value!r}")
        return self.tokens[self.pos - 1]

    def expect_identifier(self) -> str:
        if self.current.kind in {"IDENT", "KEYWORD"}:
            value = self.current.value
            self.advance()
            return value
        raise REQLSyntaxError(f"Expected identifier at position {self.current.position}; got {self.current.value!r}")

    def expect_value(self) -> Any:
        tok = self.current
        if tok.kind == "STRING":
            self.advance()
            return tok.value
        if tok.kind == "NUMBER":
            self.advance()
            return float(tok.value) if "." in tok.value else int(tok.value)
        if tok.kind == "KEYWORD" and tok.value in {"TRUE", "FALSE", "NULL"}:
            self.advance()
            return {"TRUE": True, "FALSE": False, "NULL": None}[tok.value]
        if tok.kind in {"IDENT", "KEYWORD"}:
            self.advance()
            return tok.value
        if self.match_symbol("["):
            values: list[Any] = []
            if not self.match_symbol("]"):
                while True:
                    values.append(self.expect_value())
                    if self.match_symbol("]"):
                        break
                    self.expect_symbol(",")
            return values
        raise REQLSyntaxError(f"Expected scalar value at position {tok.position}; got {tok.value!r}")

    def expect_eof(self) -> None:
        if self.current.kind != "EOF":
            raise REQLSyntaxError(f"Unexpected token {self.current.value!r} at position {self.current.position}")


def parse_reql(source: str) -> Statement:
    return REQLParser(source).parse()


_SYMBOL_NODE_TYPES = ("Module", "CodeSymbol", "Function", "Class", "Method", "Import", "Comment", "Docstring")
