"""Code-context working set, source-span, and change-plan projection."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any, Sequence

from .....domain.models import MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from .....extraction.normalization import canonicalize, tokenize
from ...common import (
    CALLER_EDGE_TYPES,
    CODE_CONTEXT_EDGE_TYPES,
    CODE_CONTEXT_NODE_TYPES,
    PUBLIC_SURFACE_EDGE_TYPES,
    QUERY_CONTEXT_MAX_RENDERED_FILES,
    QUERY_CONTEXT_MODES,
    QUERY_CONTEXT_SCOPES,
    SOURCE_EDGE_TYPES,
    SOURCE_NODE_TYPES,
    TECHNICAL_NODE_TYPES,
    _code_context_query_tokens,
    _expanded_tokens,
)


class CodeContextProjectionMixin:
    def _code_agent_context_payload(
        self,
        subgraph: MemorySubgraph,
        *,
        query_mode: str,
        max_items: int,
    ) -> dict[str, Any]:
        compact_items = min(max_items, 6)
        ranked = [item for item in subgraph.ranked_nodes if self._is_code_context_node(item.node)]
        path_rows = self._code_working_set_rows(ranked, list(subgraph.nodes), query_text=subgraph.query.text, max_items=max_items)
        working_paths = {str(row["path"]) for row in path_rows}
        owner_candidates = self._code_owner_candidates(ranked, subgraph, working_paths, max_items=max_items)
        owner_candidate_ids = {str(item["id"]) for item in owner_candidates}
        owner_candidate_paths = {str(item["path"]) for item in owner_candidates if item.get("path")}
        display_ranked = [
            item
            for item in ranked
            if (
                item.node.id in owner_candidate_ids
                or self._node_relative_path(item.node) in owner_candidate_paths
                or (
                    not owner_candidate_ids
                    and working_paths
                    and (
                        self._node_relative_path(item.node) in working_paths
                        or float(item.reasons.get("match_score", 0.0) or 0.0) >= 0.04
                    )
                )
            )
        ]
        cleanup_candidates = self._code_cleanup_candidates(
            display_ranked,
            subgraph,
            max_items=compact_items,
        ) if query_mode == "cleanup" else []
        if query_mode == "cleanup":
            cleanup_reads = self._code_cleanup_targeted_reads(cleanup_candidates, subgraph, working_paths, max_items=compact_items)
            targeted_reads = self._merge_targeted_reads(cleanup_reads, max_items=max(compact_items * 4, 12))
            snippets = self._code_snippet_payload(targeted_reads, subgraph, max_items=max_items)
        else:
            targeted_reads = self._code_targeted_reads(display_ranked, subgraph, working_paths, query_text=subgraph.query.text, max_items=compact_items)
            snippets = self._code_informative_snippet_payload(
                targeted_reads,
                subgraph,
                path_rows=path_rows,
                max_items=max_items,
            )
        cleanup_plan = self._code_cleanup_plan_lines(cleanup_candidates, path_rows, max_items=compact_items) if query_mode == "cleanup" else []
        contracts = self._code_contract_payload(display_ranked, subgraph, working_paths, max_items=compact_items)
        impact = self._code_impact_payload(subgraph, owner_candidates, working_paths, max_items=compact_items)
        test_targets = self._code_test_targets(subgraph, path_rows, query_text=subgraph.query.text, max_items=max_items)
        read_plan = self._code_read_plan_payload(targeted_reads, snippets=snippets, max_items=max_items)
        change_chain = self._code_change_chain_payload(
            owner_candidates=owner_candidates,
            read_plan=read_plan,
            contracts=contracts,
            impact=impact,
            max_items=max_items,
        )
        followups = (
            self._code_follow_up_payload(subgraph, path_rows, max_items=max_items)
            if self._code_context_needs_followups(
                query_mode=query_mode,
                path_rows=path_rows,
                cleanup_candidates=cleanup_candidates,
                targeted_reads=targeted_reads,
                snippets=snippets,
            )
            else []
        )
        return {
            "kind": "code",
            "query": subgraph.query.text,
            "query_mode": query_mode,
            "cleanup_filter": self._cleanup_filter_payload(
                total_candidates=self._cleanup_candidate_count(display_ranked, subgraph),
                shown_candidates=len(cleanup_candidates),
            ) if query_mode == "cleanup" else {},
            "read_plan": read_plan,
            "change_chain": change_chain,
            "owner_candidates": owner_candidates,
            "cleanup_candidates": cleanup_candidates,
            "working_set": self._code_working_set_payload(
                path_rows,
                query_mode=query_mode,
                max_items=min(max_items, QUERY_CONTEXT_MAX_RENDERED_FILES),
            ),
            "contracts": contracts,
            "impact": impact,
            "targeted_reads": targeted_reads,
            "snippets": snippets,
            "edit_plan": [],
            "cleanup_plan": cleanup_plan,
            "test_targets": test_targets,
            "followups": followups,
            "counts": {
                "working_set_files": len(path_rows),
                "ranked_nodes": len(display_ranked),
                "context_nodes": len([node for node in subgraph.nodes if self._is_code_context_node(node)]),
                "edges": len([edge for edge in subgraph.edges if edge.type in CODE_CONTEXT_EDGE_TYPES]),
            },
            "trace_id": subgraph.trace_id,
        }

    def _code_read_plan_payload(
        self,
        targeted_reads: list[dict[str, Any]],
        *,
        snippets: list[dict[str, Any]],
        max_items: int,
    ) -> list[dict[str, Any]]:
        snippet_spans = {
            (item.get("path"), item.get("line_start"), item.get("line_end"))
            for item in snippets
        }
        plan: list[dict[str, Any]] = []
        for item in targeted_reads[: min(max_items, 8)]:
            path = item.get("path")
            line_start = item.get("line_start")
            line_end = item.get("line_end")
            if not path or line_start is None:
                continue
            try:
                start = int(line_start)
                end = int(line_end) if line_end is not None else start
            except (TypeError, ValueError):
                continue
            line_count = max(1, end - start + 1)
            node_id = str(item.get("node_id") or "")
            span_key = (path, start, end)
            plan.append(
                {
                    "path": path,
                    "line_start": start,
                    "line_end": end,
                    "line_count": line_count,
                    "node_id": node_id,
                    "type": item.get("type"),
                    "label": item.get("label"),
                    "reason": item.get("reason") or "graph match",
                    "source_span": self._format_path_bracket_span(path, start, end),
                    "command": f"reql inspect --node-id {node_id} --json" if node_id else "",
                    "snippet_embedded": span_key in snippet_spans,
                    "sufficiency": item.get("sufficiency")
                    or {
                        "status": "bounded",
                        "reason": "source span returned by graph retrieval",
                    },
                }
            )
        return plan

    def _code_change_chain_payload(
        self,
        *,
        owner_candidates: list[dict[str, Any]],
        read_plan: list[dict[str, Any]],
        contracts: list[dict[str, Any]],
        impact: dict[str, Any],
        max_items: int,
    ) -> list[dict[str, Any]]:
        limit = min(max_items, 4)
        chain: list[dict[str, Any]] = []
        if owner_candidates:
            chain.append(
                {
                    "phase": "start",
                    "description": "owner symbols with direct or linked query evidence",
                    "items": owner_candidates[:limit],
                }
            )
        if read_plan:
            chain.append(
                {
                    "phase": "read",
                    "description": "bounded source spans associated with the owner/context nodes",
                    "items": read_plan[:limit],
                }
            )
        if contracts:
            chain.append(
                {
                    "phase": "preserve",
                    "description": "contracts, public-shaped symbols, and related graph edges near the working set",
                    "items": contracts[:limit],
                }
            )
        impact_items: list[dict[str, Any]] = []
        for key in ("public_surface", "callers", "docs"):
            for item in list(impact.get(key) or [])[:limit]:
                impact_item = dict(item)
                impact_item["impact_kind"] = key
                impact_items.append(impact_item)
        for note in list(impact.get("notes") or [])[:limit]:
            impact_items.append({"impact_kind": "note", "reason": note})
        if impact_items:
            chain.append(
                {
                    "phase": "check-impact",
                    "description": "callers, public surfaces, docs, and unknowns present in the retrieved subgraph",
                    "items": impact_items[:limit],
                }
            )
        return chain

    def _should_render_code_context(self, subgraph: MemorySubgraph, *, max_items: int) -> bool:
        if self._is_explicit_code_context(subgraph.query.node_types):
            return True
        ranked = [item for item in subgraph.ranked_nodes if self._is_code_context_node(item.node)]
        if not ranked:
            return False
        rows = self._code_working_set_rows(ranked, list(subgraph.nodes), query_text=subgraph.query.text, max_items=max_items)
        if not rows:
            return False
        if all(self._is_test_context_path(str(row.get("path") or "")) for row in rows) and self._has_direct_general_evidence(subgraph):
            return False
        return True

    def _has_direct_general_evidence(self, subgraph: MemorySubgraph) -> bool:
        query_tokens = set(_expanded_tokens(subgraph.query.text))
        if not query_tokens:
            return False
        nodes: OrderedDict[str, MemoryNode] = OrderedDict((item.node.id, item.node) for item in subgraph.ranked_nodes)
        for node in subgraph.nodes:
            nodes.setdefault(node.id, node)
        for node in nodes.values():
            if node.type in TECHNICAL_NODE_TYPES or self._is_code_context_node(node):
                continue
            searchable = " ".join(part for part in (node.label, node.text, node.canonical_key) if part)
            if query_tokens & set(_expanded_tokens(searchable)):
                return True
        return False

    def _code_working_set_rows(
        self,
        ranked: list[RankedNode],
        nodes: list[MemoryNode],
        *,
        query_text: str,
        max_items: int,
    ) -> list[dict[str, Any]]:
        rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        query_tokens = _code_context_query_tokens(query_text)
        raw_query_tokens = set(tokenize(query_text))
        cleanup_query = bool(raw_query_tokens & {"cleanup", "dead", "delete", "remove", "removal", "unused"})

        def add(node: MemoryNode, score: float, *, edit_candidate: bool = False, reason: str | None = None) -> None:
            path = self._node_relative_path(node)
            if not path:
                return
            row = rows.setdefault(
                path,
                {
                    "path": path,
                    "score": 0.0,
                    "edit_candidate": False,
                    "symbols": [],
                    "reasons": [],
                    "node_ids": [],
                    "line_start": None,
                    "line_end": None,
                },
            )
            row["score"] = max(float(row["score"]), score)
            if node.id not in row["node_ids"]:
                row["node_ids"].append(node.id)
            line_start, line_end = self._line_span(node)
            if line_start is not None and line_end is not None and line_end - line_start > 160:
                line_start = None
                line_end = None
            if line_start is not None:
                row["line_start"] = line_start if row["line_start"] is None else min(int(row["line_start"]), line_start)
            if line_end is not None:
                row["line_end"] = line_end if row["line_end"] is None else max(int(row["line_end"]), line_end)
            if node.type == "StaticAnalysisFinding" or node.type in {"Function", "Class", "Method", "Module"}:
                label = str(node.properties.get("qualified_name") or node.properties.get("symbol_name") or node.properties.get("name") or node.label or "").strip()
                if label and label not in row["symbols"]:
                    row["symbols"].append(label)
            if edit_candidate:
                row["edit_candidate"] = True
                if reason and reason not in row["reasons"]:
                    row["reasons"].append(reason)

        for item in ranked:
            direct = float(item.reasons.get("match_score", 0.0) or 0.0)
            if direct >= 0.04 or (item.node.type == "StaticAnalysisFinding" and direct > 0.0):
                overlap = self._owner_query_overlap(item.node, query_tokens)
                if overlap <= 0 and not (item.node.type == "StaticAnalysisFinding" and cleanup_query):
                    continue
                actionable_overlap = self._is_actionable_owner_overlap(overlap, query_tokens)
                edit_candidate = item.node.type == "StaticAnalysisFinding" or (
                    item.node.type in {"Module", "Function", "Class", "Interface", "Method", "Endpoint", "Schema", "Config", "Test"}
                    and actionable_overlap
                    and (direct >= 0.04 or overlap >= 2)
                )
                if not edit_candidate and self._is_application_surface_node(item.node) and actionable_overlap:
                    edit_candidate = True
                reason = "finding" if item.node.type == "StaticAnalysisFinding" else ("symbol/query overlap" if overlap else "direct match")
                add(item.node, item.score, edit_candidate=edit_candidate, reason=reason)
        for node in nodes:
            path = self._node_relative_path(node)
            if not self._is_code_context_node(node) or not path:
                continue
            secondary = self._is_secondary_code_path(path)
            if path in rows:
                overlap = self._owner_query_overlap(node, query_tokens)
                linked_owner = self._is_owner_symbol_node(node) and not secondary and self._is_actionable_owner_overlap(overlap, query_tokens)
                if not linked_owner:
                    continue
                add(node, 0.25, edit_candidate=linked_owner, reason="linked owner symbol" if linked_owner else None)
                continue
            overlap = self._owner_query_overlap(node, query_tokens)
            if (
                self._is_owner_symbol_node(node)
                and self._is_actionable_owner_overlap(overlap, query_tokens)
                and (not secondary or self._query_requests_secondary_code_context(query_text))
            ):
                add(node, 0.30 + min(0.12, overlap * 0.03), edit_candidate=not secondary, reason="owner/query overlap")

        ordered = sorted(rows.values(), key=lambda row: (bool(row["edit_candidate"]), float(row["score"])), reverse=True)
        primary_rows = [row for row in ordered if row["edit_candidate"] and not self._is_secondary_code_path(str(row["path"]))]
        if primary_rows and not self._query_requests_secondary_code_context(query_text):
            primary_paths = {str(row["path"]) for row in primary_rows}
            ordered = [
                row
                for row in ordered
                if str(row["path"]) in primary_paths
                or (bool(row["edit_candidate"]) and self._is_application_surface_path(str(row["path"])))
            ]
        return ordered[: max(max_items, 8)]

    def _code_owner_candidates(
        self,
        ranked: list[RankedNode],
        subgraph: MemorySubgraph,
        working_paths: set[str],
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        ranked_by_id = {item.node.id: item for item in ranked}
        ordered_nodes: list[MemoryNode] = [item.node for item in ranked]
        for node in subgraph.nodes:
            if node.id not in ranked_by_id:
                ordered_nodes.append(node)
        for node in ordered_nodes:
            if node.id in seen or not self._is_owner_symbol_node(node):
                continue
            path = self._node_relative_path(node)
            if not path or path not in working_paths or self._is_generated_context_path(path):
                continue
            item = ranked_by_id.get(node.id)
            reasons = dict(item.reasons) if item is not None else {}
            base_score = float(item.score) if item is not None else 0.25
            direct = float(reasons.get("match_score", 0.0) or 0.0)
            priority = base_score + direct
            line_start, line_end = self._line_span(node)
            if self._is_secondary_code_path(path):
                priority -= 0.2
            seen.add(node.id)
            candidates.append(
                {
                    "id": node.id,
                    "type": node.type,
                    "name": str(
                        node.properties.get("qualified_name")
                        or node.properties.get("symbol_name")
                        or node.properties.get("name")
                        or node.label
                        or node.id
                    ),
                    "path": path,
                    "location": self._location_summary(node),
                    "line_start": line_start,
                    "line_end": line_end,
                    "score": round(priority, 4),
                    "reason": "direct query match" if direct >= 0.04 else "linked owner symbol",
                }
            )
        candidates.sort(key=lambda item: (not self._is_secondary_code_path(str(item.get("path") or "")), float(item["score"])), reverse=True)
        return candidates[: min(max_items, 3)]

    @classmethod
    def _owner_query_overlap(cls, node: MemoryNode, query_tokens: set[str]) -> int:
        if not query_tokens:
            return 0
        node_tokens = set(_expanded_tokens(cls._node_search_text(node)))
        return len(query_tokens & node_tokens)

    @staticmethod
    def _is_actionable_owner_overlap(overlap: int, query_tokens: set[str]) -> bool:
        if overlap <= 0:
            return False
        if len(query_tokens) >= 4:
            return overlap >= 2
        return True

    @staticmethod
    def _is_secondary_code_path(path: str) -> bool:
        normalized = path.replace("\\", "/").casefold()
        return normalized.startswith("tests/") or normalized.startswith("docs/") or normalized == "readme.md" or "/docs/" in normalized

    def _is_application_surface_node(self, node: MemoryNode) -> bool:
        path = self._node_relative_path(node) or ""
        return bool(path and self._is_application_surface_path(path))

    @staticmethod
    def _query_requests_secondary_code_context(query_text: str) -> bool:
        tokens = set(tokenize(query_text))
        return bool(tokens & {"test", "tests", "testing", "unittest", "pytest", "spec", "docs", "doc", "documentation", "readme"})

    @staticmethod
    def _normalize_query_context_mode(query_mode: str) -> str:
        normalized = str(query_mode or "informative").strip().casefold()
        if normalized not in QUERY_CONTEXT_MODES:
            valid = ", ".join(sorted(QUERY_CONTEXT_MODES))
            raise ValueError(f"unknown query_context mode '{query_mode}'. Choose from: {valid}")
        return normalized

    @staticmethod
    def _normalize_query_context_scopes(query_scopes: Sequence[str] | None) -> set[str]:
        scopes: set[str] = set()
        for scope in query_scopes or ():
            normalized = str(scope or "").strip().casefold()
            if not normalized:
                continue
            if normalized not in QUERY_CONTEXT_SCOPES:
                valid = ", ".join(sorted(QUERY_CONTEXT_SCOPES))
                raise ValueError(f"unknown query_context scope '{scope}'. Choose from: {valid}")
            scopes.add(normalized)
        return scopes

    def _filter_query_context_subgraph(self, subgraph: MemorySubgraph, scopes: set[str]) -> MemorySubgraph:
        if not scopes:
            return subgraph
        node_by_id: OrderedDict[str, MemoryNode] = OrderedDict()
        ranked: list[RankedNode] = []
        for item in subgraph.ranked_nodes:
            if self._node_matches_query_context_scope(item.node, scopes):
                ranked.append(item)
                node_by_id[item.node.id] = item.node
        for node in subgraph.nodes:
            if self._node_matches_query_context_scope(node, scopes):
                node_by_id.setdefault(node.id, node)
        included_ids = set(node_by_id)
        edges = [edge for edge in subgraph.edges if edge.from_id in included_ids and edge.to_id in included_ids]
        return MemorySubgraph(
            query=subgraph.query,
            ranked_nodes=ranked,
            nodes=list(node_by_id.values()),
            edges=edges,
            seed_node_ids=[node_id for node_id in subgraph.seed_node_ids if node_id in included_ids],
            trace_id=subgraph.trace_id,
        )

    def _node_matches_query_context_scope(self, node: MemoryNode, scopes: set[str]) -> bool:
        explicit = str(node.properties.get("context_scope") or "").strip().casefold()
        return explicit in scopes

    @staticmethod
    def _code_context_needs_followups(
        *,
        query_mode: str,
        path_rows: list[dict[str, Any]],
        cleanup_candidates: list[dict[str, Any]],
        targeted_reads: list[dict[str, Any]],
        snippets: list[dict[str, Any]],
    ) -> bool:
        if not path_rows:
            return True
        if query_mode == "cleanup" and not cleanup_candidates:
            return True
        return bool(targeted_reads and not snippets)

    def _code_working_set_payload(
        self,
        path_rows: list[dict[str, Any]],
        *,
        query_mode: str,
        max_items: int,
    ) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for row in path_rows[: min(max_items, QUERY_CONTEXT_MAX_RENDERED_FILES)]:
            line_start = row.get("line_start")
            line_end = row.get("line_end")
            if line_start is not None and line_end is not None and int(line_end) - int(line_start) > 160:
                line_start = None
                line_end = None
            role = "read"
            if query_mode == "informative":
                role = "read"
            elif query_mode == "cleanup" and row["edit_candidate"]:
                role = "cleanup"
            payload.append(
                {
                    "path": row["path"],
                    "role": role,
                    "score": round(float(row["score"]), 4),
                    "symbols": list(row["symbols"][:6]),
                    "reason": ", ".join(row.get("reasons") or ["graph match"]),
                    "line_start": line_start,
                    "line_end": line_end,
                    "node_ids": list(row.get("node_ids", [])[:6]),
                }
            )
        return payload

    def _code_contract_payload(
        self,
        ranked: list[RankedNode],
        subgraph: MemorySubgraph,
        working_paths: set[str],
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        candidates: OrderedDict[str, MemoryNode] = OrderedDict()
        for item in ranked:
            candidates[item.node.id] = item.node
        for node in subgraph.nodes:
            path = self._node_relative_path(node)
            if path in working_paths and node.type in {"Module", "Function", "Class", "Interface", "Method", "Endpoint", "Schema", "Config", "Import", "Dependency"}:
                candidates.setdefault(node.id, node)

        node_by_id = {node.id: node for node in subgraph.nodes}
        node_by_id.update({item.node.id: item.node for item in ranked})
        contracts: list[dict[str, Any]] = []
        for node in candidates.values():
            if node.type not in {"Module", "Function", "Class", "Interface", "Method", "Endpoint", "Schema", "Config", "Import", "Dependency", "StaticAnalysisFinding"}:
                continue
            path = self._node_relative_path(node)
            if working_paths and path not in working_paths and node.type != "StaticAnalysisFinding":
                continue
            name = str(
                node.properties.get("qualified_name")
                or node.properties.get("symbol_name")
                or node.properties.get("name")
                or node.label
                or node.id
            )
            related: list[str] = []
            for edge in subgraph.edges:
                if edge.type not in {"CALLS", "IMPORTS_FROM", "REFERENCES", "DEPENDS_ON", "INSTANTIATES", "METHOD", "DEFINES"}:
                    continue
                other_id: str | None = None
                if edge.from_id == node.id:
                    other_id = edge.to_id
                elif edge.to_id == node.id:
                    other_id = edge.from_id
                if other_id is None:
                    continue
                other = node_by_id.get(other_id)
                label = self._compact_text(self._node_label(other) if other else other_id, max_chars=80)
                ref = f"{edge.type}:{label}"
                if ref not in related:
                    related.append(ref)
                if len(related) >= 3:
                    break
            contracts.append(
                {
                    "id": node.id,
                    "type": node.type,
                    "name": name,
                    "path": path,
                    "location": self._location_summary(node),
                    "preserve": "public/imported API surface" if node.type != "StaticAnalysisFinding" else "finding provenance",
                    "related": related,
                }
            )
            if len(contracts) >= min(max_items, 4):
                break
        return contracts

    def _code_impact_payload(
        self,
        subgraph: MemorySubgraph,
        owner_candidates: list[dict[str, Any]],
        working_paths: set[str],
        *,
        max_items: int,
    ) -> dict[str, Any]:
        nodes: OrderedDict[str, MemoryNode] = OrderedDict((node.id, node) for node in subgraph.nodes)
        for item in subgraph.ranked_nodes:
            nodes.setdefault(item.node.id, item.node)
        target_ids = {str(item.get("id")) for item in owner_candidates if item.get("id")}
        callers: list[dict[str, Any]] = []
        public_surface: list[dict[str, Any]] = []
        docs: list[dict[str, Any]] = []
        seen_callers: set[str] = set()
        seen_surface: set[str] = set()
        seen_docs: set[str] = set()
        for edge in subgraph.edges:
            if edge.type in CALLER_EDGE_TYPES and edge.to_id in target_ids and edge.from_id not in seen_callers:
                caller = nodes.get(edge.from_id)
                target = nodes.get(edge.to_id)
                if caller is not None and target is not None:
                    seen_callers.add(edge.from_id)
                    callers.append(
                        {
                            "caller": self._compact_node_ref(caller),
                            "target": self._compact_node_ref(target),
                            "edge_id": edge.id,
                            "edge_type": edge.type,
                            "reason": f"incoming {edge.type}",
                        }
                    )
            if edge.type in PUBLIC_SURFACE_EDGE_TYPES and (edge.from_id in target_ids or edge.to_id in target_ids):
                surface_id = edge.from_id if edge.from_id not in target_ids else edge.to_id
                surface = nodes.get(surface_id)
                if surface is not None and surface.id not in seen_surface:
                    seen_surface.add(surface.id)
                    public_surface.append(
                        {
                            "surface": self._compact_node_ref(surface),
                            "edge_id": edge.id,
                            "edge_type": edge.type,
                            "reason": f"{edge.type} near target",
                        }
                    )
        for node in nodes.values():
            path = self._node_relative_path(node)
            if not path or path in working_paths or path in seen_docs or not self._is_docs_mention_node(node):
                continue
            seen_docs.add(path)
            docs.append({"path": path, "node_id": node.id, "location": self._location_summary(node), "reason": "documentation mention in retrieved context"})
            if len(docs) >= min(max_items, 3):
                break
        for node_id in target_ids:
            node = nodes.get(node_id)
            if node is not None and self._is_public_surface_node(node) and node.id not in seen_surface:
                seen_surface.add(node.id)
                public_surface.append({"surface": self._compact_node_ref(node), "edge_id": None, "edge_type": None, "reason": "target is public API-shaped"})
        notes: list[str] = []
        if target_ids and not callers:
            notes.append("No static CALLS/INSTANTIATES caller was present in the retrieved subgraph; treat dynamic or public entry points as unknown until verified.")
        return {
            "callers": callers[: min(max_items, 4)],
            "public_surface": public_surface[: min(max_items, 4)],
            "docs": docs,
            "notes": notes,
        }

    def _compact_node_ref(self, node: MemoryNode) -> dict[str, Any]:
        return {
            "id": node.id,
            "type": node.type,
            "label": self._compact_text(self._node_label(node), max_chars=120),
            "path": self._node_relative_path(node),
            "location": self._location_summary(node),
        }

    def _code_targeted_reads(
        self,
        ranked: list[RankedNode],
        subgraph: MemorySubgraph,
        working_paths: set[str],
        *,
        query_text: str,
        max_items: int,
    ) -> list[dict[str, Any]]:
        if not working_paths:
            return []
        reads: OrderedDict[tuple[str, int | None, int | None, str], dict[str, Any]] = OrderedDict()
        primary_owner_available = any(not self._is_secondary_code_path(path) for path in working_paths)
        include_secondary = self._query_requests_secondary_code_context(query_text)

        def add(node: MemoryNode, reason: str) -> None:
            path = self._node_relative_path(node)
            if not path:
                return
            if self._is_generated_context_path(path):
                return
            if primary_owner_available and not include_secondary and self._is_secondary_code_path(path):
                return
            if working_paths and path not in working_paths and node.type not in SOURCE_NODE_TYPES:
                return
            line_start, line_end = self._line_span(node)
            if line_start is None:
                return
            if line_end is not None and line_end - line_start > 160:
                return
            key = (path, line_start, line_end, node.id)
            reads.setdefault(
                key,
                {
                    "path": path,
                    "source_path": node.properties.get("path") or node.properties.get("source_path"),
                    "line_start": line_start,
                    "line_end": line_end,
                    "node_id": node.id,
                    "type": node.type,
                    "label": self._compact_text(self._node_label(node), max_chars=100),
                    "reason": reason,
                },
            )

        for item in ranked:
            add(item.node, "owner symbol")
        for node in subgraph.nodes:
            if node.type in SOURCE_NODE_TYPES:
                add(node, "linked source evidence")
            elif self._is_code_context_node(node):
                add(node, "related code context")
            if len(reads) >= min(max_items, 10):
                break
        if working_paths and len(working_paths) <= 3 and len(reads) < min(max_items, 10):
            for node in self._matching_source_fragments_for_query(query_text, working_paths, max_items=max_items):
                add(node, "exact source phrase")
                if len(reads) >= min(max_items, 10):
                    break
        return list(reads.values())[: min(max_items, 10)]

    def _matching_source_fragments_for_query(self, query_text: str, working_paths: set[str], *, max_items: int) -> list[MemoryNode]:
        matches: list[tuple[float, MemoryNode]] = []
        for node in self._nodes_for_types(SOURCE_NODE_TYPES):
            if node.type not in SOURCE_NODE_TYPES:
                continue
            path = self._node_relative_path(node)
            if not path or path not in working_paths or self._is_generated_context_path(path):
                continue
            if not self._node_matches_query_context_scope(node, {"code"}):
                continue
            line_start, line_end = self._line_span(node)
            if line_start is None:
                continue
            if line_end is not None and line_end - line_start > 80:
                continue
            if not self._source_fragment_is_strong_query_match(node, query_text):
                continue
            metrics = self._node_match_metrics(node, self._query_profile(query_text))
            matches.append((metrics.match_score, node))
        matches.sort(key=lambda item: (item[0], item[1].salience, self._location_summary(item[1]) or ""), reverse=True)
        return [node for _, node in matches[: min(max_items, 5)]]

    def _code_informative_snippet_payload(
        self,
        targeted_reads: list[dict[str, Any]],
        subgraph: MemorySubgraph,
        *,
        path_rows: list[dict[str, Any]],
        max_items: int,
    ) -> list[dict[str, Any]]:
        if not targeted_reads or len(path_rows) > 3:
            return []
        nodes = {item.node.id: item.node for item in subgraph.ranked_nodes}
        nodes.update({node.id: node for node in subgraph.nodes})
        selected: list[dict[str, Any]] = []
        for read in targeted_reads:
            if read.get("type") not in SOURCE_NODE_TYPES:
                continue
            node_id = str(read.get("node_id") or "")
            node = nodes.get(node_id) or self.store.get_node(node_id)
            if node is None:
                continue
            if not self._source_fragment_is_strong_query_match(node, subgraph.query.text):
                continue
            selected.append(read)
            if len(selected) >= min(max_items, 2):
                break
        return self._code_snippet_payload(selected, subgraph, max_items=min(max_items, 2)) if selected else []

    def _source_fragment_is_strong_query_match(self, node: MemoryNode, query_text: str) -> bool:
        text = str(node.text or node.label or "")
        if not text:
            return False
        profile = self._query_profile(query_text)
        metrics = self._node_match_metrics(node, profile)
        if metrics.match_score >= 0.50:
            return True
        fragment_key = canonicalize(text)
        if not fragment_key:
            return False
        query_phrases = self._significant_query_phrases(query_text)
        return any(phrase in fragment_key for phrase in query_phrases)


    def _code_snippet_payload(
        self,
        targeted_reads: list[dict[str, Any]],
        subgraph: MemorySubgraph,
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        nodes = {item.node.id: item.node for item in subgraph.ranked_nodes}
        nodes.update({node.id: node for node in subgraph.nodes})
        path_index = self._absolute_path_index(list(nodes.values()))
        snippets: list[dict[str, Any]] = []
        seen: set[tuple[str, int | None, int | None]] = set()
        primary_types = {"Function", "Method", "Class", "Interface", "Module", "Endpoint", "Schema", "StaticAnalysisFinding"}
        ordered_reads = sorted(
            targeted_reads,
            key=lambda item: (
                self._cleanup_read_kind_order(str(item.get("read_kind") or "")),
                0 if item.get("type") in primary_types and item.get("reason") == "owner symbol" else 1,
                1 if item.get("type") in SOURCE_NODE_TYPES else 0,
            ),
        )
        for item in ordered_reads:
            path = str(item.get("path") or "")
            line_start = item.get("line_start")
            line_end = item.get("line_end")
            if not path or line_start is None:
                continue
            try:
                start = int(line_start)
                end = int(line_end) if line_end is not None else start
            except (TypeError, ValueError):
                continue
            key = (path, start, end)
            if key in seen:
                continue
            seen.add(key)
            read_path_index = path_index
            raw_source_path = item.get("source_path")
            if raw_source_path:
                candidate_path = Path(str(raw_source_path))
                if candidate_path.is_absolute():
                    read_path_index = {**path_index, path: candidate_path}
            text = self._read_source_span(path, start, end, path_index=read_path_index)
            source = "disk"
            if not text:
                node = nodes.get(str(item.get("node_id") or ""))
                text = node.text if node and node.text else ""
                source = "graph"
            text = self._bounded_multiline_text(text, max_lines=16, max_chars=900)
            if not text:
                continue
            snippets.append(
                {
                    "path": path,
                    "line_start": start,
                    "line_end": end,
                    "node_id": item.get("node_id"),
                    "type": item.get("type"),
                    "label": item.get("label"),
                    "source": source,
                    "text": text,
                }
            )
            if len(snippets) >= min(max_items, 3):
                break
        return snippets

    def _absolute_path_index(self, nodes: Sequence[MemoryNode]) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for node in nodes:
            relative_path = self._node_relative_path(node)
            raw_path = node.properties.get("path") or node.properties.get("source_path")
            if not relative_path or raw_path is None:
                continue
            candidate = Path(str(raw_path))
            if candidate.is_absolute():
                paths.setdefault(relative_path, candidate)
        return paths

    def _read_source_span(
        self,
        path: str,
        line_start: int,
        line_end: int,
        *,
        path_index: dict[str, Path],
    ) -> str:
        if line_start <= 0 or line_end < line_start:
            return ""
        candidate = path_index.get(path, Path(path))
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        if line_start > len(lines):
            return ""
        return "\n".join(lines[line_start - 1 : min(line_end, len(lines))])

    @staticmethod
    def _bounded_multiline_text(text: str, *, max_lines: int, max_chars: int) -> str:
        source_lines = str(text).splitlines()
        rendered = "\n".join(source_lines[:max_lines]).strip()
        if not rendered:
            return ""
        if len(source_lines) > max_lines:
            rendered = rendered.rstrip() + "\n..."
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 3].rstrip() + "..."
        return rendered

    def _code_test_targets(self, subgraph: MemorySubgraph, path_rows: list[dict[str, Any]], *, query_text: str, max_items: int) -> list[dict[str, Any]]:
        ranked_by_id = {item.node.id: item for item in subgraph.ranked_nodes}
        candidate_nodes: OrderedDict[str, MemoryNode] = OrderedDict(
            (node.id, node) for node in [*subgraph.nodes, *(item.node for item in subgraph.ranked_nodes)]
        )
        if not any(self._is_test_context_path(self._node_relative_path(node) or "") for node in candidate_nodes.values()):
            test_query = MemoryQuery(
                text=query_text,
                top_k=max(8, min(max_items * 2, 20)),
                max_depth=0,
                include_archived=subgraph.query.include_archived,
                context_scopes={"test"},
                store_trace=False,
            )
            test_matches = self._scoped_lexical_search(
                test_query,
                self._query_profile(query_text),
                lexical_node_types=tuple(sorted(CODE_CONTEXT_NODE_TYPES)),
                scopes={"test"},
                top_k=test_query.top_k,
            )
            for node, score in test_matches:
                ranked_by_id[node.id] = RankedNode(node=node, score=score, reasons={"match_score": score})
                candidate_nodes.setdefault(node.id, node)
        query_tokens = set(_expanded_tokens(query_text))
        working_node_ids = {
            str(node_id)
            for row in path_rows
            for node_id in list(row.get("node_ids") or [])
        }
        linked_test_ids: set[str] = set()
        for edge in subgraph.edges:
            if edge.type != "TESTS":
                continue
            if edge.from_id in working_node_ids:
                linked_test_ids.add(edge.to_id)
            if edge.to_id in working_node_ids:
                linked_test_ids.add(edge.from_id)
        targets: dict[str, dict[str, Any]] = {}
        for node in candidate_nodes.values():
            path = self._node_relative_path(node)
            if not path:
                continue
            normalized = path.replace("\\", "/")
            if normalized.startswith("tests/"):
                kind = "test"
            elif normalized.startswith("docs/") or normalized == "README.md":
                kind = "docs"
            else:
                continue
            overlap = self._owner_query_overlap(node, query_tokens)
            ranked = ranked_by_id.get(node.id)
            direct = float(ranked.reasons.get("match_score", 0.0) or 0.0) if ranked is not None else 0.0
            if kind == "test" and overlap <= 0 and direct <= 0.0 and node.id not in linked_test_ids:
                continue
            score = (1.0 if kind == "test" else 0.25) + direct + min(0.3, overlap * 0.05)
            is_owner_symbol = node.type in {"Function", "Method", "Class", "Test"}
            if is_owner_symbol:
                score += 0.1
            line_start, line_end = self._line_span(node)
            span_size = (
                max(0, int(line_end) - int(line_start))
                if line_start is not None and line_end is not None
                else 1_000_000
            )
            priority = (is_owner_symbol, score, -span_size)
            previous = targets.get(normalized)
            if previous is None or priority > tuple(previous["_priority"]):
                symbol = str(
                    node.properties.get("qualified_name")
                    or node.properties.get("symbol_name")
                    or node.properties.get("name")
                    or node.label
                    or ""
                ).strip()
                targets[normalized] = {
                    "kind": kind,
                    "path": normalized,
                    "score": round(score, 4),
                    "reason": "test graph match" if kind == "test" else "documentation mention",
                    "line_start": line_start,
                    "line_end": line_end,
                    "symbols": [symbol] if symbol and is_owner_symbol else [],
                    "_priority": priority,
                }
        ordered = sorted(targets.values(), key=lambda item: (item["kind"] == "test", float(item["score"])), reverse=True)
        for item in ordered:
            item.pop("_priority", None)
        test_rows = [item for item in ordered if item["kind"] == "test"]
        doc_rows = [item for item in ordered if item["kind"] == "docs"]
        selected = test_rows[: min(max_items, 4)]
        if not selected and self._query_requests_secondary_code_context(query_text):
            selected.extend(doc_rows[: min(max_items, 2)])
        return selected

    def _code_follow_up_payload(self, subgraph: MemorySubgraph, path_rows: list[dict[str, Any]], *, max_items: int) -> list[dict[str, str]]:
        query = self._reql_string(subgraph.query.text)
        retrieve_label = "Retrieve ranked rows" if path_rows else "Retrieve source rows"
        graph_label = "Expand code graph" if path_rows else "Expand graph context"
        followups = [
            {
                "label": retrieve_label,
                "command": f"reql query {self._shell_string(f'RETRIEVE {query} LIMIT {min(max_items, 8)} RETURN id,type,text,score,relative_path,line_start')}",
                "purpose": "ranked source/location rows",
            },
            {
                "label": graph_label,
                "command": f"reql query_graph --query {query} --max-depth {subgraph.query.max_depth} --json",
                "purpose": "expanded code graph context",
            },
            {
                "label": "Cleanup findings",
                "command": f"reql query {self._shell_string('FINDINGS RETURN finding_type,cleanup_priority,symbol_name,qualified_name,relative_path,line_start,reason ORDER BY cleanup_priority LIMIT 30')}",
                "purpose": "cleanup finding rows",
            },
        ]
        if path_rows:
            path = self._reql_string(str(path_rows[0]["path"]))
            followups.append(
                {
                    "label": "Symbols in first file",
                    "command": f"reql query {self._shell_string(f'SYMBOLS WHERE relative_path = {path} RETURN type,name,qualified_name,start_line,end_line LIMIT 50')}",
                    "purpose": "owner symbols in the first working-set file",
                }
            )
            followups.append(
                {
                    "label": "Findings in first file",
                    "command": f"reql query {self._shell_string(f'FINDINGS WHERE relative_path = {path} RETURN finding_type,cleanup_priority,symbol_name,line_start,reason ORDER BY cleanup_priority LIMIT 30')}",
                    "purpose": "static-analysis findings in the first working-set file",
                }
            )
        ids = [item.node.id for item in subgraph.ranked_nodes[: min(3, max_items)] if self._is_code_context_node(item.node)]
        if ids:
            followups.append(
                {
                    "label": "Inspect top node",
                    "command": f"reql inspect --node-id {ids[0]} --json",
                    "purpose": "top node provenance and immediate neighbors",
                }
            )
        return followups

    def _code_edit_plan_lines(
        self,
        path_rows: list[dict[str, Any]],
        ranked: list[RankedNode],
        subgraph: MemorySubgraph,
        *,
        max_items: int,
    ) -> list[str]:
        if not path_rows:
            return []
        lines: list[str] = [
            "- Existing graph-node context is available for implementation planning.",
        ]
        candidates = [row for row in path_rows if row["edit_candidate"]] or path_rows[: min(3, len(path_rows))]
        for row in candidates[: min(max_items, 4)]:
            symbols = ", ".join(row["symbols"][:3]) if row["symbols"] else "inspect file-level owner"
            reasons = ", ".join(row.get("reasons") or ["graph match"])
            lines.append(f"- Primary candidate: `{row['path']}` ({symbols}; {reasons}; score={float(row['score']):.2f})")
        owner_ids = [
            item.node.id
            for item in ranked
            if item.node.type in {"Module", "Function", "Class", "Interface", "Method", "Endpoint", "Schema", "StaticAnalysisFinding"}
        ][:3]
        if owner_ids:
            joined = ", ".join(owner_ids)
            lines.append(f"- Owner/provenance nodes: {joined}")
        source_edges = [
            edge
            for edge in subgraph.edges
            if edge.type in SOURCE_EDGE_TYPES and (edge.from_id in owner_ids or edge.to_id in owner_ids)
        ]
        if source_edges:
            lines.append("- Linked `SourceFragment` evidence exists for relevant line ranges.")
        lines.append("- Candidate alignment depends on the query terms and retrieved graph evidence.")
        return lines

    def _code_edge_lines(self, subgraph: MemorySubgraph, *, max_items: int) -> list[str]:
        nodes: dict[str, MemoryNode] = {item.node.id: item.node for item in subgraph.ranked_nodes}
        nodes.update({node.id: node for node in subgraph.nodes if self._is_code_context_node(node)})
        lines: list[str] = []
        seen: set[str] = set()
        for edge in subgraph.edges:
            if edge.id in seen or edge.type not in CODE_CONTEXT_EDGE_TYPES:
                continue
            left = nodes.get(edge.from_id)
            right = nodes.get(edge.to_id)
            if left is None or right is None:
                continue
            seen.add(edge.id)
            left_label = self._compact_text(self._node_label(left), max_chars=80)
            right_label = self._compact_text(self._node_label(right), max_chars=80)
            lines.append(f"- `{edge.id}` {left_label} --{edge.type}--> {right_label}")
            if len(lines) >= min(max_items, 8):
                break
        return lines

    def _code_follow_up_lines(self, subgraph: MemorySubgraph, path_rows: list[dict[str, Any]], *, max_items: int) -> list[str]:
        return self._render_followups(self._code_follow_up_payload(subgraph, path_rows, max_items=max_items))
