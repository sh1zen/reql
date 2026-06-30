"""Small dependency-free lexer for REQL."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import REQLSyntaxError


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    position: int


KEYWORDS = {
    "ACTIVATE",
    "AND",
    "ARCHIVED",
    "ARTIFACTS",
    "ASC",
    "BETWEEN",
    "BY",
    "CACHE",
    "CONTEXT",
    "CONTAINS",
    "COMMUNITIES",
    "DELTAS",
    "DEPTH",
    "DESC",
    "EDGE",
    "EDGES",
    "ENDS",
    "EXPLAIN",
    "EXISTS",
    "FALSE",
    "FILTER",
    "FIND",
    "FINDING",
    "FINDINGS",
    "FRAGMENTS",
    "FROM",
    "GROUP",
    "HUB",
    "HUBS",
    "ID",
    "IN",
    "INCLUDE",
    "ILIKE",
    "IS",
    "KEY",
    "LIMIT",
    "LIKE",
    "MATCHES",
    "MATCH",
    "MAX",
    "MIN",
    "NO",
    "NODE",
    "NODES",
    "NOT",
    "NULL",
    "OR",
    "ORDER",
    "PATH",
    "PROJECTS",
    "RETURN",
    "REGEX",
    "RETRIEVE",
    "SEARCH",
    "SOURCE",
    "SOURCES",
    "STARTS",
    "STATS",
    "STATUS",
    "SYMBOLS",
    "TEXT",
    "TO",
    "TOP",
    "TRUE",
    "TYPE",
    "TYPES",
    "VIA",
    "VERIFY",
    "WHERE",
    "WITH",
}

PUNCTUATION = {"(", ")", "[", "]", ",", ":", ".", "*", ";", "-", "|"}


def tokenize_reql(source: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    length = len(source)
    while i < length:
        ch = source[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "#":
            while i < length and source[i] != "\n":
                i += 1
            continue
        if source.startswith("//", i):
            while i < length and source[i] != "\n":
                i += 1
            continue
        if source.startswith("->", i) or source.startswith("<-", i) or source.startswith("--", i):
            tokens.append(Token("SYMBOL", source[i : i + 2], i))
            i += 2
            continue
        if source.startswith(">=", i) or source.startswith("<=", i) or source.startswith("!=", i) or source.startswith("~=", i):
            tokens.append(Token("OP", source[i : i + 2], i))
            i += 2
            continue
        if ch in "=><":
            tokens.append(Token("OP", ch, i))
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            start = i
            i += 1
            chars: list[str] = []
            while i < length:
                c = source[i]
                if c == "\\":
                    i += 1
                    if i >= length:
                        raise REQLSyntaxError(f"Unterminated escape sequence at position {start}")
                    esc = source[i]
                    mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
                    chars.append(mapping.get(esc, esc))
                    i += 1
                    continue
                if c == quote:
                    i += 1
                    tokens.append(Token("STRING", "".join(chars), start))
                    break
                chars.append(c)
                i += 1
            else:
                raise REQLSyntaxError(f"Unterminated string at position {start}")
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < length and source[i + 1].isdigit()):
            start = i
            i += 1
            has_dot = False
            while i < length and (source[i].isdigit() or (source[i] == "." and not has_dot)):
                if source[i] == ".":
                    has_dot = True
                i += 1
            tokens.append(Token("NUMBER", source[start:i], start))
            continue
        if ch in PUNCTUATION:
            tokens.append(Token("SYMBOL", ch, i))
            i += 1
            continue
        if _is_ident_start(ch):
            start = i
            i += 1
            while i < length and _is_ident_part(source[i]):
                i += 1
            value = source[start:i]
            upper = value.upper()
            if upper in KEYWORDS:
                tokens.append(Token("KEYWORD", upper, start))
            else:
                tokens.append(Token("IDENT", value, start))
            continue
        raise REQLSyntaxError(f"Unexpected character {ch!r} at position {i}")
    tokens.append(Token("EOF", "", length))
    return tokens


def _is_ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def _is_ident_part(ch: str) -> bool:
    return ch.isalnum() or ch in {"_", "-", "$"}
