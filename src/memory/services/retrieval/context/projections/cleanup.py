"""Cleanup-specific context selection and planning."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .....domain.models import MemoryEdge, MemoryNode, MemorySubgraph, RankedNode
from .....extraction.normalization import tokenize
from ...common import _expanded_tokens


class CleanupContextProjectionMixin:
    def _code_cleanup_targeted_reads(
        self,
        cleanup_candidates: list[dict[str, Any]],
        subgraph: MemorySubgraph,
        working_paths: set[str],
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        nodes: dict[str, MemoryNode] = {item.node.id: item.node for item in subgraph.ranked_nodes}
        nodes.update({node.id: node for node in subgraph.nodes})
        reads: list[dict[str, Any]] = []
        for candidate in cleanup_candidates[: min(max_items, 6)]:
            finding = self.store.get_node(str(candidate.get("id") or ""))
            if finding is None or finding.type != "StaticAnalysisFinding":
                continue
            nodes.setdefault(finding.id, finding)
            symbol = self._cleanup_finding_symbol(finding)
            if symbol is not None:
                nodes.setdefault(symbol.id, symbol)
            reference_reads = self._cleanup_reference_reads(finding, symbol, nodes, max_items=max_items)
            sufficient, reason = self._cleanup_read_sufficiency(finding, symbol, reference_reads)
            reads.extend(self._cleanup_primary_reads(finding, symbol, working_paths, sufficient=sufficient, reason=reason))
            reads.extend(reference_reads)
        return self._merge_targeted_reads(reads, max_items=max(max_items * 4, 12))

    def _cleanup_primary_reads(
        self,
        finding: MemoryNode,
        symbol: MemoryNode | None,
        working_paths: set[str],
        *,
        sufficient: bool,
        reason: str,
    ) -> list[dict[str, Any]]:
        reads: list[dict[str, Any]] = []
        sufficiency = {
            "status": "sufficient" if sufficient else "insufficient",
            "reason": reason,
        }
        symbol_path = self._node_relative_path(symbol) if symbol is not None else None
        if symbol is not None and symbol_path and (not working_paths or symbol_path in working_paths):
            line_start, line_end = self._line_span(symbol)
            if line_start is not None:
                symbol_type = str(symbol.type)
                read_kind = "import_block" if symbol_type == "Import" else "symbol_body"
                reads.append(
                    {
                        "path": symbol_path,
                        "line_start": line_start,
                        "line_end": line_end,
                        "node_id": symbol.id,
                        "type": symbol.type,
                        "label": self._compact_text(self._node_label(symbol), max_chars=100),
                        "reason": "import block for cleanup candidate" if read_kind == "import_block" else "symbol body for cleanup candidate",
                        "read_kind": read_kind,
                        "finding_id": finding.id,
                        "sufficiency": sufficiency,
                    }
                )
        finding_path = self._node_relative_path(finding) or symbol_path
        finding_line_start, finding_line_end = self._line_span(finding)
        if finding_path and finding_line_start is not None:
            context_start = max(1, finding_line_start - 5)
            context_end = max(finding_line_end or finding_line_start, finding_line_start + 5)
            reads.append(
                {
                    "path": finding_path,
                    "line_start": context_start,
                    "line_end": context_end,
                    "node_id": finding.id,
                    "type": finding.type,
                    "label": self._compact_text(self._node_label(finding), max_chars=100),
                    "reason": "5-10 lines around cleanup finding",
                    "read_kind": "finding_context",
                    "finding_id": finding.id,
                    "sufficiency": sufficiency,
                }
            )
        return reads

    def _cleanup_reference_reads(
        self,
        finding: MemoryNode,
        symbol: MemoryNode | None,
        nodes: dict[str, MemoryNode],
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        if symbol is None:
            return []
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        target_ids = {symbol.id, finding.id}
        reference_edge_types = {
            "CALLS",
            "USES",
            "REFERENCES",
            "READS",
            "RETURNS",
            "RAISES",
            "INSTANTIATES",
            "IMPORTS",
            "IMPORTS_FROM",
            "RE_EXPORTS",
            "TESTS",
        }
        for edge in self.store.all_edges():
            if edge.type not in reference_edge_types:
                continue
            if edge.from_id not in target_ids and edge.to_id not in target_ids:
                continue
            other_id = edge.to_id if edge.from_id in target_ids else edge.from_id
            other = nodes.get(other_id) or self.store.get_node(other_id)
            if other is None:
                continue
            if self._is_structural_import_reference(finding, symbol, edge, other):
                continue
            path = self._node_relative_path(other) or self._edge_relative_path(edge)
            if not path or self._is_generated_context_path(path):
                continue
            line_start, line_end = self._line_span(other)
            if line_start is None:
                line_start, line_end = self._edge_line_span(edge)
            if line_start is None:
                continue
            read_kind = self._cleanup_reference_read_kind(edge, other, path)
            key = (read_kind, other.id)
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "path": path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "node_id": other.id,
                    "type": other.type,
                    "label": self._compact_text(self._node_label(other), max_chars=100),
                    "reason": f"{read_kind.replace('_', ' ')} for cleanup candidate",
                    "read_kind": read_kind,
                    "finding_id": finding.id,
                    "edge_id": edge.id,
                    "edge_type": edge.type,
                    "sufficiency": {
                        "status": "insufficient",
                        "reason": "A deterministic reference edge exists; inspect this reference before removing the candidate.",
                    },
                }
            )
            if len(refs) >= min(max_items, 8):
                break
        refs.sort(key=lambda item: (self._cleanup_read_kind_order(str(item.get("read_kind") or "")), str(item.get("path") or ""), int(item.get("line_start") or 0)))
        return refs

    def _is_structural_import_reference(
        self,
        finding: MemoryNode,
        symbol: MemoryNode,
        edge: MemoryEdge,
        other: MemoryNode,
    ) -> bool:
        if symbol.type != "Import" or edge.type not in {"IMPORTS", "IMPORTS_FROM"}:
            return False
        symbol_path = self._node_relative_path(symbol)
        finding_path = self._node_relative_path(finding)
        other_path = self._node_relative_path(other) or self._edge_relative_path(edge)
        if not symbol_path or not other_path:
            return False
        return other_path == symbol_path or (finding_path is not None and other_path == finding_path)

    def _cleanup_finding_symbol(self, finding: MemoryNode) -> MemoryNode | None:
        symbol_id = finding.properties.get("symbol_id")
        if symbol_id:
            symbol = self.store.get_node(str(symbol_id))
            if symbol is not None:
                return symbol
        for edge in self.store.all_edges():
            if edge.to_id != finding.id or edge.type != "HAS_FINDING":
                continue
            node = self.store.get_node(edge.from_id)
            if node is not None and node.type not in {"SourceArtifact", "File"}:
                return node
        return None

    def _cleanup_read_sufficiency(
        self,
        finding: MemoryNode,
        symbol: MemoryNode | None,
        reference_reads: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        props = finding.properties
        if symbol is None:
            return False, "The finding has no resolved symbol node, so the targeted read cannot prove the removal boundary."
        if reference_reads:
            kinds = sorted({str(item.get("read_kind") or "reference") for item in reference_reads})
            return False, f"Reference checks found {', '.join(kinds)}; inspect them before removal."
        blocking = [str(item) for item in props.get("blocking_signals") or []]
        validation = str(props.get("validation_reason") or "").strip()
        if blocking or validation:
            reason = validation or f"Blocking signals remain: {', '.join(blocking)}."
            return False, reason
        safety = str(props.get("removal_safety") or "")
        if safety == "safe":
            return True, "The symbol/import block plus local finding context are enough for a deterministic safe cleanup candidate; no graph references were found."
        return False, f"Removal safety is {safety or 'unknown'}; validate beyond the local read before editing."

    @staticmethod
    def _cleanup_reference_read_kind(edge: MemoryEdge, node: MemoryNode, path: str) -> str:
        normalized = path.replace("\\", "/").casefold()
        if normalized.startswith("tests/") or "/tests/" in normalized or edge.type == "TESTS":
            return "test_ref"
        if normalized.startswith("docs/") or "/docs/" in normalized or normalized == "readme.md" or node.type in {"Docstring", "Comment"}:
            return "doc_ref"
        if edge.type in {"IMPORTS", "IMPORTS_FROM", "RE_EXPORTS"}:
            return "importer_ref"
        return "caller_ref"

    @staticmethod
    def _cleanup_read_kind_order(read_kind: str) -> int:
        return {
            "import_block": 0,
            "symbol_body": 1,
            "finding_context": 2,
            "caller_ref": 3,
            "importer_ref": 4,
            "doc_ref": 5,
            "test_ref": 6,
        }.get(read_kind, 9)

    @staticmethod
    def _merge_targeted_reads(reads: list[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
        merged: OrderedDict[tuple[Any, Any, Any, Any, Any], dict[str, Any]] = OrderedDict()
        spans: set[tuple[Any, Any, Any, Any]] = set()
        for read in reads:
            span_key = (
                read.get("path"),
                read.get("line_start"),
                read.get("line_end"),
                read.get("node_id"),
            )
            if not read.get("read_kind") and span_key in spans:
                continue
            key = (
                read.get("path"),
                read.get("line_start"),
                read.get("line_end"),
                read.get("node_id"),
                read.get("read_kind") or read.get("reason"),
            )
            if key not in merged:
                merged[key] = read
                spans.add(span_key)
        return list(merged.values())[:max_items]

    @staticmethod
    def _edge_relative_path(edge: MemoryEdge) -> str | None:
        value = edge.properties.get("relative_path") or edge.properties.get("source_file") or edge.properties.get("path")
        return str(value) if value else None

    @staticmethod
    def _edge_line_span(edge: MemoryEdge) -> tuple[int | None, int | None]:
        start = edge.properties.get("line_start", edge.properties.get("start_line"))
        end = edge.properties.get("line_end", edge.properties.get("end_line", start))
        try:
            parsed_start = int(start) if start is not None else None
            parsed_end = int(end) if end is not None else parsed_start
        except (TypeError, ValueError):
            return None, None
        return parsed_start, parsed_end

    def _code_cleanup_candidates(
        self,
        ranked: list[RankedNode],
        subgraph: MemorySubgraph,
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        ranked_by_id = {item.node.id: item for item in ranked}
        query_tokens = set(_expanded_tokens(subgraph.query.text))
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in [*(item.node for item in ranked), *subgraph.nodes]:
            if node.id in seen or node.type != "StaticAnalysisFinding":
                continue
            seen.add(node.id)
            if not self._is_safe_cleanup_candidate(node) and not self._is_aggregate_cleanup_candidate(node):
                continue
            if query_tokens and not self._cleanup_finding_matches_query(node, query_tokens):
                continue
            item = ranked_by_id.get(node.id)
            if item is not None:
                payload = self._agent_ranked_payload(item, max_text_chars=260)
            else:
                payload = self._query_explore_node_payload(node)
                payload["score"] = 0.0
                payload["reasons"] = {}
            props = node.properties
            payload["finding_type"] = props.get("finding_type")
            payload["cleanup_priority"] = props.get("cleanup_priority")
            payload["cleanup_rank"] = props.get("cleanup_rank")
            payload["confidence"] = props.get("confidence")
            payload["removal_safety"] = props.get("removal_safety")
            payload["removal_reason"] = props.get("removal_reason")
            payload["validation_reason"] = props.get("validation_reason")
            payload["blocking_signals"] = list(props.get("blocking_signals") or [])
            payload["symbol_name"] = props.get("symbol_name") or props.get("qualified_name") or node.label
            payload["directory"] = props.get("directory")
            payload["file_count"] = props.get("file_count")
            payload["files"] = list(props.get("files") or [])
            candidates.append(payload)
        candidates.sort(key=self._cleanup_candidate_sort_key)
        return candidates[: min(max_items, 8)]

    def _cleanup_candidate_count(self, ranked: list[RankedNode], subgraph: MemorySubgraph) -> int:
        seen: set[str] = set()
        count = 0
        for node in [*(item.node for item in ranked), *subgraph.nodes]:
            if node.id in seen or node.type != "StaticAnalysisFinding":
                continue
            seen.add(node.id)
            count += 1
        return count

    @staticmethod
    def _is_safe_cleanup_candidate(node: MemoryNode) -> bool:
        return (
            node.type == "StaticAnalysisFinding"
            and str(node.properties.get("removal_safety") or "").casefold() == "safe"
            and not list(node.properties.get("blocking_signals") or [])
        )

    @staticmethod
    def _is_aggregate_cleanup_candidate(node: MemoryNode) -> bool:
        return (
            node.type == "StaticAnalysisFinding"
            and node.properties.get("finding_type") == "possibly_orphan_directory"
            and str(node.properties.get("cleanup_priority") or "").casefold() in {"high", "medium"}
        )

    def _filter_cleanup_candidate_payloads(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        safe: list[dict[str, Any]] = []
        for item in candidates:
            node = self.store.get_node(str(item.get("id") or ""))
            if node is not None and (self._is_safe_cleanup_candidate(node) or self._is_aggregate_cleanup_candidate(node)):
                safe.append(item)
        return safe

    @staticmethod
    def _cleanup_filter_payload(*, total_candidates: int, shown_candidates: int) -> dict[str, Any]:
        return {
            "mode": "safe_remove",
            "shown_candidates": shown_candidates,
            "excluded_risky_candidates": max(0, total_candidates - shown_candidates),
        }

    @staticmethod
    def _cleanup_finding_matches_query(node: MemoryNode, query_tokens: set[str]) -> bool:
        fields = [
            node.id,
            node.label,
            node.text,
            node.canonical_key,
            node.properties.get("finding_type"),
            node.properties.get("symbol_name"),
            node.properties.get("qualified_name"),
            node.properties.get("relative_path"),
            node.properties.get("removal_reason"),
            node.properties.get("validation_reason"),
        ]
        tokens: set[str] = set()
        for field in fields:
            if field:
                tokens.update(tokenize(str(field).replace("_", " ").replace(".", " ").replace("/", " ")))
        return bool(tokens & query_tokens)

    @staticmethod
    def _cleanup_candidate_sort_key(item: dict[str, Any]) -> tuple[int, int, float, float, str]:
        safety_order = {"safe": 0, "validate": 1, "risky": 2}
        rank = int(item.get("cleanup_rank") or 0)
        safety = safety_order.get(str(item.get("removal_safety") or "validate"), 1)
        confidence = float(item.get("confidence") or 0.0)
        score = float(item.get("score") or 0.0)
        return (-rank, safety, -confidence, -score, str(item.get("symbol_name") or item.get("id") or ""))

    def _code_cleanup_plan_lines(self, cleanup_candidates: list[dict[str, Any]], path_rows: list[dict[str, Any]], *, max_items: int) -> list[str]:
        lines = [
            "- safe: high-priority unused imports and variables with listed source spans.",
            "- validate: candidates with nearby callers, docs, CLI/MCP tools, configuration, or exports.",
            "- risky: public API, entrypoint, framework lifecycle, or dynamic-reference candidates.",
        ]
        for item in cleanup_candidates[: min(max_items, 4)]:
            safety = item.get("removal_safety") or "validate"
            name = item.get("symbol_name") or item.get("label") or item.get("id")
            reason = item.get("removal_reason") or item.get("validation_reason") or "review candidate before removal"
            lines.append(f"- `{name}` safety={safety}: {reason}")
        if not cleanup_candidates:
            lines.append("- No safe-remove cleanup candidate matched this query; validate/risky candidates are outside the default result set.")
            return lines
        for row in path_rows[: min(max_items, 4)]:
            path = row.get("path")
            if path:
                lines.append(f"- Candidate file: `{path}`; source spans are listed in targeted_reads/snippets when available.")
        return lines
