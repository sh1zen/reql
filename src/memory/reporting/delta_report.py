"""Markdown report for project graph deltas."""
from __future__ import annotations

from typing import Any


class DeltaReportBuilder:
    def render(self, context: dict[str, Any]) -> str:
        lines = [
            "# REQL Delta Report",
            "",
            f"Generated at: {context['generated_at']}",
            "",
            "## Recent graph deltas",
            "",
        ]
        deltas = context["deltas"]
        if not deltas:
            lines.append("- No data yet.")
            return "\n".join(lines).rstrip() + "\n"
        for node in deltas[:50]:
            props = node.properties
            lines.append(f"### {node.id}")
            lines.append("")
            lines.append(f"- Run id: `{props.get('run_id', '')}`")
            lines.append(f"- Artifact id: `{props.get('artifact_id', '')}`")
            lines.append(f"- Created at: {props.get('created_at', node.created_at)}")
            lines.append(f"- Added nodes: {len(props.get('added_nodes') or [])}")
            lines.append(f"- Updated nodes: {len(props.get('updated_nodes') or [])}")
            lines.append(f"- Archived nodes: {len(props.get('archived_nodes') or [])}")
            lines.append(f"- Added edges: {len(props.get('added_edges') or [])}")
            lines.append(f"- Updated edges: {len(props.get('updated_edges') or [])}")
            lines.append(f"- Archived edges: {len(props.get('archived_edges') or [])}")
            affected = props.get("affected_node_ids") or []
            lines.append(f"- Affected node ids: {', '.join(affected[:10]) if affected else 'No data yet'}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
