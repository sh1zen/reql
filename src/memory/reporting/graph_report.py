"""Main project graph Markdown report."""
from __future__ import annotations

from collections import Counter
from typing import Any

from ..domain.models import MemoryNode


class GraphReportBuilder:
    def render(self, context: dict[str, Any]) -> str:
        lines = ["# REQL Project Report", "", f"Generated at: {context['generated_at']}", ""]
        self._project_summary(lines, context)
        self._compilation_summary(lines, context)
        self._cache_summary(lines, context)
        self._artifact_ingestion(lines, context)
        self._code_graph(lines, context)
        self._communities(lines, context)
        self._hubs(lines, context)
        self._health(lines, context)
        return "\n".join(lines).rstrip() + "\n"

    def _project_summary(self, lines: list[str], c: dict[str, Any]) -> None:
        artifacts = c["artifacts"]
        active = sum(1 for node in artifacts if node.status == "active")
        archived = sum(1 for node in artifacts if node.status == "archived")
        lines.extend(["## Project summary", ""])
        project = c["project"]
        lines.append(f"- Project id: `{c['project_id']}`")
        lines.append(f"- Root path: `{c['root_path']}`")
        lines.append(f"- Name: {project.label if project else _name(c['root_path'])}")
        lines.append(f"- Status: {project.status if project else 'unregistered'}")
        lines.append(f"- Total artifacts: {len(artifacts)}")
        lines.append(f"- Active artifacts: {active}")
        lines.append(f"- Archived artifacts: {archived}")
        self._counter(lines, "Artifact types", _count_prop(artifacts, "artifact_type"))
        self._counter(lines, "Languages", _count_prop(artifacts, "language"))

    def _compilation_summary(self, lines: list[str], c: dict[str, Any]) -> None:
        lines.extend(["", "## Compilation summary", ""])
        latest = c["runs"][0] if c["runs"] else None
        if not latest:
            lines.append("- No data yet.")
            return
        props = latest.properties
        lines.append(f"- Latest run: `{latest.id}`")
        lines.append(f"- Status: {props.get('status', latest.label)}")
        for key in ["files_seen", "files_changed", "files_skipped", "files_deleted", "nodes_created", "nodes_updated", "edges_created", "edges_updated"]:
            lines.append(f"- {key.replace('_', ' ').title()}: {props.get(key, 0)}")
        errors = props.get("errors") or []
        if errors:
            lines.append("- Errors:")
            lines.extend(f"  - {error}" for error in errors)
        else:
            lines.append("- Errors: none")

    def _cache_summary(self, lines: list[str], c: dict[str, Any]) -> None:
        lines.extend(["", "## Cache summary", ""])
        entries = c["cache_entries"]
        status = c.get("scan_status") or {}
        lines.append(f"- Cached artifacts: {status.get('cached_artifacts', sum(1 for n in entries if n.status == 'active'))}")
        lines.append(f"- Dirty artifacts: {status.get('dirty_artifacts', 0)}")
        lines.append(f"- Skipped artifacts: {len(c.get('skipped_files') or [])}")
        self._counter(lines, "Parser versions", _count_prop(entries, "parser_version"))
        self._counter(lines, "Chunking versions", _count_prop(entries, "chunking_version"))

    def _artifact_ingestion(self, lines: list[str], c: dict[str, Any]) -> None:
        artifacts = c["artifacts"]
        lines.extend(["", "## Artifact ingestion", ""])
        if not artifacts:
            lines.append("- No data yet.")
            return
        top = sorted(artifacts, key=lambda n: int(n.properties.get("size_bytes") or 0), reverse=True)[:10]
        lines.append("Top artifacts by size:")
        for node in top:
            lines.append(f"- `{node.properties.get('relative_path', node.label)}` size={node.properties.get('size_bytes', 0)} parser={node.properties.get('parser_name', 'not compiled')} status={node.properties.get('status', node.status)}")
        parser_errors = [node for node in artifacts if node.properties.get("parser_errors")]
        partial = [node for node in artifacts if node.properties.get("status") == "partially_readable"]
        needs_ocr = [node for node in artifacts if node.properties.get("status") == "needs_ocr"]
        needs_parser = [node for node in artifacts if node.properties.get("status") == "needs_parser"]
        self._node_list(lines, "Parser errors", parser_errors, include_errors=True)
        self._node_list(lines, "Partially readable artifacts", partial)
        self._node_list(lines, "Needs OCR artifacts", needs_ocr)
        self._node_list(lines, "Needs parser artifacts", needs_parser)

    def _code_graph(self, lines: list[str], c: dict[str, Any]) -> None:
        nodes = c["code_nodes"]
        counts = Counter(node.type for node in nodes)
        lines.extend(["", "## Code graph summary", ""])
        for type_ in ["Module", "Class", "Function", "Method", "Variable", "Import", "StaticAnalysisFinding"]:
            lines.append(f"- {type_.lower()}s: {counts.get(type_, 0)}")
        lines.append(f"- call edges: {len(c['call_edges'])}")
        top = sorted([node for node in nodes if node.type in {"Class", "Function", "Method", "CodeSymbol"} and not _external_code_symbol(node)], key=lambda n: (n.salience, n.properties.get("name") or ""), reverse=True)[:10]
        self._node_list(lines, "Top symbols", top, name_field="qualified_name")
        cleanup = sorted(
            [node for node in nodes if node.type == "StaticAnalysisFinding"],
            key=lambda n: (_cleanup_priority_rank(n), float(n.properties.get("confidence") or n.confidence)),
            reverse=True,
        )[:10]
        self._node_list(lines, "Top cleanup candidates", cleanup, name_field="qualified_name")

    def _communities(self, lines: list[str], c: dict[str, Any]) -> None:
        lines.extend(["", "## Communities", ""])
        communities = sorted(c["communities"], key=lambda n: (float(n.properties.get("salience") or n.salience), int(n.properties.get("size") or 0)), reverse=True)[:10]
        if not communities:
            lines.append("- No data yet.")
            return
        for node in communities:
            lines.append(f"- `{node.id}` {node.label}: size={node.properties.get('size', 0)}, density={float(node.properties.get('density') or 0):.2f}, salience={node.salience:.2f}")

    def _hubs(self, lines: list[str], c: dict[str, Any]) -> None:
        lines.extend(["", "## God nodes / hubs", ""])
        hubs = c["hubs"][:10]
        if not hubs:
            lines.append("- No data yet.")
        for hub in hubs:
            lines.append(f"- rank={hub.hub_rank} `{hub.node_type}` score={hub.hub_score:.2f}: {hub.label} - {'; '.join(hub.reasons[:4])}")
        warnings = c["hub_warnings"] or [f"Generic high-degree node: {n.label or n.id} degree={degree} penalty={penalty:.2f}" for n, degree, penalty in c["generic_warnings"]]
        self._plain_list(lines, "Generic hub warnings", warnings)

    def _health(self, lines: list[str], c: dict[str, Any]) -> None:
        lines.extend(["", "## Memory health", ""])
        lines.append(f"- Stale nodes: {len(c['stale_nodes'])}")
        lines.append(f"- Archived nodes: {len(c['archived_nodes'])}")
        lines.append(f"- Overly generic high-degree nodes: {len(c['generic_warnings'])}")
        actions = []
        if c["stale_nodes"]:
            actions.append("Review stale active nodes and archive stale project facts.")
        if c["generic_warnings"]:
            actions.append("Rename or split generic high-degree nodes so retrieval stays specific.")
        if not actions:
            actions.append("No immediate action recommended.")
        self._plain_list(lines, "Recommended actions", actions)

    @staticmethod
    def _counter(lines: list[str], title: str, counts: Counter[str]) -> None:
        lines.append(f"{title}:")
        if not counts:
            lines.append("- No data yet.")
            return
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {key}: {count}")

    @staticmethod
    def _node_list(lines: list[str], title: str, nodes: list[MemoryNode], *, include_errors: bool = False, name_field: str = "relative_path") -> None:
        lines.append(f"{title}:")
        if not nodes:
            lines.append("- No data yet.")
            return
        for node in nodes[:10]:
            name = node.properties.get(name_field) or node.properties.get("name") or node.label or node.id
            suffix = ""
            if include_errors:
                suffix = " - " + "; ".join(str(e) for e in node.properties.get("parser_errors", [])[:2])
            lines.append(f"- `{name}` status={node.properties.get('status', node.status)}{suffix}")

    @staticmethod
    def _plain_list(lines: list[str], title: str, items: list[str]) -> None:
        lines.append(f"{title}:")
        if not items:
            lines.append("- No data yet.")
            return
        lines.extend(f"- {item}" for item in items[:10])

def _count_prop(nodes: list[MemoryNode], prop: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for node in nodes:
        value = node.properties.get(prop) or "unknown"
        counts[str(value)] += 1
    return counts


def _name(path: str) -> str:
    return path.rstrip("/").rstrip("\\").split("/")[-1].split("\\")[-1] or path


def _external_code_symbol(node: MemoryNode) -> bool:
    return node.type == "CodeSymbol" and (node.properties.get("external") or node.properties.get("synthetic") or node.properties.get("kind") in {"external", "decorator"})


def _cleanup_priority_rank(node: MemoryNode) -> int:
    priority = str(node.properties.get("cleanup_priority") or "low").lower()
    return {"high": 3, "medium": 2, "low": 1}.get(priority, 0)
