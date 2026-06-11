"""Helpers for resolving symbols inside one parse result."""
from __future__ import annotations

from .models import CodeParseResult, CodeSymbol


class SymbolTable:
    def __init__(self, result: CodeParseResult) -> None:
        self.by_qualified_name = {symbol.qualified_name: symbol for symbol in result.symbols}
        self.by_name: dict[str, list[CodeSymbol]] = {}
        for symbol in result.symbols:
            self.by_name.setdefault(symbol.name, []).append(symbol)

    def resolve_call_target(self, target: str, *, caller: str | None = None) -> CodeSymbol | None:
        if target in self.by_qualified_name:
            return self.by_qualified_name[target]
        tail = target.split(".")[-1]
        candidates = self.by_name.get(tail, [])
        if not candidates:
            return None
        if caller:
            caller_parts = caller.split(".")[:-1]
            for candidate in candidates:
                if candidate.qualified_name.split(".")[:-1] == caller_parts:
                    return candidate
        return candidates[0]
