"""Markdown rendering for query, exploration, and context payloads."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Sequence

from .....domain.models import MemoryEdge, MemoryNode
from ...common import (
    QUERY_CONTEXT_MAX_RENDERED_FILES,
    QUERY_CONTEXT_MAX_RENDERED_TEST_FILES,
    QUERY_CONTEXT_MIN_CONFIDENCE_SCORE,
    SOURCE_NODE_TYPES,
)


class MarkdownContextRendererMixin:
    def _render_query_explore_payload(self, payload: dict[str, Any]) -> str:
        lines = ["# REQL Query Explore", "", f"Query: {payload.get('query', '')}", ""]
        seed_nodes = list(payload.get("seed_nodes") or [])
        if seed_nodes:
            lines.append("## Seed Nodes")
            for node in seed_nodes[:8]:
                lines.append(f"- `{node['id']}` [{node['type']}] {node.get('label') or node.get('text') or ''} {node.get('location') or ''}".rstrip())
            lines.append("")
        sections = payload.get("sections") or {}
        titles = {
            "owners": "Owners",
            "callers": "Callers",
            "public_surface": "Public Surface",
            "serialization_paths": "Serialization Paths",
            "docs_mentions": "Docs Mentions",
            "structural_duplicates": "Structural Duplicates",
            "code": "Code",
        }
        for key, title in titles.items():
            if key not in sections:
                continue
            value = sections[key]
            lines.append(f"## {title}")
            if key == "code":
                for row in value.get("working_set", []):
                    span = self._format_line_span(row.get("line_start"), row.get("line_end"))
                    lines.append(f"- working_set `{row['path']}` [{row['role']}] score={float(row['score']):.2f}{span}")
                for row in value.get("targeted_reads", []):
                    location = self._format_path_span(row.get("path"), row.get("line_start"), row.get("line_end"))
                    lines.append(f"- read `{location}` {row.get('reason')}: {row.get('label')}")
            elif value:
                for row in value:
                    lines.append(self._render_query_explore_row(row))
            else:
                lines.append("- No matches in this view.")
            lines.append("")
        lines.extend(self._render_counts(payload))
        return "\n".join(lines).strip()

    def _render_query_explore_row(self, row: dict[str, Any]) -> str:
        if isinstance(row.get("source"), dict) and isinstance(row.get("duplicate"), dict):
            source = row["source"]
            duplicate = row["duplicate"]
            shared = ", ".join(row.get("shared_patterns") or [])
            shared_suffix = f"; shared={shared}" if shared else ""
            return (
                f"- `{source.get('location') or source.get('id')}` <-> "
                f"`{duplicate.get('location') or duplicate.get('id')}`; "
                f"similarity={float(row.get('similarity', 0.0)):.2f}; {row.get('reason')}{shared_suffix}"
            )
        node = row.get("owner") or row.get("caller") or row.get("surface") or row.get("node") or row.get("mention") or row.get("target")
        if not isinstance(node, dict):
            return f"- {row.get('reason', 'match')}"
        location = f" @ {node['location']}" if node.get("location") else ""
        edge = row.get("edge")
        relation = f"; edge={edge.get('type')}:{edge.get('id')}" if isinstance(edge, dict) else ""
        return f"- `{node['id']}` [{node['type']}] {node.get('label') or node.get('text') or ''}{location}; {row.get('reason', 'match')}{relation}"

    def _query_explore_node_payload(self, node: MemoryNode, ranked_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "id": node.id,
            "type": node.type,
            "label": self._compact_text(self._node_label(node), max_chars=140),
            "text": self._compact_text(node.text or "", max_chars=260),
            "location": self._location_summary(node),
            "properties": self._query_explore_properties(node),
        }
        if ranked_payload:
            payload["score"] = ranked_payload.get("score")
            payload["reasons"] = ranked_payload.get("reasons")
        return payload

    def _query_explore_edge_payload(self, edge: MemoryEdge, nodes: OrderedDict[str, MemoryNode]) -> dict[str, Any]:
        payload = self._edge_context_payload(edge, nodes)
        payload["location"] = self._location_summary(edge)
        return payload

    @staticmethod
    def _query_explore_properties(node: MemoryNode) -> dict[str, Any]:
        keys = {
            "alias",
            "artifact_id",
            "kind",
            "line_end",
            "line_start",
            "module",
            "name",
            "path",
            "qualified_name",
            "relative_path",
            "source_file",
            "source_path",
            "symbol_name",
            "target",
            "is_re_export",
            "unresolved_call_count",
        }
        return {key: value for key, value in node.properties.items() if key in keys}

    @staticmethod
    def _is_public_surface_node(node: MemoryNode) -> bool:
        if node.type in {"Module", "Class", "Interface", "Endpoint", "Schema"}:
            return True
        if node.type == "Import":
            return bool(node.properties.get("is_re_export"))
        if node.type in {"Function", "Method"}:
            name = str(node.properties.get("name") or node.label or "")
            return bool(name and not name.startswith("_"))
        return False

    def _is_docs_mention_node(self, node: MemoryNode) -> bool:
        if node.type not in SOURCE_NODE_TYPES and node.type not in {"Docstring", "Comment"}:
            return False
        path = self._node_relative_path(node) or self._location_summary(node) or ""
        normalized = path.replace("\\", "/").casefold()
        return node.type in {"Docstring", "Comment"} or normalized.startswith("docs/") or normalized == "readme.md" or "/docs/" in normalized


    def _render_query_context_payload(self, payload: dict[str, Any]) -> str:
        if payload.get("kind") == "code":
            return self._render_code_context_payload(payload)
        return self._render_general_context_payload(payload)

    def _render_code_context_payload(self, payload: dict[str, Any]) -> str:
        query_mode = str(payload.get("query_mode") or "informative")
        if query_mode == "cleanup":
            return self._render_cleanup_context_payload(payload)

        lines = self._render_context_header(payload, title="# REQL Context")
        if self._query_context_confidence_is_insufficient(payload):
            return "\n".join(lines).strip()
        working_set = list(payload.get("working_set") or [])
        owner_candidates = list(payload.get("owner_candidates") or [])
        targeted_reads = list(payload.get("targeted_reads") or [])
        tests: list[dict[str, Any]] = []
        seen_test_paths: set[str] = set()
        for item in list(payload.get("test_targets") or []):
            path = str(item.get("path") or "")
            if item.get("kind") != "test" or not path or path in seen_test_paths:
                continue
            seen_test_paths.add(path)
            tests.append(item)
            if len(tests) >= QUERY_CONTEXT_MAX_RENDERED_TEST_FILES:
                break

        source_limit = max(5, QUERY_CONTEXT_MAX_RENDERED_FILES - len(tests)) if tests else QUERY_CONTEXT_MAX_RENDERED_FILES
        source_rows = working_set[:source_limit]
        rendered_paths = {str(row.get("path") or "") for row in source_rows}

        def best_span(path: str, row: dict[str, Any]) -> tuple[Any, Any]:
            if row.get("line_start") is not None or row.get("line_end") is not None:
                return row.get("line_start"), row.get("line_end")
            for item in owner_candidates:
                if item.get("path") == path and (item.get("line_start") is not None or item.get("line_end") is not None):
                    return item.get("line_start"), item.get("line_end")
            for item in targeted_reads:
                if item.get("path") == path and (item.get("line_start") is not None or item.get("line_end") is not None):
                    return item.get("line_start"), item.get("line_end")
            return None, None

        file_lines: list[str] = []
        for row in source_rows:
            path = str(row.get("path") or "")
            if not path:
                continue
            line_start, line_end = best_span(path, row)
            location = self._format_path_bracket_span(path, line_start, line_end)
            symbols: list[str] = []
            for symbol in list(row.get("symbols") or []):
                if symbol and symbol not in symbols:
                    symbols.append(str(symbol))
            for item in owner_candidates:
                symbol = item.get("name") or item.get("label")
                if item.get("path") == path and symbol and symbol not in symbols:
                    symbols.append(str(symbol))
            owner_note = f"; owners={', '.join(symbols[:4])}" if symbols else "; owners=none identified"
            file_lines.append(f"- `{location}`{owner_note}")
        if not file_lines:
            file_lines.append("- No source files matched this query.")
        self._append_section(lines, "Files", file_lines)

        test_lines: list[str] = []
        for item in tests:
            path = str(item.get("path") or "")
            if path in rendered_paths:
                continue
            location = self._format_path_bracket_span(path, item.get("line_start"), item.get("line_end"))
            symbols = [str(symbol) for symbol in list(item.get("symbols") or []) if symbol]
            owner_note = f"; owners={', '.join(symbols[:3])}" if symbols else ""
            test_lines.append(f"- `{location}`{owner_note}")
        if not test_lines:
            test_lines.append("- No associated tests found in the graph.")
        self._append_section(lines, "Associated tests", test_lines)
        return "\n".join(lines).strip()

    def _render_general_context_payload(self, payload: dict[str, Any]) -> str:
        if str(payload.get("query_mode") or "informative") == "cleanup":
            return self._render_cleanup_context_payload(payload)

        lines = self._render_context_header(payload, title="# REQL Context")
        if self._query_context_confidence_is_insufficient(payload):
            return "\n".join(lines).strip()
        results = list(payload["results"])
        result_lines: list[str] = []
        if results:
            for item in results:
                result_lines.extend(self._render_general_result_lines(item))
        else:
            result_lines.append("- No ranked nodes matched this query.")
        self._append_section(lines, "Results", result_lines)
        return "\n".join(lines).strip()

    def _render_cleanup_context_payload(self, payload: dict[str, Any]) -> str:
        lines = self._render_context_header(payload, title="# REQL Cleanup Context")
        if self._query_context_confidence_is_insufficient(payload):
            return "\n".join(lines).strip()
        cleanup_filter = payload.get("cleanup_filter") or {}
        if cleanup_filter:
            mode = cleanup_filter.get("mode") or "safe_remove"
            excluded = cleanup_filter.get("excluded_risky_candidates", 0)
            self._append_section(lines, "Cleanup filter", [f"- mode={mode}; shown={cleanup_filter.get('shown_candidates', 0)}; excluded_risky={excluded}; validate/risky findings excluded"])
        cleanup = list(payload.get("cleanup_candidates") or [])
        result_lines: list[str] = []
        if not cleanup:
            result_lines.append("- No cleanup candidates matched this query.")
        for item in cleanup:
            location = f" @ {item['location']}" if item.get("location") else ""
            name = item.get("symbol_name") or item.get("label") or item.get("id")
            finding_type = f"; finding={item.get('finding_type')}" if item.get("finding_type") else ""
            priority = f"; priority={item.get('cleanup_priority')}" if item.get("cleanup_priority") else ""
            safety = f"; safety={item.get('removal_safety')}" if item.get("removal_safety") else ""
            reason = f"; reason={item.get('removal_reason')}" if item.get("removal_reason") else ""
            validation = f"; validate={item.get('validation_reason')}" if item.get("validation_reason") else ""
            result_lines.append(f"- cleanup `{item['id']}` {name}{location}{finding_type}{priority}{safety}{reason}{validation}")
        self._append_section(lines, "Cleanup candidates", result_lines)
        read_lines: list[str] = []
        for item in list(payload.get("targeted_reads") or [])[:12]:
            location = self._format_path_bracket_span(item.get("path"), item.get("line_start"), item.get("line_end"))
            kind = item.get("read_kind") or "read"
            sufficiency = item.get("sufficiency") or {}
            status = sufficiency.get("status")
            status_text = f"; {status}: {sufficiency.get('reason')}" if status else ""
            read_lines.append(f"- {kind} `{location}` from `{item.get('node_id')}` [{item.get('type')}] {item.get('reason')}{status_text}")
        self._append_section(lines, "Targeted reads", read_lines)
        self._append_section(lines, "Change chain", self._render_change_chain_lines(payload.get("change_chain") or []))
        self._append_section(lines, "Snippets", self._render_snippet_lines(payload.get("snippets") or [], limit=3))
        self._append_section(lines, "Research queries", self._render_research_refs(payload))
        self._append_section(lines, "Summary", self._render_compact_counts(payload))
        return "\n".join(lines).strip()

    def _render_change_chain_lines(self, change_chain: Sequence[dict[str, Any]], *, limit: int = 5) -> list[str]:
        lines: list[str] = []
        for step in list(change_chain)[:limit]:
            phase = step.get("phase") or "step"
            description = step.get("description") or ""
            lines.append(f"- {phase}: {description}")
            for item in list(step.get("items") or [])[:3]:
                lines.append(f"  - {self._render_change_chain_item(item)}")
        return lines

    def _render_change_chain_item(self, item: dict[str, Any]) -> str:
        if item.get("source_span"):
            return f"`{item.get('source_span')}` via `{item.get('node_id')}`; {item.get('reason') or 'graph match'}"
        if item.get("command"):
            return f"`{item.get('command')}`; {item.get('reason') or item.get('kind') or 'verify'}"
        if item.get("surface"):
            surface = item.get("surface") or {}
            return f"{item.get('impact_kind', 'impact')} `{surface.get('id')}` {surface.get('label')} @ {surface.get('location') or surface.get('path') or 'unknown'}"
        if item.get("caller"):
            caller = item.get("caller") or {}
            target = item.get("target") or {}
            return f"{item.get('impact_kind', 'impact')} `{caller.get('id')}` -> `{target.get('id')}`; {item.get('reason') or item.get('edge_type') or 'caller'}"
        if item.get("impact_kind") == "docs":
            return f"docs `{item.get('location') or item.get('path')}`; {item.get('reason') or 'documentation mention'}"
        if item.get("impact_kind") == "note":
            return str(item.get("reason") or "")
        if item.get("location") or item.get("path"):
            name = item.get("name") or item.get("label") or item.get("id") or item.get("node_id")
            location = item.get("location") or self._format_path_bracket_span(item.get("path"), item.get("line_start"), item.get("line_end"))
            related = item.get("related") or []
            related_text = f"; related={', '.join(str(value) for value in related[:2])}" if related else ""
            return f"`{item.get('id') or item.get('node_id')}` {name} @ {location}{related_text}"
        return self._compact_text(str(item), max_chars=220)

    def _render_snippet_lines(self, snippets: Sequence[dict[str, Any]], *, limit: int) -> list[str]:
        snippet_lines: list[str] = []
        for item in list(snippets)[:limit]:
            location = self._format_path_bracket_span(item.get("path"), item.get("line_start"), item.get("line_end"))
            snippet_lines.append(f"- `{location}` ({item.get('type')}; {item.get('source')})")
            text = str(item.get("text") or "")
            if text:
                snippet_lines.extend(f"  {line}" for line in text.splitlines()[:12])
        return snippet_lines

    @staticmethod
    def _append_section(lines: list[str], title: str, body: list[str]) -> None:
        if not body:
            return
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"## {title}")
        lines.extend(body)

    @staticmethod
    def _render_context_header(payload: dict[str, Any], *, title: str) -> list[str]:
        lines = [title, f"Query: {payload.get('query', '')}", f"Mode: {payload.get('query_mode', 'informative')}"]
        scopes = list(payload.get("scopes") or [])
        if scopes:
            lines.append(f"Scope: {', '.join(scopes)}")
        confidence = payload.get("confidence") or {}
        if confidence.get("status") == "insufficient":
            max_score = float(confidence.get("max_score", 0.0) or 0.0)
            threshold = float(confidence.get("threshold", QUERY_CONTEXT_MIN_CONFIDENCE_SCORE) or QUERY_CONTEXT_MIN_CONFIDENCE_SCORE)
            lines.append(
                f"Confidence: insufficient (max score {max_score:.3f} < {threshold:.3f}); targeted rg fallback allowed."
            )
        return lines

    @staticmethod
    def _query_context_confidence_is_insufficient(payload: dict[str, Any]) -> bool:
        return (payload.get("confidence") or {}).get("status") == "insufficient"

    def _render_research_refs(self, payload: dict[str, Any]) -> list[str]:
        query = self._reql_string(str(payload.get("query") or ""))
        ids: list[str] = []
        for group in ("results", "owner_candidates", "cleanup_candidates"):
            for item in list(payload.get(group) or []):
                node_id = item.get("id") or item.get("node_id")
                if node_id and node_id not in ids:
                    ids.append(str(node_id))
                if len(ids) >= 3:
                    break
            if len(ids) >= 3:
                break
        lines = [
            f"- research raw rows: `reql query 'RETRIEVE {query} LIMIT 8 RETURN id,type,text,score,source_for,relation,direction,relative_path,line_start,line_end'`",
            f"- research graph: `reql query_graph --query {query} --max-depth 3 --json`",
        ]
        if ids:
            first = ids[0]
            lines.append(f"- research inspect: `reql inspect --node-id {first} --json`")
            id_list = ", ".join(self._reql_string(node_id) for node_id in ids)
            lines.append(f"- research compare: `reql query 'FIND nodes WHERE id IN [{id_list}] RETURN id,type,label,text,relative_path,line_start,line_end'`")
        return lines

    @staticmethod
    def _render_compact_counts(payload: dict[str, Any]) -> list[str]:
        counts = payload.get("counts") or {}
        rendered = ", ".join(f"{key}={value}" for key, value in counts.items())
        lines = [f"Counts: {rendered}" if rendered else "Counts: none"]
        if payload.get("trace_id"):
            lines.append(f"Trace: {payload['trace_id']}")
        return lines

    def _render_agent_node_payload_lines(self, item: dict[str, Any]) -> list[str]:
        label = self._compact_text(str(item.get("label") or item.get("text") or item.get("id") or ""), max_chars=140)
        line = f"- ({float(item.get('score', 0.0)):.2f}) `{item.get('id')}` [{item.get('type')}] {label}"
        if item.get("location"):
            line += f" @ {item['location']}"
        lines = [line]
        text = self._compact_text(str(item.get("text") or ""), max_chars=220)
        if text and text != label:
            lines.append(f"  text: {text}")
        return lines

    def _render_general_result_lines(self, item: dict[str, Any]) -> list[str]:
        label = self._compact_text(str(item.get("label") or item.get("text") or item.get("id") or ""), max_chars=140)
        prefix = "- source" if item.get("kind") == "source" else f"- ({float(item.get('score', 0.0)):.2f})"
        line = f"{prefix} `{item.get('id')}` [{item.get('type')}] {label}"
        if item.get("location"):
            line += f" @ {item['location']}"
        source_locations = [str(value) for value in item.get("source_locations", []) if value]
        if source_locations:
            line += f"; source={', '.join(source_locations[:3])}"
        source_ids = [str(value) for value in item.get("source_ids", []) if value]
        if source_ids:
            rendered_ids = ", ".join(f"`{source_id}`" for source_id in source_ids[:3])
            line += f"; source_ids={rendered_ids}"
        lines = [line]
        text = self._compact_text(str(item.get("text") or ""), max_chars=260)
        if text and text != label:
            lines.append(f"  text: {text}")
        return lines

    @staticmethod
    def _render_followups(followups: list[dict[str, str]]) -> list[str]:
        return [f"- {item['label']}: `{item['command']}` ({item.get('purpose', '')})" for item in followups]

    @staticmethod
    def _render_counts(payload: dict[str, Any]) -> list[str]:
        lines = ["## Counts"]
        counts = payload.get("counts") or {}
        for key, value in counts.items():
            lines.append(f"- {key}: {value}")
        if payload.get("trace_id"):
            lines.append(f"- trace_id: {payload['trace_id']}")
        return lines

    @staticmethod
    def _format_line_span(line_start: Any, line_end: Any) -> str:
        if line_start is None and line_end is None:
            return ""
        if line_end is None or line_end == line_start:
            return f" lines={line_start}"
        return f" lines={line_start}-{line_end}"

    def _format_path_span(self, path: Any, line_start: Any, line_end: Any) -> str:
        if not path:
            return ""
        if line_start is None and line_end is None:
            return str(path)
        if line_end is None or line_end == line_start:
            return f"{path}:{line_start}"
        return f"{path}:{line_start}-{line_end}"

    def _format_path_bracket_span(self, path: Any, line_start: Any, line_end: Any) -> str:
        if not path:
            return ""
        if line_start is None and line_end is None:
            return str(path)
        if line_end is None or line_end == line_start:
            return f"{path} [{line_start}]"
        return f"{path} [{line_start}-{line_end}]"
