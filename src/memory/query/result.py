"""Result objects and text rendering for REQL."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class QueryResult:
    statement: str
    command: str
    columns: list[str]
    rows: list[dict[str, Any]]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "command": self.command,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "diagnostics": self.diagnostics,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_table(self, *, max_width: int = 96) -> str:
        if not self.columns:
            return f"{len(self.rows)} row(s)"
        if not self.rows:
            return " | ".join(self.columns) + "\n" + " | ".join("-" * len(c) for c in self.columns) + "\n(0 rows)"

        matrix: list[list[str]] = []
        for row in self.rows:
            rendered: list[str] = []
            for col in self.columns:
                value = row.get(col)
                rendered.append(_compact(value, max_width=max_width))
            matrix.append(rendered)
        widths = [len(col) for col in self.columns]
        for rendered in matrix:
            for i, value in enumerate(rendered):
                widths[i] = min(max(widths[i], len(value)), max_width)
        header = " | ".join(col.ljust(widths[i]) for i, col in enumerate(self.columns))
        sep = " | ".join("-" * widths[i] for i in range(len(widths)))
        lines = [header, sep]
        for rendered in matrix:
            lines.append(" | ".join(rendered[i].ljust(widths[i]) for i in range(len(widths))))
        lines.append(f"({len(self.rows)} rows)")
        return "\n".join(lines)


def _compact(value: Any, *, max_width: int) -> str:
    if value is None:
        text = ""
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    text = text.replace("\n", "\\n")
    if len(text) > max_width:
        return text[: max_width - 3] + "..."
    return text
