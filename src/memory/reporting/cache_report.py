"""Markdown report for project compilation cache state."""
from __future__ import annotations

from collections import Counter
from typing import Any


class CacheReportBuilder:
    def render(self, context: dict[str, Any]) -> str:
        entries = context["cache_entries"]
        status = context.get("scan_status") or {}
        lines = [
            "# REQL Cache Report",
            "",
            f"Generated at: {context['generated_at']}",
            "",
            "## Cache summary",
            "",
            f"- Project id: `{context['project_id']}`",
            f"- Cached artifacts: {status.get('cached_artifacts', sum(1 for n in entries if n.status == 'active'))}",
            f"- Dirty artifacts: {status.get('dirty_artifacts', 0)}",
            f"- Deleted artifacts: {status.get('deleted_artifacts', 0)}",
            f"- Skipped artifacts: {len(context.get('skipped_files') or [])}",
            "",
            "## Parser versions",
        ]
        _counter(lines, Counter(str(n.properties.get("parser_version") or "unknown") for n in entries))
        lines.append("")
        lines.append("## Chunking versions")
        _counter(lines, Counter(str(n.properties.get("chunking_version") or "unknown") for n in entries))
        lines.append("")
        lines.append("## Cache entries")
        if not entries:
            lines.append("- No data yet.")
        else:
            for node in sorted(entries, key=lambda n: str(n.properties.get("relative_path") or n.label))[:50]:
                lines.append(
                    f"- `{node.properties.get('relative_path', node.label)}` status={node.status} "
                    f"compiled_at={node.properties.get('compiled_at', '')} parser={node.properties.get('parser_version', '')}"
                )
        return "\n".join(lines).rstrip() + "\n"


def _counter(lines: list[str], counts: Counter[str]) -> None:
    if not counts:
        lines.append("- No data yet.")
        return
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key}: {count}")
