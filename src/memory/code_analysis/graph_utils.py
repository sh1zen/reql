"""Small code graph helpers."""
from __future__ import annotations

from .models import CodeCall, CodeImport


def imported_modules(imports: list[CodeImport]) -> set[str]:
    modules: set[str] = set()
    for item in imports:
        if item.module:
            modules.add(item.module)
        elif item.name:
            modules.add(item.name)
    return modules


def calls_by_caller(calls: list[CodeCall]) -> dict[str | None, list[CodeCall]]:
    grouped: dict[str | None, list[CodeCall]] = {}
    for call in calls:
        grouped.setdefault(call.caller, []).append(call)
    return grouped
