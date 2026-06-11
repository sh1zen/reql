"""Boolean expression parser for REQL WHERE clauses."""
from __future__ import annotations

from typing import Any

from ..ast import BooleanCondition, Comparison, Condition, NotCondition
from ..errors import REQLSyntaxError
from ..lexer import Token


class ExpressionParser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = [*tokens, Token("EOF", "", tokens[-1].position + 1 if tokens else 0)]
        self.pos = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.current
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def parse(self) -> Condition:
        expr = self._parse_or()
        if self.current.kind != "EOF":
            raise REQLSyntaxError(f"Unexpected token in WHERE expression: {self.current.value!r}")
        return expr

    def _parse_or(self) -> Condition:
        left = self._parse_and()
        while self._match_keyword("OR"):
            left = BooleanCondition("OR", left, self._parse_and())
        return left

    def _parse_and(self) -> Condition:
        left = self._parse_not()
        while self._match_keyword("AND"):
            left = BooleanCondition("AND", left, self._parse_not())
        return left

    def _parse_not(self) -> Condition:
        if self._match_keyword("NOT"):
            return NotCondition(self._parse_not())
        return self._parse_atom()

    def _parse_atom(self) -> Condition:
        if self._match_symbol("("):
            expr = self._parse_or()
            self._expect_symbol(")")
            return expr
        field = self._parse_field_name()
        if self._match_keyword("NOT"):
            return NotCondition(self._parse_field_operator(field))
        return self._parse_field_operator(field)

    def _parse_field_operator(self, field: str) -> Condition:
        if self._match_keyword("IN"):
            value = self._parse_value()
            if not isinstance(value, list):
                raise REQLSyntaxError("IN expects a list value, for example status IN ['active','reinforced']")
            return Comparison(field, "IN", value)
        if self._match_keyword("BETWEEN"):
            lower = self._parse_value()
            if not self._match_keyword("AND"):
                raise REQLSyntaxError("BETWEEN expects AND, for example score BETWEEN 0.2 AND 0.8")
            upper = self._parse_value()
            return Comparison(field, "BETWEEN", [lower, upper])
        if self._match_keyword("IS"):
            negate = self._match_keyword("NOT")
            value = self._parse_value()
            return Comparison(field, "IS_NOT" if negate else "IS", value)
        if self._match_keyword("CONTAINS"):
            return Comparison(field, "CONTAINS", self._parse_value())
        if self._match_keyword("LIKE"):
            return Comparison(field, "LIKE", self._parse_value())
        if self._match_keyword("ILIKE"):
            return Comparison(field, "ILIKE", self._parse_value())
        if self._match_keyword("MATCHES") or self._match_keyword("REGEX"):
            return Comparison(field, "REGEX", self._parse_value())
        if self._match_keyword("STARTS"):
            self._match_keyword("WITH")
            return Comparison(field, "STARTS_WITH", self._parse_value())
        if self._match_keyword("ENDS"):
            self._match_keyword("WITH")
            return Comparison(field, "ENDS_WITH", self._parse_value())
        if self._match_keyword("EXISTS"):
            return Comparison(field, "EXISTS", True)
        if self.current.kind == "OP":
            op = self.advance().value
            return Comparison(field, op, self._parse_value())
        return Comparison(field, "EXISTS", True)

    def _parse_field_name(self) -> str:
        parts = [self._expect_identifier()]
        while self._match_symbol("."):
            parts.append(self._expect_identifier())
        return ".".join(part.lower() for part in parts)

    def _parse_value(self) -> Any:
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
            return self._parse_bare_value()
        if self._match_symbol("["):
            values: list[Any] = []
            if not self._match_symbol("]"):
                while True:
                    values.append(self._parse_value())
                    if self._match_symbol("]"):
                        break
                    self._expect_symbol(",")
            return values
        raise REQLSyntaxError(f"Expected value in WHERE expression at {tok.position}")

    def _parse_bare_value(self) -> str:
        parts = [self.advance().value]
        while self._match_symbol(":"):
            if self.current.kind not in {"IDENT", "KEYWORD", "NUMBER"}:
                raise REQLSyntaxError(f"Expected value after ':' in WHERE expression; got {self.current.value!r}")
            parts.extend((":", self.advance().value))
        return "".join(str(part) for part in parts)

    def _match_keyword(self, value: str) -> bool:
        if self.current.kind == "KEYWORD" and self.current.value == value:
            self.advance()
            return True
        return False

    def _match_symbol(self, value: str) -> bool:
        if self.current.kind == "SYMBOL" and self.current.value == value:
            self.advance()
            return True
        return False

    def _expect_symbol(self, value: str) -> None:
        if not self._match_symbol(value):
            raise REQLSyntaxError(f"Expected {value!r} in WHERE expression; got {self.current.value!r}")

    def _expect_identifier(self) -> str:
        if self.current.kind in {"IDENT", "KEYWORD"}:
            value = self.current.value
            self.advance()
            return value
        raise REQLSyntaxError(f"Expected field name in WHERE expression; got {self.current.value!r}")
