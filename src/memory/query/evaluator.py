"""Execution engine for REQL statements."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, is_dataclass
import re
from typing import Any, Iterable, Sequence

from ..domain.constants import ACTIVE_STATUSES
from ..domain.models import ActivationOptions, MemoryEdge, MemoryNode, MemoryQuery
from ..extraction.normalization import canonicalize, clamp
from .ast import (
    Activate,
    BooleanCondition,
    CacheStatus,
    Communities,
    Comparison,
    Condition,
    Explain,
    FindEdges,
    FindNodes,
    Hubs,
    Match,
    NodeSelector,
    NotCondition,
    PathQuery,
    Retrieve,
    Search,
    SortSpec,
    Stats,
    Statement,
    TypedNodeList,
    VerifyFinding,
)
from .errors import REQLEvaluationError
from .parser import parse_reql
from .result import QueryResult


DEFAULT_NODE_COLUMNS = ["id", "type", "label", "text", "status", "salience", "confidence", "activation"]
DEFAULT_EDGE_COLUMNS = ["id", "type", "from_id", "to_id", "weight", "confidence", "polarity", "origin"]
DEFAULT_SEARCH_COLUMNS = ["rank", "score", "id", "type", "label", "text", "salience", "confidence", "activation"]
DEFAULT_RETRIEVE_COLUMNS = ["rank", "score", "id", "type", "label", "text", "source_for", "relation", "direction", "relative_path", "line_start", "line_end"]
DEFAULT_MATCH_COLUMNS = ["a.id", "a.type", "a.label", "r.type", "r.weight", "b.id", "b.type", "b.label"]
DEFAULT_ACTIVATE_COLUMNS = ["id", "type", "label", "text", "activation", "salience", "confidence"]
DEFAULT_PATH_COLUMNS = ["length", "score", "node_ids", "node_labels", "edge_types", "edge_ids"]
DEFAULT_EXPLAIN_NODE_COLUMNS = ["node_id", "node_type", "direction", "edge_type", "weight", "confidence", "neighbor_id", "neighbor_type", "neighbor_label"]
DEFAULT_EXPLAIN_SEARCH_COLUMNS = ["rank", "score", "id", "type", "label", "text", "reasons"]
DEFAULT_VERIFY_FINDING_COLUMNS = ["finding", "minimal_snippet", "uses_found", "scopes_checked", "risks", "recommended_action"]
_VERIFY_FINDING_USAGE_EDGE_TYPES = {
    "CALLS",
    "USES",
    "REFERENCES",
    "READS",
    "RETURNS",
    "RAISES",
    "INSTANTIATES",
    "EMITS",
    "DECORATED_BY",
    "HANDLES_ROUTE",
    "TESTS",
    "CONFIGURES",
    "IMPORTS",
    "IMPORTS_FROM",
    "RE_EXPORTS",
    "DEPENDS_ON",
    "INHERITS",
    "IMPLEMENTS",
    "OVERRIDES",
}
DEFAULT_LIST_COLUMNS = {
    "PROJECTS": ["id", "name", "root_path", "status", "updated_at"],
    "ARTIFACTS": ["id", "relative_path", "path", "artifact_type", "language", "status", "size_bytes", "sha256"],
    "FRAGMENTS": ["id", "artifact_id", "fragment_type", "section_path", "start_line", "end_line", "status", "text"],
    "SYMBOLS": ["id", "type", "name", "qualified_name", "relative_path", "start_line", "end_line", "status"],
    "FINDINGS": [
        "id",
        "finding_type",
        "severity",
        "symbol_type",
        "symbol_name",
        "qualified_name",
        "relative_path",
        "directory",
        "file_count",
        "files",
        "line_start",
        "cleanup_priority",
        "cleanup_rank",
        "evidence_scope",
        "confidence",
        "reason",
        "status",
    ],
    "DELTAS": ["id", "run_id", "project_id", "artifact_id", "created_at", "added_nodes", "updated_nodes", "archived_nodes"],
}


class QueryEvaluator:
    """Executes parsed REQL against a MemoryGraph facade."""

    def __init__(self, graph: Any) -> None:
        self.graph = graph
        self.store = graph.store

    @staticmethod
    def _is_graph_layer_node(node: MemoryNode | None) -> bool:
        return node is not None

    def _is_graph_layer_edge(self, edge: MemoryEdge) -> bool:
        return self._is_graph_layer_node(self.store.get_node(edge.from_id)) and self._is_graph_layer_node(self.store.get_node(edge.to_id))

    def _count_graph_nodes(self, node_types: set[str] | None = None, statuses: set[str] | None = None) -> int:
        return sum(
            1
            for node in self.store.all_nodes()
            if self._is_graph_layer_node(node)
            and (node_types is None or node.type in node_types)
            and (statuses is None or node.status in statuses)
        )

    def _count_graph_edges(self, edge_types: set[str] | None = None) -> int:
        return sum(
            1
            for edge in self.store.all_edges()
            if (edge_types is None or edge.type in edge_types) and self._is_graph_layer_edge(edge)
        )

    def execute(self, source_or_statement: str | Statement) -> QueryResult:
        profile = getattr(self.graph, "profile_logger", None)
        if isinstance(source_or_statement, str):
            source = source_or_statement.strip()
            if profile:
                with profile.span("query.parse", statement_length=len(source)):
                    statement = parse_reql(source)
            else:
                statement = parse_reql(source)
        else:
            source = repr(source_or_statement)
            statement = source_or_statement

        statement_name = type(statement).__name__
        if profile:
            with profile.span("query.evaluate", statement_type=statement_name):
                return self._execute_statement(source, statement)
        return self._execute_statement(source, statement)

    def _execute_statement(self, source: str, statement: Statement) -> QueryResult:
        if isinstance(statement, FindNodes):
            return self._find_nodes(source, statement)
        if isinstance(statement, FindEdges):
            return self._find_edges(source, statement)
        if isinstance(statement, Search):
            return self._search(source, statement)
        if isinstance(statement, Retrieve):
            return self._retrieve(source, statement)
        if isinstance(statement, Activate):
            return self._activate(source, statement)
        if isinstance(statement, Match):
            return self._match(source, statement)
        if isinstance(statement, PathQuery):
            return self._path(source, statement)
        if isinstance(statement, Explain):
            return self._explain(source, statement)
        if isinstance(statement, Stats):
            return self._stats(source, statement)
        if isinstance(statement, Communities):
            return self._communities(source, statement)
        if isinstance(statement, Hubs):
            return self._hubs(source, statement)
        if isinstance(statement, TypedNodeList):
            return self._typed_node_list(source, statement)
        if isinstance(statement, CacheStatus):
            return self._cache_status(source, statement)
        if isinstance(statement, VerifyFinding):
            return self._verify_finding(source, statement)
        raise REQLEvaluationError(f"Unsupported statement: {statement!r}")

    def _indexed_node_candidates(
        self,
        condition: Condition | None,
        node_types: Sequence[str],
        include_archived: bool,
        *,
        limit: int,
    ) -> list[MemoryNode] | None:
        comparison = _simple_indexable_comparison(condition)
        if comparison is None:
            return None
        field = _strip_default_alias(comparison.field, "node")
        statuses = None if include_archived else sorted(ACTIVE_STATUSES)
        if field == "id":
            node = self.store.get_node(str(comparison.value))
            return [node] if node and (include_archived or node.status in ACTIVE_STATUSES) else []
        if field == "type":
            types = [str(comparison.value)]
            if node_types:
                requested = {item.casefold() for item in node_types}
                types = [item for item in types if item.casefold() in requested]
            out: list[MemoryNode] = []
            for type_ in types:
                out.extend(self.store.find_nodes(type_=type_, status=statuses, limit=limit, order_by="salience"))
            return out[:limit]
        if field == "status":
            out: list[MemoryNode] = []
            scoped_types = tuple(node_types) if node_types else (None,)
            for type_ in scoped_types:
                out.extend(self.store.find_nodes(type_=type_, status=str(comparison.value), limit=limit, order_by="updated_at"))
            return out[:limit]
        out = []
        scoped_types = tuple(node_types) if node_types else (None,)
        for type_ in scoped_types:
            out.extend(self.store.find_nodes_by_property(field, comparison.value, type_=type_, status=statuses, limit=limit))
        return out[:limit]

    def _indexed_edge_candidates(
        self,
        condition: Condition | None,
        edge_types: Sequence[str],
        *,
        limit: int,
    ) -> list[MemoryEdge] | None:
        comparison = _simple_indexable_comparison(condition)
        if comparison is None:
            return None
        field = _strip_default_alias(comparison.field, "edge")
        if field == "id":
            edge = self.store.get_edge(str(comparison.value)) if hasattr(self.store, "get_edge") else None
            return [edge] if edge else []
        if field == "type":
            types = [str(comparison.value)]
            if edge_types:
                requested = {item.casefold() for item in edge_types}
                types = [item for item in types if item.casefold() in requested]
            out: list[MemoryEdge] = []
            for type_ in types:
                out.extend(self.store.get_edges(type_=type_, limit=limit))
            return out[:limit]
        out = []
        scoped_types = tuple(edge_types) if edge_types else (None,)
        for type_ in scoped_types:
            out.extend(self.store.find_edges_by_property(field, comparison.value, type_=type_, limit=limit))
        return out[:limit]

    # ------------------------------------------------------------------
    # FIND
    # ------------------------------------------------------------------
    def _find_nodes(self, source: str, statement: FindNodes) -> QueryResult:
        allowed_types = _casefold_set(statement.node_types)
        indexed_nodes = self._indexed_node_candidates(statement.where, statement.node_types, statement.include_archived, limit=max(statement.limit * 20, 1000))
        if indexed_nodes is not None:
            row_objects: list[dict[str, Any]] = []
            for node in indexed_nodes:
                if not self._is_graph_layer_node(node):
                    continue
                if allowed_types and node.type.casefold() not in allowed_types:
                    continue
                if not statement.include_archived and node.status not in ACTIVE_STATUSES:
                    continue
                row = {"node": node}
                if statement.where and not evaluate_condition(statement.where, row, default_alias="node"):
                    continue
                row_objects.append(row)
            if statement.order_by:
                row_objects.sort(
                    key=lambda row: _sort_key(resolve_field(row, statement.order_by.field, default_alias="node")),
                    reverse=statement.order_by.descending,
                )
            else:
                row_objects.sort(key=lambda row: (row["node"].salience, row["node"].activation, row["node"].updated_at), reverse=True)
            columns = _columns(statement.returns, DEFAULT_NODE_COLUMNS)
            rows = [project_row(row, columns, default_alias="node") for row in row_objects[: statement.limit]]
            return QueryResult(source, "FIND NODES", columns, rows, {"matched": len(row_objects), "indexed": True})
        if statement.where is None and statement.order_by is None:
            node_types = set(statement.node_types) if statement.node_types else None
            statuses = None if statement.include_archived else set(ACTIVE_STATUSES)
            status_filter = None if statuses is None else sorted(statuses)
            if node_types:
                nodes = [
                    node
                    for type_ in sorted(node_types)
                    for node in self.store.find_nodes(type_=type_, status=status_filter, limit=statement.limit, order_by="salience")
                ]
                nodes.sort(key=lambda node: (node.salience, node.activation, node.updated_at), reverse=True)
                nodes = nodes[: statement.limit]
            elif statement.node_types:
                nodes = []
            else:
                nodes = self.store.find_nodes(status=status_filter, limit=max(statement.limit * 20, 1000), order_by="salience")
                nodes = [node for node in nodes if self._is_graph_layer_node(node)][: statement.limit]
            columns = _columns(statement.returns, DEFAULT_NODE_COLUMNS)
            rows = [project_row({"node": node}, columns, default_alias="node") for node in nodes]
            matched = self._count_graph_nodes(node_types=node_types, statuses=statuses)
            return QueryResult(source, "FIND NODES", columns, rows, {"matched": matched})
        nodes = self.store.all_nodes()
        rows: list[dict[str, Any]] = []
        row_objects: list[dict[str, Any]] = []
        for node in nodes:
            if not self._is_graph_layer_node(node):
                continue
            if allowed_types and node.type.casefold() not in allowed_types:
                continue
            if not statement.include_archived and node.status not in ACTIVE_STATUSES:
                continue
            row = {"node": node}
            if statement.where and not evaluate_condition(statement.where, row, default_alias="node"):
                continue
            row_objects.append(row)
        if statement.order_by:
            row_objects.sort(
                key=lambda row: _sort_key(resolve_field(row, statement.order_by.field, default_alias="node")),
                reverse=statement.order_by.descending,
            )
        else:
            row_objects.sort(key=lambda row: (row["node"].salience, row["node"].activation, row["node"].updated_at), reverse=True)
        columns = _columns(statement.returns, DEFAULT_NODE_COLUMNS)
        for row in row_objects[: statement.limit]:
            rows.append(project_row(row, columns, default_alias="node"))
        return QueryResult(source, "FIND NODES", columns, rows, {"matched": len(row_objects)})

    def _find_edges(self, source: str, statement: FindEdges) -> QueryResult:
        allowed_types = _casefold_set(statement.edge_types)
        if statement.where is None and statement.order_by is None:
            edge_types = set(statement.edge_types) if statement.edge_types else None
            row_objects = []
            for edge in self.store.get_edges(type_=sorted(edge_types) if edge_types else None, limit=max(statement.limit * 20, 1000)):
                from_node = self.store.get_node(edge.from_id)
                to_node = self.store.get_node(edge.to_id)
                if not self._is_graph_layer_node(from_node) or not self._is_graph_layer_node(to_node):
                    continue
                row_objects.append({"edge": edge, "r": edge, "from": from_node, "to": to_node})
                if len(row_objects) >= statement.limit:
                    break
            columns = _columns(statement.returns, DEFAULT_EDGE_COLUMNS)
            rows = [project_row(row, columns, default_alias="edge") for row in row_objects]
            return QueryResult(source, "FIND EDGES", columns, rows, {"matched": self._count_graph_edges(edge_types=edge_types)})
        row_objects: list[dict[str, Any]] = []
        candidate_edges = self._indexed_edge_candidates(statement.where, statement.edge_types, limit=max(statement.limit * 20, 1000))
        if candidate_edges is None:
            candidate_edges = self.store.all_edges()
        for edge in candidate_edges:
            if allowed_types and edge.type.casefold() not in allowed_types:
                continue
            from_node = self.store.get_node(edge.from_id)
            to_node = self.store.get_node(edge.to_id)
            if not self._is_graph_layer_node(from_node) or not self._is_graph_layer_node(to_node):
                continue
            row = {"edge": edge, "r": edge, "from": from_node, "to": to_node}
            if statement.where and not evaluate_condition(statement.where, row, default_alias="edge"):
                continue
            row_objects.append(row)
        if statement.order_by:
            row_objects.sort(
                key=lambda row: _sort_key(resolve_field(row, statement.order_by.field, default_alias="edge")),
                reverse=statement.order_by.descending,
            )
        else:
            row_objects.sort(key=lambda row: (row["edge"].weight, row["edge"].confidence, row["edge"].updated_at), reverse=True)
        columns = _columns(statement.returns, DEFAULT_EDGE_COLUMNS)
        rows = [project_row(row, columns, default_alias="edge") for row in row_objects[: statement.limit]]
        return QueryResult(source, "FIND EDGES", columns, rows, {"matched": len(row_objects)})

    # ------------------------------------------------------------------
    # SEARCH / ACTIVATE
    # ------------------------------------------------------------------
    def _search(self, source: str, statement: Search) -> QueryResult:
        subgraph = self.graph.retrieval.retrieve(
            MemoryQuery(
                text=statement.text,

                top_k=statement.top_k,
                max_depth=statement.max_depth,
                include_archived=statement.include_archived,
                node_types=set(statement.node_types) if statement.node_types else None,
            )
        )
        ranked_nodes = [item for item in subgraph.ranked_nodes if self._is_graph_layer_node(item.node)]
        if statement.context and statement.returns.is_default:
            context = self.graph.retrieval.compose_context(subgraph, max_items=max(8, statement.top_k))
            return QueryResult(source, "SEARCH", ["context", "trace_id", "ranked_nodes"], [{"context": context, "trace_id": subgraph.trace_id, "ranked_nodes": len(ranked_nodes)}], {"seed_node_ids": subgraph.seed_node_ids})
        columns = _columns(statement.returns, DEFAULT_SEARCH_COLUMNS)
        rows: list[dict[str, Any]] = []
        for i, ranked in enumerate(ranked_nodes, start=1):
            row = {"node": ranked.node, "score": ranked.score, "rank": i, "reasons": ranked.reasons}
            rows.append(project_row(row, columns, default_alias="node"))
        return QueryResult(source, "SEARCH", columns, rows, {"trace_id": subgraph.trace_id, "seed_node_ids": subgraph.seed_node_ids})

    def _retrieve(self, source: str, statement: Retrieve) -> QueryResult:
        memories = self.graph.retrieval.query_memories(
            MemoryQuery(
                text=statement.text,

                top_k=statement.top_k,
                max_depth=statement.max_depth,
                include_archived=statement.include_archived,
                node_types=set(statement.node_types) if statement.node_types else None,
            ),
            limit=statement.limit,
            include_sources=statement.include_sources,
            filter_generic=statement.filter_generic,
            max_text_chars=statement.max_text_chars,
        )
        columns = _columns(statement.returns, DEFAULT_RETRIEVE_COLUMNS)
        rows = []
        for rank, item in enumerate(memories, start=1):
            row = dict(item)
            row["rank"] = rank
            row.update(_location_fields(item.get("properties")))
            rows.append(project_row(row, columns))
        return QueryResult(source, "RETRIEVE", columns, rows, {"query": statement.text, "matched": len(memories)})

    def _activate(self, source: str, statement: Activate) -> QueryResult:
        result = self.graph.activation.activate(
            list(statement.node_ids),
            ActivationOptions(

                max_depth=statement.max_depth,
                min_activation=statement.min_activation,
                update_store=True,
            ),
        )
        ranked_nodes = sorted(
            [node for node in result.active_nodes if self._is_graph_layer_node(node)],
            key=lambda n: result.activation_by_node.get(n.id, n.activation),
            reverse=True,
        )
        columns = _columns(statement.returns, DEFAULT_ACTIVATE_COLUMNS)
        rows = []
        for node in ranked_nodes[: statement.limit]:
            row = {"node": node, "activation": result.activation_by_node.get(node.id, node.activation)}
            rows.append(project_row(row, columns, default_alias="node"))
        return QueryResult(source, "ACTIVATE", columns, rows, {"fired_edges": len(result.fired_edges)})

    # ------------------------------------------------------------------
    # MATCH / PATH
    # ------------------------------------------------------------------
    def _match(self, source: str, statement: Match) -> QueryResult:
        edge_types = _casefold_set(statement.edge.types)
        left_type = statement.left.type_.casefold() if statement.left.type_ else None
        right_type = statement.right.type_.casefold() if statement.right.type_ else None
        rows_objects: list[dict[str, Any]] = []
        if statement.edge.types:
            edge_iterable = self.store.get_edges(type_=sorted(statement.edge.types), limit=max(statement.limit * 50, 10000))
        else:
            edge_iterable = self.store.all_edges()
        for edge in edge_iterable:
            if edge_types and edge.type.casefold() not in edge_types:
                continue
            left = self.store.get_node(edge.from_id)
            right = self.store.get_node(edge.to_id)
            if not left or not right:
                continue
            if not self._is_graph_layer_node(left) or not self._is_graph_layer_node(right):
                continue
            candidates: list[tuple[MemoryNode, MemoryNode, MemoryEdge]] = []
            if statement.edge.direction in {"out", "both"}:
                candidates.append((left, right, edge))
            if statement.edge.direction in {"in", "both"}:
                candidates.append((right, left, edge))
            for a, b, r in candidates:
                if left_type and a.type.casefold() != left_type:
                    continue
                if right_type and b.type.casefold() != right_type:
                    continue
                row = {statement.left.alias: a, statement.edge.alias: r, statement.right.alias: b, "a": a, "r": r, "b": b}
                if statement.where and not evaluate_condition(statement.where, row, default_alias=statement.left.alias):
                    continue
                rows_objects.append(row)
        if statement.order_by:
            rows_objects.sort(
                key=lambda row: _sort_key(resolve_field(row, statement.order_by.field, default_alias=statement.left.alias)),
                reverse=statement.order_by.descending,
            )
        else:
            rows_objects.sort(key=lambda row: (row[statement.edge.alias].weight, row[statement.left.alias].salience, row[statement.right.alias].salience), reverse=True)
        columns = _columns(statement.returns, DEFAULT_MATCH_COLUMNS)
        rows = [project_row(row, columns, default_alias=statement.left.alias) for row in rows_objects[: statement.limit]]
        return QueryResult(source, "MATCH", columns, rows, {"matched": len(rows_objects), "left_alias": statement.left.alias, "edge_alias": statement.edge.alias, "right_alias": statement.right.alias})

    def _path(self, source: str, statement: PathQuery) -> QueryResult:
        start_nodes = self._resolve_selector(statement.start, limit=5)
        end_nodes = self._resolve_selector(statement.end, limit=5)
        if not start_nodes:
            raise REQLEvaluationError(f"PATH start selector did not match any node: {statement.start}")
        if not end_nodes:
            raise REQLEvaluationError(f"PATH end selector did not match any node: {statement.end}")
        end_ids = {node.id for node in end_nodes}
        allowed_edges = set(statement.edge_types) if statement.edge_types else None
        found: list[dict[str, Any]] = []
        seen_paths: set[tuple[str, ...]] = set()
        for start in start_nodes:
            queue: deque[tuple[MemoryNode, list[MemoryNode], list[MemoryEdge], float]] = deque([(start, [start], [], 0.0)])
            while queue and len(found) < statement.limit * 4:
                current, path_nodes, path_edges, score = queue.popleft()
                if len(path_edges) > statement.max_depth:
                    continue
                if current.id in end_ids and path_edges:
                    key = tuple(node.id for node in path_nodes)
                    if key not in seen_paths:
                        seen_paths.add(key)
                        found.append(_path_row(path_nodes, path_edges, score))
                    continue
                if len(path_edges) >= statement.max_depth:
                    continue
                for edge, neighbor in self.store.neighbors(current.id, direction="both", edge_types=allowed_edges, min_weight=0.0, limit=80):
                    if neighbor.status in {"deleted", "rejected"}:
                        continue
                    if not self._is_graph_layer_node(neighbor):
                        continue
                    if neighbor.id in {n.id for n in path_nodes}:
                        continue
                    edge_score = edge.weight * edge.confidence * max(edge.polarity, 0)
                    queue.append((neighbor, [*path_nodes, neighbor], [*path_edges, edge], score + edge_score))
        found.sort(key=lambda row: (row["score"], -row["length"]), reverse=True)
        columns = _columns(statement.returns, DEFAULT_PATH_COLUMNS)
        rows = [{col: row.get(col) if col in row else resolve_field(row, col) for col in columns} for row in found[: statement.limit]]
        return QueryResult(source, "PATH", columns, rows, {"start_matches": len(start_nodes), "end_matches": len(end_nodes), "candidate_paths": len(found)})

    def _resolve_selector(self, selector: NodeSelector, *, limit: int) -> list[MemoryNode]:
        if selector.mode == "id":
            node = self.store.get_node(selector.value)
            return [node] if node and self._is_graph_layer_node(node) else []
        if selector.mode == "key":
            if not selector.type_:
                return []
            node = self.store.get_node_by_key(selector.type_, canonicalize(selector.value))
            return [node] if self._is_graph_layer_node(node) else []
        if selector.mode == "text":
            subgraph = self.graph.retrieve(selector.value, top_k=limit, max_depth=1)
            return [item.node for item in subgraph.ranked_nodes if self._is_graph_layer_node(item.node)][:limit]
        return []

    # ------------------------------------------------------------------
    # EXPLAIN / STATS
    # ------------------------------------------------------------------
    def _explain(self, source: str, statement: Explain) -> QueryResult:
        if statement.mode == "hub":
            hub = self.graph.explain_hub(statement.target)
            if hub is None:
                raise REQLEvaluationError(f"Hub target not found: {statement.target}")
            columns = ["node_id", "node_type", "label", "hub_score", "hub_rank", "is_hub", "reasons"]
            row = {
                "node_id": hub.node_id,
                "node_type": hub.node_type,
                "label": hub.label,
                "hub_score": hub.hub_score,
                "hub_rank": hub.hub_rank,
                "is_hub": hub.is_hub,
                "reasons": hub.reasons,
            }
            return QueryResult(source, "EXPLAIN HUB", columns, [row])
        if statement.mode == "search":
            subgraph = self.graph.retrieve(statement.target, top_k=statement.top_k, max_depth=statement.max_depth)
            columns = DEFAULT_EXPLAIN_SEARCH_COLUMNS
            rows = []
            ranked_nodes = [item for item in subgraph.ranked_nodes if self._is_graph_layer_node(item.node)]
            for i, item in enumerate(ranked_nodes, start=1):
                rows.append(
                    {
                        "rank": i,
                        "score": item.score,
                        "id": item.node.id,
                        "type": item.node.type,
                        "label": item.node.label,
                        "text": item.node.text,
                        "reasons": item.reasons,
                    }
                )
            return QueryResult(source, "EXPLAIN SEARCH", columns, rows, {"seed_node_ids": subgraph.seed_node_ids, "trace_id": subgraph.trace_id})

        node = self.store.get_node(statement.target)
        if node is None or not self._is_graph_layer_node(node):
            raise REQLEvaluationError(f"Node not found: {statement.target}")
        rows: list[dict[str, Any]] = []
        for edge in self.store.get_edges(from_id=node.id, limit=statement.limit):
            neighbor = self.store.get_node(edge.to_id)
            if self._is_graph_layer_node(neighbor):
                rows.append(_explain_edge_row(node, edge, neighbor, "out"))
        for edge in self.store.get_edges(to_id=node.id, limit=statement.limit):
            neighbor = self.store.get_node(edge.from_id)
            if self._is_graph_layer_node(neighbor):
                rows.append(_explain_edge_row(node, edge, neighbor, "in"))
        rows.sort(key=lambda row: (row["weight"], row["confidence"]), reverse=True)
        return QueryResult(source, "EXPLAIN NODE", DEFAULT_EXPLAIN_NODE_COLUMNS, rows[: statement.limit], {"node": _node_brief(node), "degree": self.store.degree(node.id)})

    def _stats(self, source: str, statement: Stats) -> QueryResult:
        if not statement.group_by:
            node_types = set(statement.node_types) if statement.node_types else None
            rows = [{"nodes": self._count_graph_nodes(node_types=node_types), "edges": self._count_graph_edges()}]
            return QueryResult(source, "STATS", ["nodes", "edges"], rows)
        if len(statement.group_by) == 1 and statement.group_by[0] in {"type", "node.type"}:
            allowed = set(statement.node_types) if statement.node_types else None
            counts = self.store.node_type_counts()
            columns = [statement.group_by[0], "count"]
            rows = [
                {statement.group_by[0]: type_, "count": count}
                for type_, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
                if allowed is None or type_ in allowed
            ]
            return QueryResult(source, "STATS", columns, rows)
        nodes = self.store.all_nodes()
        nodes = [node for node in nodes if self._is_graph_layer_node(node)]
        if statement.node_types:
            allowed = _casefold_set(statement.node_types)
            nodes = [node for node in nodes if node.type.casefold() in allowed]
        counts: Counter[tuple[Any, ...]] = Counter()
        for node in nodes:
            row = {"node": node}
            key = tuple(resolve_field(row, field, default_alias="node") for field in statement.group_by)
            counts[key] += 1
        columns = [*statement.group_by, "count"]
        rows = []
        for key, count in counts.most_common():
            rows.append({**{statement.group_by[i]: key[i] for i in range(len(statement.group_by))}, "count": count})
        return QueryResult(source, "STATS", columns, rows)

    def _communities(self, source: str, statement: Communities) -> QueryResult:
        result = self.graph.detect_communities(limit=max(statement.limit * 5, 100))
        columns = ["id", "label", "size", "density", "salience"]
        row_objects = []
        for node in result.community_nodes:
            row = {
                "node": node,
                "id": node.id,
                "label": node.label,
                "size": node.properties.get("size"),
                "density": node.properties.get("density"),
                "salience": node.salience,
            }
            if statement.where and not evaluate_condition(statement.where, row, default_alias="node"):
                continue
            row_objects.append(row)
        row_objects = _sort_rows(row_objects, statement.order_by, "salience")
        rows = [{column: row.get(column) for column in columns} for row in row_objects[: statement.limit]]
        return QueryResult(source, "COMMUNITIES", columns, rows, {"matched": len(result.community_nodes)})

    def _hubs(self, source: str, statement: Hubs) -> QueryResult:
        report = self.graph.analyze_hubs(

            limit=max(statement.limit * 5, 100),
            node_types=set(statement.node_types) if statement.node_types else None,
        )
        columns = ["rank", "id", "type", "label", "hub_score", "specificity", "generic_penalty", "reason"]
        row_objects = []
        for hub in report.hubs:
            node = self.store.get_node(hub.node_id)
            row = {
                "node": node,
                "rank": hub.hub_rank,
                "id": hub.node_id,
                "type": hub.node_type,
                "label": hub.label,
                "hub_score": hub.hub_score,
                "specificity": hub.specificity_score,
                "specificity_score": hub.specificity_score,
                "generic_penalty": hub.generic_penalty,
                "reason": "; ".join(hub.reasons),
            }
            if statement.where and not evaluate_condition(statement.where, row, default_alias="node"):
                continue
            row_objects.append(row)
        row_objects = _sort_rows(row_objects, statement.order_by, "hub_score")
        rows = [{column: row.get(column) for column in columns} for row in row_objects[: statement.limit]]
        return QueryResult(source, "HUBS", columns, rows, {"warnings": report.warnings})

    def _typed_node_list(self, source: str, statement: TypedNodeList) -> QueryResult:
        allowed = _casefold_set(statement.node_types)
        indexed_nodes = self._indexed_node_candidates(statement.where, statement.node_types, include_archived=False, limit=max(statement.limit * 20, 1000))
        if indexed_nodes is not None:
            row_objects = []
            for node in indexed_nodes:
                if allowed and node.type.casefold() not in allowed:
                    continue
                if node.status not in ACTIVE_STATUSES:
                    continue
                if statement.command == "DELTAS" and node.properties.get("kind") not in {"compilation", None}:
                    continue
                row = _node_listing_row(node)
                if statement.where and not evaluate_condition(statement.where, row, default_alias="node"):
                    continue
                row_objects.append(row)
            default_order = _typed_node_default_order(statement.command)
            row_objects = _sort_rows(row_objects, statement.order_by, default_order)
            columns = _columns(statement.returns, DEFAULT_LIST_COLUMNS[statement.command])
            rows = [project_row(row, columns, default_alias="node") for row in row_objects[: statement.limit]]
            return QueryResult(source, statement.command, columns, rows, {"matched": len(row_objects), "node_types": list(statement.node_types), "indexed": True})
        if statement.where is None:
            default_order = _typed_node_default_order(statement.command)
            fetch_limit = max(statement.limit * 50, 10000) if statement.command == "FINDINGS" else statement.limit
            nodes = [
                node
                for type_ in sorted(statement.node_types)
                for node in self.store.find_nodes(

                    type_=type_,
                    status=sorted(ACTIVE_STATUSES),
                    limit=fetch_limit,
                    order_by=default_order,
                    descending=True,
                )
                if node.status in ACTIVE_STATUSES
            ]
            if statement.command == "DELTAS":
                nodes = [node for node in nodes if node.properties.get("kind") in {"compilation", None}]
            row_objects = [_node_listing_row(node) for node in nodes]
            row_objects = _sort_rows(row_objects, statement.order_by, default_order)
            columns = _columns(statement.returns, DEFAULT_LIST_COLUMNS[statement.command])
            rows = [project_row(row, columns, default_alias="node") for row in row_objects[: statement.limit]]
            matched = sum(self.store.count_nodes(node_types={type_}, statuses=set(ACTIVE_STATUSES)) for type_ in statement.node_types)
            return QueryResult(source, statement.command, columns, rows, {"matched": matched, "node_types": list(statement.node_types)})
        row_objects: list[dict[str, Any]] = []
        for node in self.store.all_nodes():
            if allowed and node.type.casefold() not in allowed:
                continue
            if node.status not in ACTIVE_STATUSES:
                continue
            if statement.command == "DELTAS" and node.properties.get("kind") not in {"compilation", None}:
                continue
            row = _node_listing_row(node)
            if statement.where and not evaluate_condition(statement.where, row, default_alias="node"):
                continue
            row_objects.append(row)
        default_order = _typed_node_default_order(statement.command)
        row_objects = _sort_rows(row_objects, statement.order_by, default_order)
        columns = _columns(statement.returns, DEFAULT_LIST_COLUMNS[statement.command])
        rows = [project_row(row, columns, default_alias="node") for row in row_objects[: statement.limit]]
        return QueryResult(source, statement.command, columns, rows, {"matched": len(row_objects), "node_types": list(statement.node_types)})

    def _cache_status(self, source: str, statement: CacheStatus) -> QueryResult:
        nodes = self.store.find_nodes(type_="ArtifactCacheEntry", limit=100000, order_by="updated_at")
        by_project: dict[str, dict[str, Any]] = {}
        for node in nodes:
            project_id = str(node.properties.get("project_id") or "")
            if not project_id:
                continue
            row = by_project.setdefault(
                project_id,
                {
                    "project_id": project_id,
                    "total_entries": 0,
                    "active_entries": 0,
                    "archived_entries": 0,
                    "cached_artifacts": 0,
                    "latest_compiled_at": "",
                },
            )
            row["total_entries"] += 1
            if node.status == "active":
                row["active_entries"] += 1
                row["cached_artifacts"] += 1
            elif node.status == "archived":
                row["archived_entries"] += 1
            compiled_at = str(node.properties.get("compiled_at") or "")
            if compiled_at > row["latest_compiled_at"]:
                row["latest_compiled_at"] = compiled_at
        rows = sorted(by_project.values(), key=lambda row: (row["latest_compiled_at"], row["project_id"]), reverse=True)[: statement.limit]
        columns = ["project_id", "total_entries", "active_entries", "archived_entries", "cached_artifacts", "latest_compiled_at"]
        diagnostics = {
            "projects": len(by_project),
            "entries": len(nodes),
            "active_entries": sum(1 for node in nodes if node.status == "active"),
        }
        return QueryResult(source, "CACHE STATUS", columns, rows, diagnostics)

    def _verify_finding(self, source: str, statement: VerifyFinding) -> QueryResult:
        finding = self.store.get_node(statement.finding_id)
        if finding is None:
            raise REQLEvaluationError(f"Finding not found: {statement.finding_id}")
        if finding.type != "StaticAnalysisFinding":
            raise REQLEvaluationError(f"VERIFY FINDING expects a StaticAnalysisFinding id; got {finding.type}")

        symbol = self._finding_symbol(finding)
        uses = self._finding_usage_edges(symbol) if symbol is not None else []
        risks = self._finding_risks(finding, uses)
        row = {
            "finding": self._finding_payload(finding, symbol),
            "minimal_snippet": self._finding_minimal_snippet(finding, symbol),
            "uses_found": uses,
            "scopes_checked": self._finding_scopes(finding, symbol, uses),
            "risks": risks,
            "recommended_action": self._finding_recommended_action(finding, uses, risks),
        }
        diagnostics = {
            "finding_id": finding.id,
            "symbol_id": symbol.id if symbol else None,
            "usage_edges_found": len(uses),
            "incoming_usage_edges_found": sum(1 for item in uses if item.get("direction") == "incoming"),
        }
        return QueryResult(source, "VERIFY FINDING", DEFAULT_VERIFY_FINDING_COLUMNS, [row], diagnostics)

    def _finding_symbol(self, finding: MemoryNode) -> MemoryNode | None:
        symbol_id = finding.properties.get("symbol_id")
        if symbol_id:
            symbol = self.store.get_node(str(symbol_id))
            if symbol is not None:
                return symbol
        candidates: list[MemoryNode] = []
        for edge in self.store.all_edges():
            if edge.to_id != finding.id or edge.type != "HAS_FINDING":
                continue
            node = self.store.get_node(edge.from_id)
            if node is not None and node.type not in {"SourceArtifact", "File"}:
                candidates.append(node)
        candidates.sort(key=lambda node: (node.type, node.id))
        return candidates[0] if candidates else None

    def _finding_payload(self, finding: MemoryNode, symbol: MemoryNode | None) -> dict[str, Any]:
        props = finding.properties
        return {
            "id": finding.id,
            "status": finding.status,
            "finding_type": props.get("finding_type"),
            "severity": props.get("severity"),
            "reason": props.get("reason") or finding.text,
            "confidence": props.get("confidence", finding.confidence),
            "cleanup_priority": props.get("cleanup_priority"),
            "cleanup_rank": props.get("cleanup_rank"),
            "removal_safety": props.get("removal_safety"),
            "symbol": {
                "id": symbol.id if symbol else props.get("symbol_id"),
                "type": symbol.type if symbol else props.get("symbol_type"),
                "name": props.get("symbol_name") or (symbol.properties.get("name") if symbol else None),
                "qualified_name": props.get("qualified_name") or (symbol.properties.get("qualified_name") if symbol else None),
            },
            "location": _location_payload(props),
        }

    def _finding_minimal_snippet(self, finding: MemoryNode, symbol: MemoryNode | None) -> dict[str, Any]:
        source = self._finding_source_fragment(finding, symbol)
        if source is None:
            props = finding.properties
            return {
                **_location_payload(props),
                "source_node_id": None,
                "text": _compact_text(str(finding.text or props.get("reason") or ""), max_chars=800),
                "truncated": len(str(finding.text or props.get("reason") or "")) > 800,
            }
        text = str(source.text or "")
        return {
            **_location_payload(source.properties),
            "source_node_id": source.id,
            "text": _compact_text(text, max_chars=800),
            "truncated": len(text) > 800,
        }

    def _finding_source_fragment(self, finding: MemoryNode, symbol: MemoryNode | None) -> MemoryNode | None:
        if symbol is not None:
            linked: list[MemoryNode] = []
            for edge in self.store.all_edges():
                if edge.type != "EVIDENCED_BY" or edge.from_id != symbol.id:
                    continue
                node = self.store.get_node(edge.to_id)
                if node is not None and node.type == "SourceFragment":
                    linked.append(node)
            if linked:
                linked.sort(key=_source_fragment_sort_key)
                return linked[0]

        props = finding.properties
        relative_path = props.get("relative_path")
        artifact_id = props.get("artifact_id")
        line_start = _safe_int(props.get("line_start"))
        candidates = []
        for node in self.store.all_nodes():
            if node.type != "SourceFragment" or node.status not in ACTIVE_STATUSES:
                continue
            node_props = node.properties
            if artifact_id and node_props.get("artifact_id") != artifact_id:
                continue
            if relative_path and node_props.get("relative_path") != relative_path:
                continue
            if line_start is not None and not _line_in_span(line_start, node_props):
                continue
            candidates.append(node)
        candidates.sort(key=_source_fragment_sort_key)
        return candidates[0] if candidates else None

    def _finding_usage_edges(self, symbol: MemoryNode) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for edge in self.store.all_edges():
            if edge.type not in _VERIFY_FINDING_USAGE_EDGE_TYPES:
                continue
            if edge.from_id != symbol.id and edge.to_id != symbol.id:
                continue
            direction = "outgoing" if edge.from_id == symbol.id else "incoming"
            other_id = edge.to_id if direction == "outgoing" else edge.from_id
            other = self.store.get_node(other_id)
            rows.append(
                {
                    "edge_id": edge.id,
                    "edge_type": edge.type,
                    "direction": direction,
                    "meaning": "symbol_uses_target" if direction == "outgoing" else "source_uses_symbol",
                    "other_id": other_id,
                    "other_type": other.type if other else None,
                    "other_label": other.label if other else None,
                    "location": _location_payload(edge.properties),
                    "evidence": edge.properties.get("evidence"),
                }
            )
        rows.sort(key=lambda row: (str(row.get("direction")), str(row.get("edge_type")), str(row.get("other_id")), str(row.get("edge_id"))))
        return rows

    def _finding_scopes(self, finding: MemoryNode, symbol: MemoryNode | None, uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        props = finding.properties
        scopes = [
            {
                "scope": "finding",
                "finding_id": finding.id,
                "status": finding.status,
                "finding_type": props.get("finding_type"),
            },
            {
                "scope": "artifact",
                "artifact_id": props.get("artifact_id"),
                "relative_path": props.get("relative_path"),
                "context_scope": props.get("context_scope"),
                "evidence_scope": props.get("evidence_scope"),
            },
            {
                "scope": "symbol",
                "symbol_id": symbol.id if symbol else props.get("symbol_id"),
                "symbol_type": symbol.type if symbol else props.get("symbol_type"),
                "qualified_name": props.get("qualified_name") or (symbol.properties.get("qualified_name") if symbol else None),
            },
            {
                "scope": "usage_edges",
                "checked_edge_types": sorted({item["edge_type"] for item in uses} | _VERIFY_FINDING_USAGE_EDGE_TYPES),
                "found": len(uses),
                "incoming_found": sum(1 for item in uses if item.get("direction") == "incoming"),
            },
        ]
        return scopes

    def _finding_risks(self, finding: MemoryNode, uses: list[dict[str, Any]]) -> list[str]:
        props = finding.properties
        risks: list[str] = []
        if finding.status != "active":
            risks.append(f"finding_status_{finding.status}")
        risks.extend(str(item) for item in props.get("blocking_signals") or [])
        if any(item.get("direction") == "incoming" for item in uses):
            risks.append("deterministic_incoming_usage_edges_present")
        removal_safety = str(props.get("removal_safety") or "")
        if removal_safety in {"validate", "risky"}:
            risks.append(f"removal_safety_{removal_safety}")
        validation_reason = props.get("validation_reason")
        if validation_reason:
            risks.append(str(validation_reason))
        return list(dict.fromkeys(risks))

    def _finding_recommended_action(self, finding: MemoryNode, uses: list[dict[str, Any]], risks: list[str]) -> str:
        props = finding.properties
        symbol = props.get("symbol_name") or props.get("qualified_name") or finding.label or finding.id
        path = props.get("relative_path") or "the reported artifact"
        if finding.status != "active":
            return f"No cleanup action: `{symbol}` finding is {finding.status}; refresh findings before editing."
        if any(item.get("direction") == "incoming" for item in uses):
            return f"Do not remove `{symbol}` automatically; review the deterministic incoming usage edges first."
        safety = str(props.get("removal_safety") or "validate")
        if safety == "safe" and not risks:
            return f"Remove `{symbol}` in `{path}`, then rerun project compile and tests."
        if safety == "safe":
            return f"Remove `{symbol}` in `{path}` only after checking the listed risks, then rerun project compile and tests."
        if safety == "validate":
            return f"Validate `{symbol}` against the listed scope and risks before removal; if clear, edit `{path}` and rerun project compile and tests."
        return f"Treat `{symbol}` as a review item; keep it unless manual validation clears the listed risks."


# ----------------------------------------------------------------------
# Condition evaluation and projection helpers
# ----------------------------------------------------------------------
def _node_listing_row(node: MemoryNode) -> dict[str, Any]:
    row = dict(node.properties)
    row.update(
        {
            "node": node,
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "text": node.text,
            "canonical_key": node.canonical_key,
            "status": node.status,
            "salience": node.salience,
            "confidence": node.confidence,
            "activation": node.activation,
            "utility": node.utility,
            "created_at": node.created_at,
            "updated_at": node.updated_at,
        }
    )
    return row


def _sort_rows(rows: list[dict[str, Any]], order_by: SortSpec | None, default_field: str) -> list[dict[str, Any]]:
    field = order_by.field if order_by else default_field
    descending = order_by.descending if order_by else True
    return sorted(rows, key=lambda row: _sort_key(_sort_value(field, resolve_field(row, field, default_alias="node"))), reverse=descending)


def _typed_node_default_order(command: str) -> str:
    if command == "DELTAS":
        return "created_at"
    if command == "FINDINGS":
        return "cleanup_rank"
    return "updated_at"


def _sort_value(field: str, value: Any) -> Any:
    if _strip_default_alias(field, "node") == "cleanup_priority":
        return {"high": 3, "medium": 2, "low": 1}.get(str(value).casefold(), 0)
    return value


def _location_payload(properties: Any) -> dict[str, Any]:
    if not isinstance(properties, dict):
        return {"relative_path": None, "line_start": None, "line_end": None, "artifact_id": None}
    metadata = properties.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "relative_path": properties.get("relative_path") or properties.get("source_file") or metadata.get("relative_path") or metadata.get("source_file") or metadata.get("source_path"),
        "line_start": properties.get("line_start", properties.get("start_line", metadata.get("line_start", metadata.get("start_line")))),
        "line_end": properties.get("line_end", properties.get("end_line", metadata.get("line_end", metadata.get("end_line")))),
        "artifact_id": properties.get("artifact_id") or metadata.get("artifact_id"),
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _line_in_span(line: int, properties: dict[str, Any]) -> bool:
    start = _safe_int(properties.get("line_start", properties.get("start_line")))
    end = _safe_int(properties.get("line_end", properties.get("end_line")))
    if start is None:
        return False
    if end is None:
        end = start
    return start <= line <= end


def _source_fragment_sort_key(node: MemoryNode) -> tuple[int, int, str]:
    props = node.properties
    start = _safe_int(props.get("line_start", props.get("start_line"))) or 0
    end = _safe_int(props.get("line_end", props.get("end_line"))) or start
    return (max(end - start, 0), start, node.id)


def _compact_text(text: str, *, max_chars: int) -> str:
    normalized = text.strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def evaluate_condition(condition: Condition, row: dict[str, Any], *, default_alias: str | None = None) -> bool:
    if isinstance(condition, BooleanCondition):
        left = evaluate_condition(condition.left, row, default_alias=default_alias)
        if condition.operator == "AND":
            return left and evaluate_condition(condition.right, row, default_alias=default_alias)
        return left or evaluate_condition(condition.right, row, default_alias=default_alias)
    if isinstance(condition, NotCondition):
        return not evaluate_condition(condition.condition, row, default_alias=default_alias)
    if isinstance(condition, Comparison):
        actual = resolve_field(row, condition.field, default_alias=default_alias)
        return _compare(actual, condition.operator, condition.value)
    return False


def resolve_field(row: dict[str, Any], field: str, *, default_alias: str | None = None) -> Any:
    if field in row:
        return row[field]
    parts = field.split(".")
    if parts[0] in row:
        subject = row[parts[0]]
        return _resolve_on_object(subject, parts[1:]) if len(parts) > 1 else _format_value(subject)
    if default_alias and default_alias in row:
        return _resolve_on_object(row[default_alias], parts)
    if "node" in row:
        return _resolve_on_object(row["node"], parts)
    if "edge" in row:
        return _resolve_on_object(row["edge"], parts)
    return None


def project_row(row: dict[str, Any], columns: Sequence[str], *, default_alias: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in columns:
        out[col] = _format_value(resolve_field(row, col, default_alias=default_alias))
    return out


def _resolve_on_object(subject: Any, parts: list[str]) -> Any:
    value = subject
    for part in parts:
        if value is None:
            return None
        if isinstance(value, dict):
            if part in value:
                value = value[part]
            elif "properties" in value and isinstance(value["properties"], dict):
                value = value["properties"].get(part)
            else:
                value = None
        elif hasattr(value, part):
            value = getattr(value, part)
        elif hasattr(value, "properties") and isinstance(value.properties, dict):
            value = value.properties.get(part)
        else:
            value = None
    return value


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "EXISTS":
        return actual is not None and actual != ""
    if operator == "IS":
        return _equal(actual, expected)
    if operator == "IS_NOT":
        return not _equal(actual, expected)
    if operator == "IN":
        if isinstance(expected, list):
            return any(_equal(actual, item) for item in expected)
        return False
    if operator == "BETWEEN":
        if not isinstance(expected, list) or len(expected) != 2:
            return False
        left, lower = _coerce_orderable(actual, expected[0])
        right, upper = _coerce_orderable(actual, expected[1])
        if left is None or lower is None or right is None or upper is None:
            return False
        return left >= lower and right <= upper
    if operator in {"CONTAINS", "~="}:
        if actual is None:
            return False
        if isinstance(actual, (list, tuple, set)):
            return any(_equal(item, expected) for item in actual)
        return str(expected).casefold() in str(actual).casefold()
    if operator in {"LIKE", "ILIKE"}:
        if actual is None:
            return False
        return _like(str(actual), str(expected), case_sensitive=operator == "LIKE")
    if operator == "REGEX":
        if actual is None:
            return False
        try:
            return re.search(str(expected), str(actual)) is not None
        except re.error:
            return False
    if operator == "STARTS_WITH":
        return actual is not None and str(actual).casefold().startswith(str(expected).casefold())
    if operator == "ENDS_WITH":
        return actual is not None and str(actual).casefold().endswith(str(expected).casefold())
    if operator in {"=", "=="}:
        return _equal(actual, expected)
    if operator == "!=":
        return not _equal(actual, expected)
    left, right = _coerce_orderable(actual, expected)
    if left is None or right is None:
        return False
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    return False


def _like(actual: str, pattern: str, *, case_sensitive: bool) -> bool:
    regex = "^" + "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern) + "$"
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.match(regex, actual, flags=flags) is not None


def _equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(actual) is bool(expected)
    if actual is None or expected is None:
        return actual is expected
    return str(actual).casefold() == str(expected).casefold()


def _coerce_orderable(actual: Any, expected: Any) -> tuple[Any, Any]:
    try:
        if isinstance(actual, (int, float)) or isinstance(expected, (int, float)):
            return float(actual), float(expected)
    except (TypeError, ValueError):
        return None, None
    if actual is None or expected is None:
        return None, None
    return str(actual).casefold(), str(expected).casefold()


def _format_value(value: Any) -> Any:
    if isinstance(value, MemoryNode):
        return _node_brief(value)
    if isinstance(value, MemoryEdge):
        return _edge_brief(value)
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_format_value(v) for v in value]
    if isinstance(value, tuple):
        return [_format_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _format_value(v) for k, v in value.items()}
    return value


def _location_fields(properties: Any) -> dict[str, Any]:
    if not isinstance(properties, dict):
        return {"path": None, "relative_path": None, "source_url": None, "line_start": None, "line_end": None, "section": None, "artifact_id": None}
    props = dict(properties)
    metadata = props.get("metadata")
    if isinstance(metadata, dict):
        for key in ("source_path", "path", "relative_path", "source_file", "source_url", "url"):
            if key not in props and metadata.get(key) is not None:
                props[key] = metadata.get(key)
        for key in ("line_start", "start_line", "line_end", "end_line"):
            if key not in props and metadata.get(key) is not None:
                props[key] = metadata.get(key)
    return {
        "path": props.get("path") or props.get("source_path"),
        "relative_path": props.get("relative_path") or props.get("source_file"),
        "source_url": props.get("source_url") or props.get("url"),
        "line_start": props.get("line_start", props.get("start_line")),
        "line_end": props.get("line_end", props.get("end_line")),
        "section": props.get("section_path") or props.get("section"),
        "artifact_id": props.get("artifact_id"),
    }


def _node_brief(node: MemoryNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "label": node.label,
        "text": node.text,
        "canonical_key": node.canonical_key,
        "status": node.status,
        "salience": node.salience,
        "confidence": node.confidence,
        "activation": node.activation,
    }


def _edge_brief(edge: MemoryEdge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "type": edge.type,
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "polarity": edge.polarity,
        "origin": edge.origin,
    }


def _explain_edge_row(node: MemoryNode, edge: MemoryEdge, neighbor: MemoryNode, direction: str) -> dict[str, Any]:
    return {
        "node_id": node.id,
        "node_type": node.type,
        "direction": direction,
        "edge_type": edge.type,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "neighbor_id": neighbor.id,
        "neighbor_type": neighbor.type,
        "neighbor_label": neighbor.label or neighbor.text or neighbor.canonical_key,
    }


def _path_row(path_nodes: list[MemoryNode], path_edges: list[MemoryEdge], score: float) -> dict[str, Any]:
    length = len(path_edges)
    normalized = clamp((score / max(length, 1)) * 0.85 + 0.15 * sum(n.salience for n in path_nodes) / max(len(path_nodes), 1))
    return {
        "length": length,
        "score": normalized,
        "node_ids": [node.id for node in path_nodes],
        "node_types": [node.type for node in path_nodes],
        "node_labels": [node.label or node.text or node.canonical_key or node.id for node in path_nodes],
        "edge_ids": [edge.id for edge in path_edges],
        "edge_types": [edge.type for edge in path_edges],
    }


def _columns(return_spec: Any, defaults: list[str]) -> list[str]:
    if return_spec.is_all:
        return list(defaults)
    if return_spec.is_default:
        return list(defaults)
    return list(return_spec.fields)


def _simple_indexable_comparison(condition: Condition | None) -> Comparison | None:
    if condition is None:
        return None
    if isinstance(condition, Comparison) and condition.operator in {"=", "=="}:
        return condition
    if isinstance(condition, BooleanCondition) and condition.operator == "AND":
        left = _simple_indexable_comparison(condition.left)
        if left is not None:
            return left
        return _simple_indexable_comparison(condition.right)
    return None


def _strip_default_alias(field: str, default_alias: str) -> str:
    prefix = f"{default_alias}."
    if field.startswith(prefix):
        return field[len(prefix) :]
    return field


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, (int, float)):
        return (1, float(value))
    return (1, str(value).casefold())


def _casefold_set(values: Iterable[str]) -> set[str]:
    return {value.casefold() for value in values}


def execute_reql(graph: Any, source: str) -> QueryResult:
    return QueryEvaluator(graph).execute(source)
