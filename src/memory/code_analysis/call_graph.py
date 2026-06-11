"""Small call graph helper."""
from __future__ import annotations

from .models import CodeCall


def calls_by_caller(calls: list[CodeCall]) -> dict[str | None, list[CodeCall]]:
    grouped: dict[str | None, list[CodeCall]] = {}
    for call in calls:
        grouped.setdefault(call.caller, []).append(call)
    return grouped
