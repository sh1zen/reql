"""Helpers for resolving symbols inside one parse result."""
from __future__ import annotations

from .models import CodeParseResult, CodeSymbol


def is_computed_reference(target: str) -> bool:
    """Identify receiver expressions whose result type is absent from a name table."""
    return any(character in target for character in "()[]")


class SymbolTable:
    def __init__(self, result: CodeParseResult) -> None:
        self.by_qualified_name = {symbol.qualified_name: symbol for symbol in result.symbols}
        self.by_name: dict[str, list[CodeSymbol]] = {}
        for symbol in result.symbols:
            self.by_name.setdefault(symbol.name, []).append(symbol)

    def resolve_call_target(self, target: str, *, caller: str | None = None) -> CodeSymbol | None:
        """Resolve local names without inferring the result type of receiver expressions."""
        if target in self.by_qualified_name:
            return self.by_qualified_name[target]
        # A name-only table cannot infer factory results, subscriptions or super
        # dispatch. Matching just the final name fabricates calls to local methods.
        if is_computed_reference(target):
            return None
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
