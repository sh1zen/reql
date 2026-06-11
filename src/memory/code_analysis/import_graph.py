"""Small import graph helper."""
from __future__ import annotations

from .models import CodeImport


def imported_modules(imports: list[CodeImport]) -> set[str]:
    modules: set[str] = set()
    for item in imports:
        if item.module:
            modules.add(item.module)
        elif item.name:
            modules.add(item.name)
    return modules
