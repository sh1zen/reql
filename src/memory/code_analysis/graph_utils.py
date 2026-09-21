"""Small code graph helpers."""
from __future__ import annotations

from .models import CodeCall, CodeImport


def imported_modules(imports: list[CodeImport]) -> set[str]:
    return {module for item in imports if (module := item.module or item.name)}


def calls_by_caller(calls: list[CodeCall]) -> dict[str | None, list[CodeCall]]:
    grouped: dict[str | None, list[CodeCall]] = {}
    for call in calls:
        grouped.setdefault(call.caller, []).append(call)
    return grouped
