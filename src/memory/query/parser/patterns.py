"""MATCH pattern parser for the compact Cypher-like REQL subset."""
from __future__ import annotations

import re

from ..ast import PatternEdge, PatternNode
from ..errors import REQLSyntaxError
from ..lexer import Token


def join_pattern_tokens(tokens: list[Token]) -> str:
    return "".join(tok.value for tok in tokens if tok.kind != "EOF")


def parse_match_pattern(pattern: str) -> tuple[PatternNode, PatternEdge, PatternNode]:
    regex = re.compile(
        r"^\((?P<a>[A-Za-z_]\w*)?(?::(?P<atype>[A-Za-z_][\w-]*))?\)"
        r"(?P<link1><-|--|-)"
        r"\[(?P<r>[A-Za-z_]\w*)?(?::(?P<rtype>[A-Za-z_][\w|.-]*))?\]"
        r"(?P<link2>->|--|-)?"
        r"\((?P<b>[A-Za-z_]\w*)?(?::(?P<btype>[A-Za-z_][\w-]*))?\)$"
    )
    m = regex.match(pattern)
    if not m:
        raise REQLSyntaxError(f"Invalid MATCH pattern: {pattern!r}")
    left_alias = m.group("a") or "a"
    edge_alias = m.group("r") or "r"
    right_alias = m.group("b") or "b"
    link1 = m.group("link1")
    link2 = m.group("link2") or "-"
    if link1 == "<-":
        direction = "in"
    elif link2 == "->":
        direction = "out"
    else:
        direction = "both"
    raw_types = m.group("rtype")
    edge_types = tuple(t for t in raw_types.split("|") if t) if raw_types else ()
    return (
        PatternNode(alias=left_alias, type_=m.group("atype")),
        PatternEdge(alias=edge_alias, types=edge_types, direction=direction),
        PatternNode(alias=right_alias, type_=m.group("btype")),
    )
